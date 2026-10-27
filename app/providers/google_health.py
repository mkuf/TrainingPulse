"""Google Health activity source.

Google Health has no per-activity detail endpoint — the list payload is already
complete — so ``supports_detail`` is False and the engine marks rows as
detail-synced at upsert time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from catalog import AuthMethod, DataKind
from google_health_client import DEFAULT_API_BASE, GoogleHealthClient
from models import Activity
from providers.base import (
    OAuthStateGuard,
    RateLimitSnapshot,
    granted_if_connected,
    prefixed_id,
)
from providers.mapping import (
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
    # MetricsSummary has no max heart rate; the engine derives it from the
    # heart-rate stream. Strava sessions synced via Health Connect arrive with
    # an empty summary.
    avg_hr = as_float(summary.get("averageHeartRateBeatsPerMinute"))
    calories = as_float(summary.get("caloriesKcal"))
    distance = _millimeters_to_meters(summary.get("distanceMillimeters"))
    speed = _millimeters_to_meters(summary.get("averageSpeedMillimetersPerSecond"))
    elevation = _millimeters_to_meters(summary.get("elevationGainMillimeters"))

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
        "name": clip_text(display, 500) or sport_type(exercise_type),
        "description": exercise.get("notes"),
        "private_note": None,
        "sport_type": sport_type(exercise_type),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": active or elapsed,
        "distance": distance or 0.0,
        "average_heartrate": avg_hr,
        "max_heartrate": None,
        "has_heartrate": avg_hr is not None,
        "average_watts": None,
        "max_watts": None,
        "average_speed": speed,
        "total_elevation_gain": elevation,
        "kilojoules": None,
        "calories": calories,
        "device_watts": False,
        "device_name": clip_text("Google Health", 255),
        "gear_id": None,
        "gear_name": None,
        "weighted_average_watts": None,
        "suffer_score": None,
        "training_load": None,
        "provider_updated_at": updated,
    }


def _millimeters_to_meters(raw: object) -> float | None:
    value = as_float(raw)
    return value / 1000.0 if value is not None else None


TCX_NS = {"tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}


def tcx_to_streams(tcx: str, start: datetime) -> dict[str, list]:
    """Turn TCX trackpoints into time/latlng/altitude/distance/heartrate streams.

    Only trackpoints with a position are kept, so every stream lines up with
    ``latlng``; a stream is dropped when none of its samples has a value.
    """
    try:
        root = ElementTree.fromstring(tcx)
    except ElementTree.ParseError as exc:
        logger.warning("Google Health TCX could not be parsed: %s", exc)
        return {}

    def number(trackpoint, path: str) -> float | None:
        return as_float(trackpoint.findtext(path, namespaces=TCX_NS))

    rows: list[tuple[int, list[float], float | None, float | None, float | None]] = []
    for trackpoint in root.iterfind(".//tcx:Trackpoint", TCX_NS):
        timestamp = parse_dt(trackpoint.findtext("tcx:Time", namespaces=TCX_NS))
        lat = number(trackpoint, "tcx:Position/tcx:LatitudeDegrees")
        lng = number(trackpoint, "tcx:Position/tcx:LongitudeDegrees")
        if timestamp is None or lat is None or lng is None:
            continue
        rows.append(
            (
                max(0, int((timestamp - start).total_seconds())),
                [lat, lng],
                number(trackpoint, "tcx:AltitudeMeters"),
                number(trackpoint, "tcx:DistanceMeters"),
                number(trackpoint, "tcx:HeartRateBpm/tcx:Value"),
            )
        )
    if not rows:
        return {}
    rows.sort(key=lambda row: row[0])
    streams: dict[str, list] = {
        "time": [row[0] for row in rows],
        "latlng": [row[1] for row in rows],
    }
    for index, key in ((2, "altitude"), (3, "distance"), (4, "heartrate")):
        values = [row[index] for row in rows]
        if any(value is not None for value in values):
            streams[key] = values
    if "distance" in streams:
        streams["velocity_smooth"] = _smoothed_speed(
            streams["time"], streams["distance"]
        )
    return streams


#: Trackpoints on each side of a sample that its speed is averaged over.
SPEED_WINDOW = 2


def _smoothed_speed(times: list[int], distances: list[float | None]) -> list[float]:
    """Speed in m/s from cumulative distance, over a few neighbouring points."""
    out: list[float] = []
    last = len(times) - 1
    for index in range(len(times)):
        lo = max(0, index - SPEED_WINDOW)
        hi = min(last, index + SPEED_WINDOW)
        dt = times[hi] - times[lo]
        if dt <= 0 or distances[hi] is None or distances[lo] is None:
            out.append(0.0)
            continue
        out.append(max(0.0, (distances[hi] - distances[lo]) / dt))
    return out


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
        sample_time = (
            heart_rate.get("sampleTime") if isinstance(heart_rate, dict) else None
        ) or {}
        timestamp = parse_dt(
            sample_time.get("physicalTime")
            or (point.get("timeInterval") or point.get("interval") or {}).get("startTime")
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


def _resample(heart_rate: dict[str, list], times: list[int]) -> list[float | None]:
    """Latest heart-rate sample at or before each route time (None before any)."""
    out: list[float | None] = []
    index = -1
    hr_times = heart_rate["time"]
    for moment in times:
        while index + 1 < len(hr_times) and hr_times[index + 1] <= moment:
            index += 1
        out.append(heart_rate["heartrate"][index] if index >= 0 else None)
    return out


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
        self._client: GoogleHealthClient | None = None
        self._oauth_state = OAuthStateGuard()

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
            state = self._oauth_state.issue()
            async with self._session_factory() as session:
                url = self._build_client(session).authorization_url(state=state)
            return RedirectResponse(url=url, status_code=307)

        @app.get(CALLBACK_PATH)
        async def google_callback(
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
                    "Google Health OAuth callback rejected: bad or missing state"
                )
                return RedirectResponse(
                    url="/?error=google_invalid_state", status_code=303
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
        tcx = await self.client.export_exercise_tcx(
            activity.id.removeprefix(ID_PREFIX)
        )
        route = tcx_to_streams(tcx, start) if tcx else {}
        if "heartrate" in route:
            return route
        heart_rate = hr_points_to_streams(
            await self.client.list_heart_rate(start, end), start
        )
        if not route:
            return heart_rate
        if heart_rate:
            route["heartrate"] = _resample(heart_rate, route["time"])
        return route

    def feeds(self) -> dict[DataKind, "GoogleHealthSource"]:
        # Single-kind provider: the source is its own activities feed.
        return {DataKind.ACTIVITIES: self}

    async def granted_kinds(self) -> frozenset[DataKind]:
        return await granted_if_connected(self)

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
