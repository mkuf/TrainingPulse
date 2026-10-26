"""intervals.icu activity source."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker
from trainingpulse_common.catalog import AuthMethod, DataKind
from fastapi import FastAPI

from intervals_client import DEFAULT_API_BASE, IntervalsClient
from models import Activity
from providers.base import granted_if_connected
from sources.base import RateLimitSnapshot, external_id, prefixed_id
from sources.mapping import as_float, as_int, clip_text, normal_gear_id, parse_dt

logger = logging.getLogger(__name__)

SOURCE_NAME = "icu"
ID_PREFIX = "icu_"

_STREAM_ALIASES = {
    "heart_rate": "heartrate",
    "heartrate": "heartrate",
    "hr": "heartrate",
    "watts": "watts",
    "power": "watts",
    "velocity_smooth": "velocity_smooth",
    "speed": "velocity_smooth",
    "latlng": "latlng",
    "lat_lng": "latlng",
    "altitude": "altitude",
    "cadence": "cadence",
    "distance": "distance",
    "time": "time",
    "temp": "temp",
    "grade_smooth": "grade_smooth",
}


def activity_id(raw: object) -> str:
    """Prefixed local id for an intervals.icu activity."""
    return prefixed_id(ID_PREFIX, raw)


def provider_updated_at(payload: dict) -> datetime | None:
    for key in ("updated", "updated_at", "icu_ignore_entry"):
        parsed = parse_dt(payload.get(key))
        if parsed:
            return parsed
    return None


def map_row(payload: dict, athlete_id: int) -> dict | None:
    """Map an intervals.icu activity payload onto ``activities`` columns."""
    raw_id = payload.get("id")
    if raw_id is None:
        logger.warning("intervals.icu activity payload has no id; skipping")
        return None
    # ``start_date_local`` is local wall-clock time with no offset, not UTC;
    # ``parse_dt`` treats a naive timestamp as UTC, so falling back to it here
    # would silently shift the stored time. Only ``start_date`` (true UTC) is
    # usable without also knowing the athlete's UTC offset.
    start = parse_dt(payload.get("start_date"))
    if start is None:
        logger.warning("intervals.icu activity %s has no start_date; skipping", raw_id)
        return None

    moving = as_int(payload.get("moving_time") or payload.get("icu_moving_time"))
    elapsed = as_int(
        payload.get("elapsed_time") or payload.get("icu_elapsed_time"), moving
    )

    avg_hr = payload.get("average_heartrate") or payload.get("icu_average_heartrate")
    max_hr = payload.get("max_heartrate") or payload.get("icu_max_heartrate")
    has_hr = bool(payload.get("has_heartrate") or avg_hr or max_hr)

    avg_watts = payload.get("average_watts") or payload.get("icu_average_watts")
    max_watts = payload.get("max_watts") or payload.get("icu_max_watts")
    weighted = as_float(
        payload.get("weighted_average_watts") or payload.get("icu_weighted_avg_watts")
    )

    device_watts = bool(
        payload.get("device_watts")
        or payload.get("icu_pm_ftp")
        or avg_watts
        or weighted
    )

    gear = payload.get("gear")
    gear_name = None
    gear_id = normal_gear_id(payload.get("gear_id") or payload.get("icu_gear_id"))
    if isinstance(gear, dict):
        gear_name = clip_text(gear.get("name") or gear.get("nickname"), 255)
        if not gear_id:
            gear_id = normal_gear_id(gear.get("id"))
    elif isinstance(gear, str):
        gear_name = clip_text(gear, 255)

    sport = (
        payload.get("type")
        or payload.get("sport_type")
        or payload.get("activity_type")
        or ""
    )
    training_load = payload.get("icu_training_load") or payload.get("training_load")

    kilojoules = as_float(payload.get("kilojoules"))
    if kilojoules is None:
        joules = as_float(payload.get("icu_joules"))
        kilojoules = joules / 1000.0 if joules is not None else None

    return {
        "id": activity_id(raw_id),
        "source": SOURCE_NAME,
        "athlete_id": athlete_id,
        "name": clip_text(payload.get("name"), 500) or "",
        "description": payload.get("description"),
        "private_note": payload.get("private_note") or payload.get("coach_notes"),
        "sport_type": str(sport),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": moving,
        "distance": as_float(payload.get("distance")) or 0.0,
        "average_heartrate": as_float(avg_hr),
        "max_heartrate": as_float(max_hr),
        "has_heartrate": has_hr,
        "average_watts": as_float(avg_watts),
        "max_watts": as_float(max_watts),
        "average_speed": as_float(payload.get("average_speed")),
        "total_elevation_gain": as_float(payload.get("total_elevation_gain")),
        # ``icu_joules`` is the mechanical work in joules: kJ, never kcal.
        "kilojoules": kilojoules,
        "calories": as_float(payload.get("calories")),
        "device_watts": device_watts,
        "device_name": clip_text(
            payload.get("device_name") or payload.get("icu_device"), 255
        ),
        "gear_id": gear_id,
        "gear_name": gear_name,
        "weighted_average_watts": weighted,
        "suffer_score": None,
        "training_load": as_float(training_load),
        "provider_updated_at": provider_updated_at(payload),
    }


def normalize_streams(raw: dict) -> dict[str, list]:
    """Normalize an intervals.icu stream payload to ``{type: [samples]}``.

    Handles the three shapes the API and client can produce:
    ``{"time": [...]}``, ``{"time": {"data": [...]}}``, and
    ``{"time": {"values": [...]}}``.
    """
    streams: dict[str, list] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        canonical = _STREAM_ALIASES.get(key, key)
        samples = None
        if isinstance(value, list):
            samples = value
        elif isinstance(value, dict):
            if isinstance(value.get("data"), list):
                samples = value["data"]
            elif isinstance(value.get("values"), list):
                samples = value["values"]
        if samples is not None:
            streams[canonical] = samples
    return streams


class IntervalsSource:
    """intervals.icu, authenticated with a personal API key."""

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
        self._client: IntervalsClient | None = None
        # Unused: intervals.icu needs no OAuth and therefore no DB access.
        self._session_factory = session_factory

    # ── Identity / auth ─────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def is_connected(self) -> bool:
        return self.is_configured()

    @property
    def client(self) -> IntervalsClient:
        if self._client is None:
            self._client = IntervalsClient(self._api_key, api_base=self._api_base)
        return self._client

    async def verify_credentials(self) -> int | None:
        try:
            athlete = await self.client.get_athlete()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError(
                    "intervals.icu rejected the API key "
                    f"(HTTP {exc.response.status_code}). Check sources.icu.api_key."
                ) from exc
            raise
        raw = athlete.get("id")
        if raw is None:
            return None
        # intervals.icu ids look like "i123" or a bare number.
        digits = re.sub(r"\D", "", str(raw))
        return int(digits) if digits else None

    def register_auth_routes(self, app: FastAPI) -> None:
        """No-op: intervals.icu uses a configured API key, not OAuth."""

    # ── Data ────────────────────────────────────────────────────────

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        oldest = start.astimezone(timezone.utc).date().isoformat()
        newest = end.astimezone(timezone.utc).date().isoformat()
        return await self.client.list_activities(oldest, newest)

    async def fetch_detail(self, activity_external_id: str) -> dict | None:
        try:
            return await self.client.get_activity(activity_external_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return None
            raise

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        raw = await self.client.get_activity_streams(
            external_id(ID_PREFIX, activity.id)
        )
        return normalize_streams(raw)

    def feeds(self) -> dict[DataKind, "IntervalsSource"]:
        # Single-kind provider: the source is its own activities feed.
        return {DataKind.ACTIVITIES: self}

    async def granted_kinds(self) -> frozenset[DataKind]:
        return await granted_if_connected(self)

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        return map_row(payload, athlete_id)

    # ── Bookkeeping ─────────────────────────────────────────────────

    def rate_limits(self) -> RateLimitSnapshot:
        if self._client is None:
            return RateLimitSnapshot(known=False)
        client = self._client
        return RateLimitSnapshot(
            used_15min=client.rate_limit_usage_15min,
            limit_15min=client.rate_limit_limit_15min,
            used_daily=client.rate_limit_usage_daily,
            limit_daily=client.rate_limit_limit_daily,
            checked_at=datetime.now(timezone.utc),
            known=client.rate_limit_seen,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
