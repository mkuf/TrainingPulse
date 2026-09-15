"""Google Health activity source.

Google Health has no per-activity detail endpoint — the list payload is already
complete — so ``supports_detail`` is False and the engine marks rows as
detail-synced at upsert time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from google_health_client import DEFAULT_API_BASE, GoogleHealthClient
from models import Activity
from sources.base import RateLimitSnapshot, prefixed_id
from sources.mapping import (
    as_float,
    clip_text,
    parse_dt,
    parse_duration_seconds,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "google_health"
ID_PREFIX = "ghealth_"

CALLBACK_PATH = "/auth/google/callback"
CONNECT_PATH = "/auth/google"
DISCONNECT_PATH = "/auth/google/disconnect"

#: Bike-like Google Health sessions are usually a duplicate of a far richer
#: recording from a head unit. They are still stored; precedence decides which
#: row wins.
BIKE_EXERCISE_TYPES = {
    "BIKING",
    "BIKING_STATIONARY",
    "CYCLING",
    "ROAD_BIKING",
    "MOUNTAIN_BIKING",
    "SPINNING",
    "HAND_CYCLING",
}


def activity_id(raw: object) -> str:
    """Prefixed local id for a Google Health exercise point."""
    return prefixed_id(ID_PREFIX, raw)


def exercise_payload(point: dict) -> dict:
    """Unwrap the exercise object from the several shapes the API returns."""
    if isinstance(point.get("exercise"), dict):
        return point["exercise"]
    data = point.get("data")
    if isinstance(data, dict) and isinstance(data.get("exercise"), dict):
        return data["exercise"]
    return point


def sport_type(exercise_type: object) -> str:
    """Turn a ``WEIGHTLIFTING``-style enum into ``Weightlifting``."""
    if not exercise_type:
        return "Workout"
    words = str(exercise_type).replace("-", "_").split("_")
    pretty = "".join(
        word.capitalize() if index == 0 else word.lower()
        for index, word in enumerate(words)
    )
    return pretty or "Workout"


def map_row(payload: dict, athlete_id: int) -> dict | None:
    """Map a Google Health exercise dataPoint onto ``activities`` columns."""
    resource_name = payload.get("name") or ""
    exercise = exercise_payload(payload)
    if not isinstance(exercise, dict):
        exercise = {}

    exercise_type = (
        exercise.get("exerciseType") or exercise.get("exercise_type") or ""
    )

    interval = (
        exercise.get("interval")
        or payload.get("interval")
        or payload.get("timeInterval")
        or {}
    )
    start = parse_dt(
        interval.get("startTime")
        or interval.get("start_time")
        or payload.get("startTime")
    )
    end = parse_dt(
        interval.get("endTime")
        or interval.get("end_time")
        or payload.get("endTime")
    )
    if start is None:
        logger.warning(
            "Google Health exercise %s has no start time; skipping", resource_name
        )
        return None

    active = parse_duration_seconds(
        exercise.get("activeDuration") or exercise.get("active_duration")
    )
    if active <= 0 and end is not None:
        active = max(0, int((end - start).total_seconds()))
    elapsed = active
    if end is not None:
        elapsed = max(active, int((end - start).total_seconds()))

    summary = (
        exercise.get("metricsSummary") or exercise.get("metrics_summary") or {}
    )
    avg_hr = summary.get("averageHeartRate") or summary.get("average_heart_rate")
    max_hr = summary.get("maxHeartRate") or summary.get("max_heart_rate")
    calories = summary.get("calories") or summary.get("energyBurned")
    if isinstance(calories, dict):
        calories = calories.get("value") or calories.get("kcal")
    distance = summary.get("distanceMeters") or summary.get("distance") or 0.0

    display = exercise.get("displayName") or exercise.get("display_name") or ""
    raw_id = resource_name or payload.get("id") or start.isoformat()

    updated = parse_dt(
        payload.get("updateTime")
        or payload.get("update_time")
        or exercise.get("updateTime")
    )

    return {
        "id": activity_id(raw_id),
        "source": SOURCE_NAME,
        "athlete_id": athlete_id,
        "name": display or sport_type(exercise_type),
        "description": exercise.get("notes"),
        "private_note": None,
        "sport_type": sport_type(exercise_type),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": active or elapsed,
        "distance": as_float(distance) or 0.0,
        "average_heartrate": as_float(avg_hr),
        "max_heartrate": as_float(max_hr),
        "has_heartrate": avg_hr is not None or max_hr is not None,
        "average_watts": None,
        "max_watts": None,
        "average_speed": None,
        "total_elevation_gain": None,
        "kilojoules": None,
        "calories": as_float(calories),
        "device_watts": False,
        "device_name": clip_text("Google Health", 255),
        "gear_id": None,
        "gear_name": None,
        "weighted_average_watts": None,
        "suffer_score": None,
        "training_load": None,
        "provider_updated_at": updated,
    }


def hr_points_to_streams(points: list[dict], start: datetime) -> dict[str, list]:
    """Convert Google Health heart-rate dataPoints to time/heartrate streams."""
    samples: list[tuple[int, float]] = []
    for point in points:
        heart_rate = point.get("heartRate") or point.get("heart_rate") or {}
        bpm = None
        if isinstance(heart_rate, dict):
            bpm = heart_rate.get("beatsPerMinute") or heart_rate.get(
                "beats_per_minute"
            )
        if bpm is None and "value" in point:
            bpm = point["value"]
        timestamp = parse_dt(
            (point.get("timeInterval") or point.get("interval") or {}).get("startTime")
            or point.get("startTime")
            or point.get("instantaneousTime")
        )
        if bpm is None or timestamp is None:
            continue
        samples.append((max(0, int((timestamp - start).total_seconds())), float(bpm)))

    samples.sort(key=lambda sample: sample[0])
    if not samples:
        return {}
    return {
        "time": [sample[0] for sample in samples],
        "heartrate": [sample[1] for sample in samples],
    }


class GoogleHealthSource:
    """Google Health, authenticated with OAuth."""

    name = SOURCE_NAME
    id_prefix = ID_PREFIX
    # The list payload already contains everything we store.
    supports_detail = False
    supports_streams = True
    # Google Health publishes no per-activity update marker we can trust and
    # re-listing is the same cost as the incremental pass, so reconciliation is
    # left off until the list-call cost is measured against a real account.
    supports_reconcile = False
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
        self._client: GoogleHealthClient | None = None

    @property
    def redirect_uri(self) -> str:
        return f"{self._app_base_url}{CALLBACK_PATH}"

    def _build_client(self, session) -> GoogleHealthClient:
        return GoogleHealthClient(
            session,
            client_id=self._client_id,
            client_secret=self._client_secret,
            api_base=self._api_base,
            redirect_uri=self.redirect_uri,
        )

    @property
    def client(self) -> GoogleHealthClient:
        """Long-lived client for sync work.

        Deliberately uses its own session: an OAuth token refresh commits, and
        it must not drag half-finished activity writes along with it.
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
        """Google Health exposes no cheap athlete endpoint, so there is no id.

        Connectivity is checked by :meth:`is_connected` before syncing; a bad or
        revoked token surfaces from the first data call.
        """
        return None

    def register_auth_routes(self, app: FastAPI) -> None:
        @app.get(CONNECT_PATH)
        async def google_connect():
            if not self.is_configured():
                return RedirectResponse(
                    url="/?error=google_health_not_configured", status_code=303
                )
            async with self._session_factory() as session:
                url = self._build_client(session).authorization_url()
            return RedirectResponse(url=url, status_code=307)

        @app.get(CALLBACK_PATH)
        async def google_callback(code: str | None = None, error: str | None = None):
            if error or not code:
                return RedirectResponse(
                    url=f"/?error={error or 'missing_code'}", status_code=303
                )
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    payload = await client.exchange_code(code)
                    await client.store_tokens(payload)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    logger.error("Google Health OAuth exchange failed: %s", exc)
                    return RedirectResponse(
                        url="/?error=google_oauth_failed", status_code=303
                    )
                finally:
                    await client.close()
            return RedirectResponse(url="/?connected=google_health", status_code=303)

        @app.post(DISCONNECT_PATH)
        async def google_disconnect():
            async with self._session_factory() as session:
                client = self._build_client(session)
                try:
                    await client.disconnect()
                finally:
                    await client.close()
            return RedirectResponse(url="/", status_code=303)

    # ── Data ────────────────────────────────────────────────────────

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        return await self.client.list_exercises(start, end)

    async def fetch_detail(self, activity_external_id: str) -> dict | None:
        """Never called: ``supports_detail`` is False."""
        raise NotImplementedError(
            "Google Health has no separate detail endpoint"
        )

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        start = activity.start_date
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(seconds=max(activity.elapsed_time or 0, 60))
        points = await self.client.list_heart_rate(start, end)
        return hr_points_to_streams(points, start)

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        return map_row(payload, athlete_id)

    # ── Bookkeeping ─────────────────────────────────────────────────

    def rate_limits(self) -> RateLimitSnapshot:
        """Google publishes no per-response quota headers we can read."""
        return RateLimitSnapshot(known=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None:
            await self._session.close()
            self._session = None
