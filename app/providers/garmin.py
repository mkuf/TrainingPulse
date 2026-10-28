"""Garmin Connect provider: activities and weigh-ins via the Personal API.

Authenticated with a Personal API key the user creates in Garmin Connect, so
there is no OAuth flow and no redirect URI. Activities follow the Hammerhead
pattern: the list JSON gives provisional values, and ``fetch_detail``
downloads the original ``.fit`` file once, stores it permanently, and lets
:mod:`fitfiles` derive HR, power, elevation, and streams from it.

Payload field names below match what Garmin Connect's web endpoints return;
Garmin publishes no response schema for the Personal API, so check them
against a real account before relying on them.

Garmin also exposes sleep, HRV, stress, Body Battery, resting heart rate, and
SpO2. None has a feed yet, because TrainingPulse has no tables for them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import fitfiles
from catalog import AuthMethod, DataKind
from models import Activity
from providers.base import (
    RateLimitSnapshot,
    external_id,
    granted_if_connected,
    prefixed_id,
)
from providers.feeds import BodyRecord, Feed
from providers.garmin_client import (
    DEFAULT_API_BASE,
    GarminAuthError,
    GarminClient,
    extract_fit,
)
from providers.mapping import as_float, as_int, clip_text, parse_dt

logger = logging.getLogger(__name__)

SOURCE_NAME = "garmin"
ID_PREFIX = "garmin_"

#: Garmin ``activityType.typeKey`` mapped onto the Strava-flavoured vocabulary
#: ``metrics.SPORT_TRIMP_PER_MIN`` understands.
_TYPE_KEY_SPORT_MAP = {
    "running": "Run",
    "street_running": "Run",
    "track_running": "Run",
    "treadmill_running": "Run",
    "trail_running": "Trail Run",
    "virtual_run": "VirtualRun",
    "cycling": "Ride",
    "road_biking": "Ride",
    "e_bike_fitness": "Ride",
    "indoor_cycling": "VirtualRide",
    "virtual_ride": "VirtualRide",
    "mountain_biking": "MountainBikeRide",
    "gravel_cycling": "GravelRide",
    "lap_swimming": "Swim",
    "open_water_swimming": "Swim",
    "walking": "Walk",
    "hiking": "Hike",
    "cross_country_skiing_ws": "NordicSki",
    "resort_skiing_snowboarding_ws": "AlpineSki",
    "strength_training": "WeightTraining",
    "hiit": "Workout",
    "cardio": "Workout",
    "yoga": "Yoga",
    "rowing": "Rowing",
    "indoor_rowing": "Rowing",
    "kayaking_v2": "Kayaking",
}

#: The activity search filters by local calendar date, so pad the UTC window
#: by a day on each side and drop anything outside it client-side.
_LIST_PADDING = timedelta(days=1)

_PER_PAGE = 100

#: Bounds the fetch_detail → fetch_streams handoff cache; see Hammerhead.
_ANALYSIS_CACHE_MAX = 32


def activity_id(raw: object) -> str:
    """Prefixed local id for a Garmin activity."""
    return prefixed_id(ID_PREFIX, raw)


def sport_type(type_key: object) -> str:
    return _TYPE_KEY_SPORT_MAP.get(str(type_key or ""), "")


def flatten_detail(detail: dict) -> dict:
    """Reshape an ``activity-service`` detail payload into the search shape.

    The detail endpoint nests its numbers under ``summaryDTO`` and the sport
    under ``activityTypeDTO``; the search endpoint has them at the top level.
    Flattening keeps :func:`map_row` to one shape.
    """
    summary = detail.get("summaryDTO") or {}
    metadata = detail.get("metadataDTO") or {}
    return {
        "activityId": detail.get("activityId"),
        "activityName": detail.get("activityName"),
        "description": detail.get("description"),
        "activityType": detail.get("activityTypeDTO") or {},
        "startTimeGMT": summary.get("startTimeGMT"),
        "distance": summary.get("distance"),
        "duration": summary.get("duration"),
        "elapsedDuration": summary.get("elapsedDuration"),
        "movingDuration": summary.get("movingDuration"),
        "elevationGain": summary.get("elevationGain"),
        "averageSpeed": summary.get("averageSpeed"),
        "averageHR": summary.get("averageHR"),
        "maxHR": summary.get("maxHR"),
        "avgPower": summary.get("averagePower"),
        "maxPower": summary.get("maxPower"),
        "normPower": summary.get("normalizedPower"),
        "calories": summary.get("calories"),
        "lastUpdateDate": metadata.get("lastUpdateDate"),
    }


def map_row(payload: dict, athlete_id: int) -> dict | None:
    """Map a Garmin activity payload onto ``activities`` columns.

    ``payload`` is a search result, or a flattened detail payload with a
    private ``_fit_summary`` added by :meth:`GarminSource.fetch_detail`.

    Every field reconcile compares (``ingest.activities._DIFF_FIELDS``) comes
    from Garmin's JSON only, which is identical in the search and detail
    payloads, so re-listing flags an activity only when it was edited
    upstream. The FIT summary, which differs from Garmin's numbers by a
    second or a fraction of a beat, only fills fields the JSON lacks.
    """
    raw_id = payload.get("activityId")
    if raw_id is None:
        logger.warning("Garmin activity payload has no activityId; skipping")
        return None

    fit = payload.get("_fit_summary") or {}

    # ``startTimeGMT`` is UTC without an offset, which ``parse_dt`` assumes.
    start = parse_dt(payload.get("startTimeGMT")) or parse_dt(fit.get("start_date"))
    if start is None:
        logger.warning("Garmin activity %s has no start time; skipping", raw_id)
        return None

    elapsed = as_int(payload.get("elapsedDuration") or payload.get("duration"))
    # Garmin reports a moving time of 0 for stationary sports (strength, yoga);
    # use the elapsed time so TRIMP and calorie estimates still see the session.
    moving = as_int(payload.get("movingDuration")) or elapsed

    def pick(json_key: str, fit_key: str) -> float | None:
        value = as_float(payload.get(json_key))
        return value if value is not None else as_float(fit.get(fit_key))

    average_hr = as_float(payload.get("averageHR"))
    max_hr = as_float(payload.get("maxHR"))
    average_watts = as_float(payload.get("avgPower"))
    device_watts = average_watts is not None
    # The search payload carries float32 noise (30394.099609375 vs 30394.1).
    distance = round(as_float(payload.get("distance")) or 0.0, 1)
    kilojoules = (
        average_watts * moving / 1000.0
        if device_watts and average_watts and moving
        else None
    )

    activity_type = payload.get("activityType") or {}

    return {
        "id": activity_id(raw_id),
        "source": SOURCE_NAME,
        "athlete_id": athlete_id,
        "name": clip_text(payload.get("activityName"), 500) or "",
        "description": clip_text(payload.get("description"), 10000),
        "private_note": None,
        "sport_type": sport_type(activity_type.get("typeKey")),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": moving,
        "distance": distance,
        "average_heartrate": average_hr,
        "max_heartrate": max_hr,
        "has_heartrate": average_hr is not None,
        "average_watts": average_watts,
        "max_watts": as_float(payload.get("maxPower")),
        "average_speed": pick("averageSpeed", "average_speed"),
        "total_elevation_gain": pick("elevationGain", "total_elevation_gain"),
        "kilojoules": kilojoules,
        "calories": pick("calories", "calories"),
        "device_watts": device_watts,
        "device_name": clip_text(fit.get("device_name"), 255),
        "gear_id": None,
        "gear_name": None,
        "weighted_average_watts": pick("normPower", "weighted_average_watts"),
        "suffer_score": None,
        "training_load": None,
        "provider_updated_at": parse_dt(payload.get("lastUpdateDate")),
    }


def map_weight_metric(metric: dict) -> BodyRecord | None:
    """Map one ``allWeightMetrics`` entry onto a body record.

    Garmin reports weight and muscle mass in grams. ``timestampGMT`` is a true
    UTC epoch in milliseconds; ``date`` is local wall-clock time and is only a
    fallback.
    """
    weight_g = as_float(metric.get("weight"))
    sample_id = metric.get("samplePk")
    measured_ms = as_float(metric.get("timestampGMT")) or as_float(metric.get("date"))
    if not weight_g or sample_id is None or measured_ms is None:
        return None
    muscle_g = as_float(metric.get("muscleMass"))
    return BodyRecord(
        external_id=str(sample_id),
        measured_at=datetime.fromtimestamp(measured_ms / 1000.0, tz=timezone.utc),
        weight_kg=round(weight_g / 1000.0, 3),
        fat_pct=as_float(metric.get("bodyFat")),
        muscle_mass_kg=round(muscle_g / 1000.0, 3) if muscle_g else None,
        device=clip_text(metric.get("sourceType"), 128),
    )


class GarminBodyFeed:
    """Garmin weigh-ins (Index scales or manual entries) as a ``BodyFeed``."""

    kind = DataKind.BODY

    def __init__(self, provider: "GarminSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        body = await self._provider.client.get_weight_measurements(
            (start - _LIST_PADDING).date(), (end + _LIST_PADDING).date()
        )
        metrics: list[dict] = []
        for summary in body.get("dailyWeightSummaries") or []:
            metrics.extend(summary.get("allWeightMetrics") or [])
        lower, upper = start.timestamp() * 1000, end.timestamp() * 1000
        return [
            metric
            for metric in metrics
            if lower <= (as_float(metric.get("timestampGMT")) or -1) < upper
        ]

    def map_record(self, payload: dict) -> BodyRecord | None:
        return map_weight_metric(payload)


class GarminSource:
    """Garmin Connect, authenticated with a Personal API key.

    The provider is its own activities feed, plus a separate body feed.
    Reconcile is on: one search request covers 100 activities, and
    :func:`map_row` keeps list and detail rows comparable, so only activities
    edited in Garmin Connect (sport, name, trimmed) are re-detailed. Their FIT
    file is already stored, so a re-detail costs one JSON request.
    """

    name = SOURCE_NAME
    id_prefix = ID_PREFIX
    supports_detail = True
    supports_streams = True
    supports_reconcile = True
    auth = AuthMethod.API_KEY
    kind = DataKind.ACTIVITIES
    connect_path = ""
    disconnect_path = ""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = DEFAULT_API_BASE,
        session_factory: async_sessionmaker | None = None,
    ):
        self._api_key = (api_key or "").strip()
        self._api_base = api_base
        # Needed only for permanent FIT file storage.
        self._session_factory = session_factory
        self._client: GarminClient | None = None
        self._analysis_cache: "OrderedDict[str, fitfiles.FitAnalysis]" = OrderedDict()
        self._feeds: dict[DataKind, Feed] = {
            DataKind.ACTIVITIES: self,
            DataKind.BODY: GarminBodyFeed(self),
        }

    @property
    def client(self) -> GarminClient:
        if self._client is None:
            self._client = GarminClient(self._api_key, api_base=self._api_base)
        return self._client

    # ── Identity / auth ─────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def is_connected(self) -> bool:
        return self.is_configured()

    async def granted_kinds(self) -> frozenset[DataKind]:
        return await granted_if_connected(self)

    async def verify_credentials(self) -> int | None:
        """Exchange the key for a token and make one cheap call.

        The Personal API returns no numeric user id worth storing.
        """
        today = datetime.now(timezone.utc).date()
        try:
            await self.client.search_activities(
                start=0, limit=1, start_date=today, end_date=today
            )
        except GarminAuthError as exc:
            raise ValueError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError(
                    "Garmin rejected the bearer token "
                    f"(HTTP {exc.response.status_code}). Check providers.garmin.api_key."
                ) from exc
            raise
        return None

    def register_auth_routes(self, app: FastAPI) -> None:
        """No-op: Garmin uses a configured Personal API key, not OAuth."""

    # ── Activities ──────────────────────────────────────────────────

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        start_date = (start - _LIST_PADDING).astimezone(timezone.utc).date()
        end_date = (end + _LIST_PADDING).astimezone(timezone.utc).date()
        out: list[dict] = []
        offset = 0
        while True:
            batch = await self.client.search_activities(
                start=offset, limit=_PER_PAGE, start_date=start_date, end_date=end_date
            )
            out.extend(batch)
            if len(batch) < _PER_PAGE:
                break
            offset += _PER_PAGE
        return [
            item for item in out if _in_window(item.get("startTimeGMT"), start, end)
        ]

    async def fetch_detail(self, activity_external_id: str) -> dict | None:
        """Fetch detail JSON and merge in the FIT-derived summary."""
        try:
            payload = flatten_detail(
                await self.client.get_activity(activity_external_id)
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return None
            raise

        local_id = activity_id(activity_external_id)
        payload["_fit_summary"] = None
        try:
            raw = await self._ensure_fit_bytes(local_id, activity_external_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 410):
                raise
            logger.warning(
                "Garmin activity %s has no original file", activity_external_id
            )
            return payload
        except ValueError as exc:
            # Uploaded as GPX/TCX, or not a ZIP: keep the JSON values.
            logger.warning("Garmin activity %s: %s", activity_external_id, exc)
            return payload
        try:
            analysis = await _analyze(raw)
        except fitfiles.FitParseError as exc:
            logger.warning(
                "Could not parse FIT file for Garmin activity %s: %s",
                activity_external_id,
                exc,
            )
            return payload
        payload["_fit_summary"] = asdict(analysis.summary)
        self._cache_analysis(local_id, analysis)
        return payload

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        cached = self._analysis_cache.pop(activity.id, None)
        if cached is not None:
            return cached.streams
        try:
            raw = await self._ensure_fit_bytes(
                activity.id, external_id(ID_PREFIX, activity.id)
            )
            analysis = await _analyze(raw)
        except (ValueError, fitfiles.FitParseError) as exc:
            logger.warning("No streams for Garmin activity %s: %s", activity.id, exc)
            return {}
        return analysis.streams

    def _cache_analysis(self, local_id: str, analysis: "fitfiles.FitAnalysis") -> None:
        self._analysis_cache[local_id] = analysis
        self._analysis_cache.move_to_end(local_id)
        while len(self._analysis_cache) > _ANALYSIS_CACHE_MAX:
            self._analysis_cache.popitem(last=False)

    async def _ensure_fit_bytes(self, local_id: str, provider_id: str) -> bytes:
        """Read the permanently stored FIT file, or download and store it once."""
        async with self._session_factory() as session:
            cached = await fitfiles.get_fit_bytes(session, local_id)
        if cached is not None:
            return cached

        raw = extract_fit(await self.client.download_activity(provider_id))
        async with self._session_factory() as session:
            await fitfiles.save_fit_bytes(
                session, activity_id=local_id, source=SOURCE_NAME, raw_data=raw
            )
        return raw

    def feeds(self) -> dict[DataKind, Feed]:
        return dict(self._feeds)

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        return map_row(payload, athlete_id)

    # ── Bookkeeping ─────────────────────────────────────────────────

    def rate_limits(self) -> RateLimitSnapshot:
        # No documented quota or quota headers.
        return RateLimitSnapshot(known=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


def _in_window(raw_start: object, start: datetime, end: datetime) -> bool:
    parsed = parse_dt(raw_start)
    return parsed is not None and start <= parsed < end


async def _analyze(raw: bytes) -> "fitfiles.FitAnalysis":
    """Parse FIT bytes off the event loop (see ``hammerhead._analyze``)."""
    return await asyncio.to_thread(fitfiles.analyze_fit_file, raw)
