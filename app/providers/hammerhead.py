"""Hammerhead activity source.

Hammerhead's list/detail JSON carries only ``id``, ``name``, ``createdAt``,
``duration``, ``distance`` and (detail only) ``activityType``, ``description``,
``updatedAt`` — no heart rate, power, cadence, elevation gain, or moving/elapsed
split. All of that only exists inside the ``.fit`` file, so ``fetch_detail``
and ``fetch_streams`` both go through :mod:`fitfiles` to download (once),
permanently store, and analyze it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

import fitfiles
from catalog import AuthMethod, DataKind
from models import Activity
from providers.base import (
    OAuthStateGuard,
    RateLimitSnapshot,
    external_id,
    granted_if_connected,
    prefixed_id,
)
from providers.hammerhead_client import DEFAULT_API_BASE, HammerheadClient
from providers.mapping import as_float, as_int, clip_text, parse_dt

logger = logging.getLogger(__name__)

SOURCE_NAME = "hammerhead"
ID_PREFIX = "hammerhead_"

CONNECT_PATH = "/auth/hammerhead"
CALLBACK_PATH = "/auth/hammerhead/callback"
DISCONNECT_PATH = "/auth/hammerhead/disconnect"

#: Hammerhead's ``activityType`` (bike-computer specific) mapped onto the
#: Strava-flavoured vocabulary ``metrics.SPORT_TRIMP_PER_MIN`` understands, so
#: the no-HR TRIMP estimate fallback behaves sensibly.
_ACTIVITY_TYPE_SPORT_MAP = {
    "RIDE": "Ride",
    "EBIKE": "Ride",
    "VELOMOBILE": "Ride",
    "MOUNTAIN_BIKE": "MountainBikeRide",
    "EMOUNTAIN_BIKE": "MountainBikeRide",
    "GRAVEL": "GravelRide",
}

#: ``startDate`` filter semantics are undocumented, and a Karoo device syncs to
#: the cloud opportunistically — a ride can be recorded on one day and only
#: appear (via ``createdAt``) days later. Reconcile is off for this source (see
#: ``HammerheadSource.supports_reconcile``), so pad the incremental window's
#: lower bound generously. Upserts are idempotent, so the only cost of padding
#: too much is a bit of extra pagination.
_LIST_SAFETY_MARGIN_DAYS = 14

_PER_PAGE = 100

#: Bounds the fetch_detail → fetch_streams handoff cache (see
#: ``_cache_analysis``). The engine streams each activity right after merging
#: its detail, so the cache only has to cover the activities in flight at
#: once (``sync.detail_concurrency``), not a whole backfill.
_ANALYSIS_CACHE_MAX = 32


def activity_id(raw: object) -> str:
    """Prefixed local id for a Hammerhead activity."""
    return prefixed_id(ID_PREFIX, raw)


def map_row(payload: dict, athlete_id: int) -> dict | None:
    """Map a Hammerhead activity payload onto ``activities`` columns.

    Pure and synchronous. ``payload`` is either a bare list-phase
    ``ActivitySummary`` (``id``, ``name``, ``createdAt`` = end time,
    ``duration`` in ms, ``distance``) or a detail-phase payload that additionally carries
    ``activityType``, ``description``, ``updatedAt``, and a private
    ``_fit_summary`` dict (populated by :meth:`HammerheadSource.fetch_detail`
    from the downloaded FIT file). Everything HR/power/elevation-related comes
    from ``_fit_summary`` when present; list-phase rows are provisional and get
    corrected once detail runs.

    ``athlete_id`` is the local athlete id, never anything from the payload.
    """
    raw_id = payload.get("id")
    if raw_id is None:
        logger.warning("Hammerhead activity payload has no id; skipping")
        return None

    fit = payload.get("_fit_summary") or {}

    # The summary's ``duration`` is in milliseconds and ``createdAt`` is when
    # the ride was saved, i.e. its end. Superseded rows never get a detail
    # merge, so these provisional values must be right on their own.
    duration = round(as_float(payload.get("duration")) or 0.0) // 1000
    created = parse_dt(payload.get("createdAt"))
    list_start = created - timedelta(seconds=duration) if created else None

    start = parse_dt(fit.get("start_date")) or list_start
    if start is None:
        logger.warning("Hammerhead activity %s has no start date; skipping", raw_id)
        return None

    fit_elapsed = fit.get("elapsed_time")
    elapsed = as_int(fit_elapsed) if fit_elapsed is not None else duration
    fit_moving = fit.get("moving_time")
    moving = as_int(fit_moving) if fit_moving is not None else elapsed

    fit_distance = fit.get("distance")
    distance = (
        as_float(fit_distance)
        if fit_distance is not None
        else as_float(payload.get("distance")) or 0.0
    )

    sport = _ACTIVITY_TYPE_SPORT_MAP.get(payload.get("activityType"), "")

    # FIT files carry no work total; derive it the way Strava does.
    average_watts = as_float(fit.get("average_watts"))
    device_watts = bool(fit.get("device_watts"))
    kilojoules = (
        average_watts * moving / 1000.0
        if device_watts and average_watts and moving
        else None
    )

    return {
        "id": activity_id(raw_id),
        "source": SOURCE_NAME,
        "athlete_id": athlete_id,
        "name": clip_text(payload.get("name"), 500) or "",
        "description": clip_text(payload.get("description"), 10000),
        "private_note": None,
        "sport_type": sport,
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": moving,
        "distance": distance,
        "average_heartrate": as_float(fit.get("average_heartrate")),
        "max_heartrate": as_float(fit.get("max_heartrate")),
        "has_heartrate": bool(fit.get("has_heartrate")),
        "average_watts": average_watts,
        "max_watts": as_float(fit.get("max_watts")),
        "average_speed": as_float(fit.get("average_speed")),
        "total_elevation_gain": as_float(fit.get("total_elevation_gain")),
        "kilojoules": kilojoules,
        "calories": as_float(fit.get("calories")),
        "device_watts": device_watts,
        "device_name": clip_text(fit.get("device_name"), 255),
        "gear_id": None,
        "gear_name": None,
        "weighted_average_watts": as_float(fit.get("weighted_average_watts")),
        "suffer_score": None,
        "training_load": None,
        "provider_updated_at": parse_dt(payload.get("updatedAt")),
    }


class HammerheadSource:
    """Hammerhead (Karoo bike computers), authenticated with OAuth.

    Hammerhead publishes no documented rate-limit headers or budget, and its
    activity list can only be filtered by a starting date — there is no
    updated-since marker, so reconcile is not worth the wasted FIT
    re-downloads it would otherwise cause (see ``supports_reconcile``).
    """

    name = SOURCE_NAME
    id_prefix = ID_PREFIX
    supports_detail = True
    supports_streams = True
    supports_reconcile = False
    auth = AuthMethod.OAUTH
    kind = DataKind.ACTIVITIES
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
        self._client: HammerheadClient | None = None
        self._oauth_state = OAuthStateGuard()
        # Handoff cache from fetch_detail to fetch_streams so the same FIT
        # bytes aren't parsed twice when both phases process an activity in
        # the same enrich pass (see _cache_analysis/_take_cached_analysis).
        self._analysis_cache: "OrderedDict[str, fitfiles.FitAnalysis]" = (
            OrderedDict()
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self._app_base_url}{CALLBACK_PATH}"

    def _build_client(self, session) -> HammerheadClient:
        return HammerheadClient(
            session,
            client_id=self._client_id,
            client_secret=self._client_secret,
            api_base=self._api_base,
            redirect_uri=self.redirect_uri,
        )

    @property
    def client(self) -> HammerheadClient:
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
        """Validate the stored token with a cheap call, no profile endpoint exists."""
        try:
            await self.client.get_activities(page=1, per_page=1)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError(
                    "Hammerhead rejected the stored token "
                    f"(HTTP {exc.response.status_code}). Reconnect Hammerhead "
                    "from the home page."
                ) from exc
            raise
        async with self._session_factory() as session:
            external_user_id = await self._build_client(session).get_external_user_id()
        if external_user_id is None:
            return None
        try:
            return int(external_user_id)
        except (TypeError, ValueError):
            return None

    def register_auth_routes(self, app: FastAPI) -> None:
        @app.get(CONNECT_PATH)
        async def hammerhead_connect():
            if not self.is_configured():
                return RedirectResponse(
                    url="/?error=hammerhead_not_configured", status_code=303
                )
            state = self._oauth_state.issue()
            async with self._session_factory() as session:
                url = self._build_client(session).authorization_url(state=state)
            return RedirectResponse(url=url, status_code=307)

        @app.get(CALLBACK_PATH)
        async def hammerhead_callback(
            code: str | None = None,
            state: str | None = None,
            error: str | None = None,
        ):
            if error or not code:
                return RedirectResponse(
                    url=f"/?error={error or 'missing_code'}", status_code=303
                )
            if not self._oauth_state.verify(state):
                logger.warning(
                    "Hammerhead OAuth callback rejected: bad or missing state"
                )
                return RedirectResponse(
                    url="/?error=hammerhead_invalid_state", status_code=303
                )
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    payload = await client.exchange_code(code)
                    await client.store_tokens(payload)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    logger.error("Hammerhead OAuth exchange failed: %s", exc)
                    return RedirectResponse(
                        url="/?error=hammerhead_oauth_failed", status_code=303
                    )
                finally:
                    await client.close()
            return RedirectResponse(url="/?connected=hammerhead", status_code=303)

        @app.post(DISCONNECT_PATH)
        async def hammerhead_disconnect():
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    await client.disconnect()
                finally:
                    await client.close()
            return RedirectResponse(url="/", status_code=303)

    # ── Data ────────────────────────────────────────────────────────

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        """Page ``/activities`` from a padded ``start`` onward.

        Hammerhead has no upper-bound filter, so pages are fetched until
        exhausted and anything at or past ``end`` is dropped client-side.
        """
        padded_start = start - timedelta(days=_LIST_SAFETY_MARGIN_DAYS)
        start_date: date = padded_start.astimezone(timezone.utc).date()

        out: list[dict] = []
        page = 1
        while True:
            envelope = await self.client.get_activities(
                page=page, per_page=_PER_PAGE, start_date=start_date
            )
            batch = envelope.get("data") or []
            out.extend(batch)
            total_pages = envelope.get("totalPages") or 1
            if page >= total_pages or not batch:
                break
            page += 1

        # Re-listing an already-detailed activity is harmless:
        # ``engine.upsert_rows`` leaves detail-merged rows untouched.
        return [item for item in out if not _at_or_past(item.get("createdAt"), end)]

    async def fetch_detail(self, activity_external_id: str) -> dict | None:
        """Fetch detail JSON and merge in the FIT-derived summary.

        Downloads (or reuses a previously stored) FIT file and runs it through
        the generic analyzer so :func:`map_row` stays pure and synchronous. A
        missing or unreadable FIT file degrades gracefully: detail still
        merges name/description/activityType, just without HR/power/etc.
        """
        try:
            payload = await self.client.get_activity(activity_external_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                # Deleted upstream: stop asking.
                return None
            raise

        local_id = activity_id(activity_external_id)
        payload["_fit_summary"] = None
        try:
            raw = await self._ensure_fit_bytes(local_id, activity_external_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                logger.warning(
                    "Hammerhead activity %s has no FIT file upstream; "
                    "detail fields stay provisional",
                    activity_external_id,
                )
                raw = None
            else:
                raise
        else:
            try:
                analysis = await _analyze(raw)
            except fitfiles.FitParseError as exc:
                logger.warning(
                    "Could not parse FIT file for Hammerhead activity %s: %s",
                    activity_external_id,
                    exc,
                )
            else:
                payload["_fit_summary"] = asdict(analysis.summary)
                # fetch_streams runs shortly after, on the same bytes; hand it
                # the already-parsed streams instead of re-parsing.
                self._cache_analysis(local_id, analysis)
        return payload

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        cached = self._take_cached_analysis(activity.id)
        if cached is not None:
            return cached.streams

        raw = await self._ensure_fit_bytes(
            activity.id, external_id(ID_PREFIX, activity.id)
        )
        try:
            analysis = await _analyze(raw)
        except fitfiles.FitParseError as exc:
            logger.warning(
                "Could not parse FIT file for Hammerhead activity %s: %s",
                activity.id,
                exc,
            )
            return {}
        return analysis.streams

    def _cache_analysis(self, local_id: str, analysis: "fitfiles.FitAnalysis") -> None:
        self._analysis_cache[local_id] = analysis
        self._analysis_cache.move_to_end(local_id)
        while len(self._analysis_cache) > _ANALYSIS_CACHE_MAX:
            self._analysis_cache.popitem(last=False)

    def _take_cached_analysis(self, local_id: str) -> "fitfiles.FitAnalysis | None":
        return self._analysis_cache.pop(local_id, None)

    async def _ensure_fit_bytes(self, local_id: str, provider_id: str) -> bytes:
        """Read the permanently stored FIT file, or download and store it once."""
        async with self._session_factory() as session:
            cached = await fitfiles.get_fit_bytes(session, local_id)
        if cached is not None:
            return cached

        raw = await self.client.get_activity_fit_file(provider_id)
        async with self._session_factory() as session:
            await fitfiles.save_fit_bytes(
                session, activity_id=local_id, source=SOURCE_NAME, raw_data=raw
            )
        return raw

    def feeds(self) -> dict[DataKind, "HammerheadSource"]:
        # Single-kind provider: the source is its own activities feed.
        return {DataKind.ACTIVITIES: self}

    async def granted_kinds(self) -> frozenset[DataKind]:
        return await granted_if_connected(self)

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        return map_row(payload, athlete_id)

    # ── Bookkeeping ─────────────────────────────────────────────────

    def rate_limits(self) -> RateLimitSnapshot:
        # No documented quota headers.
        return RateLimitSnapshot(known=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None


def _at_or_past(raw_created_at: object, end: datetime) -> bool:
    created = parse_dt(raw_created_at)
    if created is None:
        return False
    return created >= end


async def _analyze(raw: bytes) -> "fitfiles.FitAnalysis":
    """Parse FIT bytes off the event loop.

    ``fitfiles.analyze_fit_file`` is synchronous and CPU-bound over every
    record in the file; running it inline would block the shared event loop
    for the duration of the parse while other activities are being fetched
    concurrently (``engine.run_with_concurrency``) and while the app serves
    HTTP requests.
    """
    return await asyncio.to_thread(fitfiles.analyze_fit_file, raw)
