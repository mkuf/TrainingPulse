"""Strava activity source."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from models import Activity
from sources.base import (
    OAuthStateGuard,
    RateLimitSnapshot,
    SourceRateLimitExceeded,
    external_id,
    prefixed_id,
)
from sources.mapping import as_float, as_int, clip_text, normal_gear_id, parse_dt
from strava_client import DEFAULT_API_BASE, StravaClient

logger = logging.getLogger(__name__)

SOURCE_NAME = "strava"
ID_PREFIX = "strava_"

CONNECT_PATH = "/auth/strava"
CALLBACK_PATH = "/auth/strava/callback"
DISCONNECT_PATH = "/auth/strava/disconnect"

#: Streams Strava returns keyed by type, mapped onto canonical names.
_STREAM_ALIASES = {
    "time": "time",
    "heartrate": "heartrate",
    "watts": "watts",
    "cadence": "cadence",
    "distance": "distance",
    "altitude": "altitude",
    "latlng": "latlng",
    "velocity_smooth": "velocity_smooth",
    "temp": "temp",
    "grade_smooth": "grade_smooth",
}


def activity_id(raw: object) -> str:
    """Prefixed local id for a Strava activity.

    Idempotent, so already-prefixed ids from rows preserved across the
    intervals.icu migration map back onto themselves.
    """
    return prefixed_id(ID_PREFIX, raw)


def map_row(payload: dict, athlete_id: int) -> dict | None:
    """Map a Strava activity payload onto ``activities`` columns.

    Pure and synchronous. Gear display names are resolved in
    :meth:`StravaSource.fetch_detail` and arrive on the payload as
    ``gear_name``, so this stays testable against recorded fixtures.

    ``athlete_id`` is the local athlete id, never ``payload["athlete"]["id"]``:
    the provider's own id must not overwrite the local key.
    """
    raw_id = payload.get("id")
    if raw_id is None:
        logger.warning("Strava activity payload has no id; skipping")
        return None

    # ``start_date_local`` is local wall-clock time with no offset, not UTC;
    # ``parse_dt`` treats a naive timestamp as UTC, so falling back to it here
    # would silently shift the stored time. Only ``start_date`` (true UTC) is
    # usable without also knowing the athlete's UTC offset.
    start = parse_dt(payload.get("start_date"))
    if start is None:
        logger.warning("Strava activity %s has no start_date; skipping", raw_id)
        return None

    moving = as_int(payload.get("moving_time"))
    elapsed = as_int(payload.get("elapsed_time"), moving)

    avg_hr = payload.get("average_heartrate")
    max_hr = payload.get("max_heartrate")
    has_hr = bool(payload.get("has_heartrate") or avg_hr or max_hr)

    sport = payload.get("sport_type") or payload.get("type") or ""

    gear_id = normal_gear_id(payload.get("gear_id"))
    gear = payload.get("gear")
    gear_name = None
    if isinstance(gear, dict):
        gear_name = clip_text(gear.get("nickname") or gear.get("name"), 255)
        if not gear_id:
            gear_id = normal_gear_id(gear.get("id"))
    if gear_name is None:
        gear_name = clip_text(payload.get("gear_name"), 255)

    return {
        "id": activity_id(raw_id),
        "source": SOURCE_NAME,
        "athlete_id": athlete_id,
        "name": clip_text(payload.get("name"), 500) or "",
        "description": payload.get("description"),
        "private_note": payload.get("private_note"),
        "sport_type": str(sport),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": moving,
        "distance": as_float(payload.get("distance")) or 0.0,
        "average_heartrate": as_float(avg_hr),
        "max_heartrate": as_float(max_hr),
        "has_heartrate": has_hr,
        "average_watts": as_float(payload.get("average_watts")),
        "max_watts": as_float(payload.get("max_watts")),
        "average_speed": as_float(payload.get("average_speed")),
        "total_elevation_gain": as_float(payload.get("total_elevation_gain")),
        "kilojoules": as_float(payload.get("kilojoules")),
        "calories": as_float(payload.get("calories")),
        "device_watts": bool(payload.get("device_watts")),
        "device_name": clip_text(payload.get("device_name"), 255),
        "gear_id": gear_id,
        "gear_name": gear_name,
        "weighted_average_watts": as_float(payload.get("weighted_average_watts")),
        "suffer_score": as_float(payload.get("suffer_score")),
        "training_load": None,
        "provider_updated_at": parse_dt(payload.get("updated_at")),
    }


def normalize_streams(raw: dict) -> dict[str, list]:
    """Unwrap Strava's ``{type: {"data": [...]}}`` stream payload."""
    streams: dict[str, list] = {}
    for key, value in raw.items():
        canonical = _STREAM_ALIASES.get(key, key)
        samples = None
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            samples = value["data"]
        elif isinstance(value, list):
            samples = value
        if samples is not None:
            streams[canonical] = samples
    return streams


class StravaSource:
    """Strava, authenticated with OAuth.

    Strava's default read budget is 100 requests per 15 minutes and 1000 per
    day — roughly twenty times tighter than intervals.icu — so the engine only
    spends detail and stream calls on activities that win on precedence.
    """

    name = SOURCE_NAME
    id_prefix = ID_PREFIX
    supports_detail = True
    supports_streams = True
    supports_reconcile = True
    requires_authorization = True
    connect_path = CONNECT_PATH
    disconnect_path = DISCONNECT_PATH

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        client_id: str,
        client_secret: str,
        api_base: str = DEFAULT_API_BASE,
        app_base_url: str = "",
    ):
        self._session_factory = session_factory
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_base = api_base
        self._app_base_url = app_base_url.rstrip("/")
        self._session = None
        self._client: StravaClient | None = None
        self._oauth_state = OAuthStateGuard()

    @property
    def redirect_uri(self) -> str:
        return f"{self._app_base_url}{CALLBACK_PATH}"

    def _build_client(self, session) -> StravaClient:
        return StravaClient(
            session,
            client_id=self._client_id,
            client_secret=self._client_secret,
            api_base=self._api_base,
            redirect_uri=self.redirect_uri,
        )

    @property
    def client(self) -> StravaClient:
        """Long-lived client for sync work, on its own session.

        A token refresh commits, and it must not commit half-finished activity
        writes along with it.
        """
        if self._client is None:
            self._session = self._session_factory()
            self._client = self._build_client(self._session)
        return self._client

    # ── Identity / auth ─────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def is_connected(self) -> bool:
        if not self.is_configured():
            return False
        async with self._session_factory() as session:
            return await self._build_client(session).is_connected()

    async def verify_credentials(self) -> int | None:
        try:
            athlete = await self.client.get_athlete()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError(
                    "Strava rejected the stored token "
                    f"(HTTP {exc.response.status_code}). Reconnect Strava from "
                    "the home page."
                ) from exc
            raise
        raw = athlete.get("id")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def register_auth_routes(self, app: FastAPI) -> None:
        @app.get(CONNECT_PATH)
        async def strava_connect():
            if not self.is_configured():
                return RedirectResponse(
                    url="/?error=strava_not_configured", status_code=303
                )
            state = self._oauth_state.issue()
            async with self._session_factory() as session:
                url = self._build_client(session).authorization_url(state=state)
            return RedirectResponse(url=url, status_code=307)

        @app.get(CALLBACK_PATH)
        async def strava_callback(
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
        ):
            if error or not code:
                return RedirectResponse(
                    url=f"/?error={error or 'missing_code'}", status_code=303
                )
            if not self._oauth_state.verify(state):
                logger.warning("Strava OAuth callback rejected: bad or missing state")
                return RedirectResponse(
                    url="/?error=strava_invalid_state", status_code=303
                )
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    payload = await client.exchange_code(code)
                    await client.store_tokens(payload)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    logger.error("Strava OAuth exchange failed: %s", exc)
                    return RedirectResponse(
                        url="/?error=strava_oauth_failed", status_code=303
                    )
                finally:
                    await client.close()
            return RedirectResponse(url="/?connected=strava", status_code=303)

        @app.post(DISCONNECT_PATH)
        async def strava_disconnect():
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    await client.disconnect()
                finally:
                    await client.close()
            return RedirectResponse(url="/", status_code=303)

    # ── Data ────────────────────────────────────────────────────────

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        """Page ``/athlete/activities`` across ``[start, end)``."""
        after = int(start.astimezone(timezone.utc).timestamp())
        before = int(end.astimezone(timezone.utc).timestamp())
        per_page = 200
        out: list[dict] = []
        page = 1
        while True:
            batch = await self.client.get_activities(
                page=page, per_page=per_page, after=after, before=before
            )
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    async def fetch_detail(self, activity_external_id: str) -> dict | None:
        """Fetch detail and resolve the gear display name in one step.

        Resolving gear here is what keeps :func:`map_row` pure and synchronous.
        """
        try:
            payload = await self.client.get_activity(activity_external_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                # Deleted or made private upstream: stop asking.
                return None
            raise

        gear_id = normal_gear_id(payload.get("gear_id"))
        if gear_id and not payload.get("gear_name"):
            try:
                payload["gear_name"] = await self.client.get_gear_display_name(gear_id)
            except SourceRateLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - gear is cosmetic
                logger.warning("Could not resolve Strava gear %s: %s", gear_id, exc)
        return payload

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        raw = await self.client.get_activity_streams(
            external_id(ID_PREFIX, activity.id)
        )
        return normalize_streams(raw)

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        return map_row(payload, athlete_id)

    # ── Bookkeeping ─────────────────────────────────────────────────

    def rate_limits(self) -> RateLimitSnapshot:
        if self._client is None:
            return RateLimitSnapshot(known=False)
        used_15, limit_15 = self._client.tightest_15min
        used_daily, limit_daily = self._client.tightest_daily
        return RateLimitSnapshot(
            used_15min=used_15,
            limit_15min=limit_15,
            used_daily=used_daily,
            limit_daily=limit_daily,
            checked_at=datetime.now(timezone.utc),
            known=self._client.rate_limit_seen,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None
