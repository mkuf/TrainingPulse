"""Activity sync engine — intervals.icu + Google Health into one store."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Iterable, TypeVar
import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import async_session
from google_health_client import GoogleHealthClient
from google_health_client import RateLimitExceeded as GoogleHealthRateLimitExceeded
from intervals_client import IntervalsClient
from intervals_client import RateLimitExceeded as IntervalsRateLimitExceeded
from metrics import (
    calculate_best_interval,
    calculate_power_curve,
    calculate_power_zones,
    calculate_trimp_from_streams,
    estimate_trimp_without_hr,
    get_hr_zone_boundaries,
    get_power_zone_boundaries,
    recalculate_daily_metrics,
)
from models import Activity, ActivityStream, AthleteSettings

logger = logging.getLogger(__name__)

RateLimitExceeded = IntervalsRateLimitExceeded  # default alias for shared helpers

SOURCE_ICU = "icu"
SOURCE_GOOGLE_HEALTH = "google_health"

# Bike-like exercise types (prefer intervals.icu when overlapping)
_BIKE_EXERCISE_TYPES = {
    "BIKING",
    "ROAD_BIKING",
    "MOUNTAIN_BIKING",
    "SPINNING",
    "CYCLING",
    "INDOOR_CYCLING",
    "HAND_CYCLING",
}


class SyncState:
    """Tracks the current state of the sync process."""

    def __init__(self):
        self.is_running: bool = False
        self.phase: str = "idle"
        self.provider: str = ""  # icu | google_health | ""
        self.total_activities: int = 0
        self.synced_activities: int = 0
        self.details_fetched: int = 0
        self.details_pending: int = 0
        self.streams_fetched: int = 0
        self.streams_pending: int = 0
        self.rate_limit_15min_usage: int = 0
        self.rate_limit_15min_limit: int = 2500
        self.rate_limit_daily_usage: int = 0
        self.rate_limit_daily_limit: int = 5000
        self.rate_limit_last_checked_at: datetime | None = None
        self.last_error: str | None = None
        self.last_sync: datetime | None = None


sync_state = SyncState()

T = TypeVar("T")


def _snapshot_rate_limits(client: IntervalsClient) -> None:
    sync_state.rate_limit_15min_usage = client._rate_limit_usage_15min
    sync_state.rate_limit_15min_limit = client._rate_limit_limit_15min
    sync_state.rate_limit_daily_usage = client._rate_limit_usage_daily
    sync_state.rate_limit_daily_limit = client._rate_limit_limit_daily
    sync_state.rate_limit_last_checked_at = datetime.now(timezone.utc)


async def _run_with_concurrency(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[None]],
    concurrency: int,
) -> bool:
    items = list(items)
    if not items:
        return True

    semaphore = asyncio.Semaphore(max(1, concurrency))
    rate_limited = asyncio.Event()

    async def _runner(item: T) -> None:
        if rate_limited.is_set():
            return
        async with semaphore:
            if rate_limited.is_set():
                return
            try:
                await worker(item)
            except (IntervalsRateLimitExceeded, GoogleHealthRateLimitExceeded):
                rate_limited.set()
                raise

    tasks = [asyncio.create_task(_runner(item)) for item in items]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    for r in results:
        if isinstance(r, (IntervalsRateLimitExceeded, GoogleHealthRateLimitExceeded)):
            return False
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            raise r
    return not rate_limited.is_set()


def _activity_needs_streams(activity: Activity) -> bool:
    return bool(activity.has_heartrate or activity.device_watts)


def _icu_activity_id(raw_id: object) -> str:
    s = str(raw_id).strip()
    if s.startswith("icu_"):
        return s
    if s.startswith("icu:"):
        return "icu_" + s[4:]
    return f"icu_{s}"


def _google_health_activity_id(raw_id: object) -> str:
    s = str(raw_id).strip()
    if s.startswith("ghealth_"):
        return s
    if s.startswith("fitbit_"):
        return "ghealth_" + s[len("fitbit_"):]
    if s.startswith("fitbit:"):
        s = s[7:]
    # Prefer last path segment of resource name
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    return f"ghealth_{s}"



def _parse_dt(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # date-only
        try:
            d = date.fromisoformat(s[:10])
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_duration_seconds(raw: object) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    if s.endswith("s") and s[:-1].replace(".", "", 1).isdigit():
        return int(float(s[:-1]))
    try:
        return int(float(s))
    except ValueError:
        return 0


def _normal_gear_id(raw: object) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() == "none":
        return None
    return s[:64]


# ── Athlete settings ────────────────────────────────────────────────


async def fetch_and_store_athlete_settings(
    session: AsyncSession,
    athlete_id: int,
    *,
    icu_client: IntervalsClient | None = None,
) -> AthleteSettings:
    """Resolve HR/FTP: env → intervals.icu sport-settings → fallbacks."""
    max_hr = settings.MAX_HR
    rest_hr = settings.REST_HR
    ftp = settings.FTP
    hr_zones = None
    provider_max_hr = None
    provider_ftp = None
    provider_rest_hr = None

    if icu_client is not None:
        try:
            sport_settings = await icu_client.get_sport_settings()
            _snapshot_rate_limits(icu_client)
            items = sport_settings if isinstance(sport_settings, list) else [sport_settings]
            # Prefer Ride, then first entry
            chosen = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                sport = (item.get("type") or item.get("sport") or "").lower()
                if sport in ("ride", "cycling", "bike"):
                    chosen = item
                    break
                if chosen is None:
                    chosen = item
            if isinstance(chosen, dict):
                provider_ftp = chosen.get("ftp") or chosen.get("icu_ftp")
                if chosen.get("max_hr"):
                    provider_max_hr = int(chosen["max_hr"])
                if chosen.get("resting_hr") or chosen.get("rest_hr"):
                    provider_rest_hr = int(
                        chosen.get("resting_hr") or chosen.get("rest_hr")
                    )
                zones = chosen.get("hr_zones")
                if isinstance(zones, list) and zones:
                    # Convert simple threshold list to Strava-like zone objects if needed
                    if zones and isinstance(zones[0], (int, float)):
                        hr_zones = []
                        for i, hi in enumerate(zones[1:], start=0):
                            lo = zones[i]
                            hr_zones.append({"min": lo, "max": hi})
                    else:
                        hr_zones = zones
            athlete = await icu_client.get_athlete()
            _snapshot_rate_limits(icu_client)
            if provider_ftp is None:
                provider_ftp = athlete.get("ftp") or athlete.get("icu_ftp")
            if provider_rest_hr is None and athlete.get("icu_resting_hr"):
                provider_rest_hr = int(athlete["icu_resting_hr"])
        except Exception as e:
            logger.warning("Could not fetch athlete data from intervals.icu: %s", e)

    is_hr_overridden = False
    if max_hr is not None:
        logger.info("Using MAX_HR from environment: %d", max_hr)
        is_hr_overridden = True
    else:
        max_hr = provider_max_hr or settings.FALLBACK_MAX_HR
        logger.info(
            "Using MAX_HR: %d (%s)",
            max_hr,
            "intervals.icu" if provider_max_hr else "Fallback",
        )

    if rest_hr is not None:
        logger.info("Using REST_HR from environment: %d", rest_hr)
        is_hr_overridden = True
    else:
        rest_hr = provider_rest_hr or settings.FALLBACK_REST_HR
        logger.info(
            "Using REST_HR: %d (%s)",
            rest_hr,
            "intervals.icu" if provider_rest_hr else "Fallback",
        )

    if ftp is not None:
        logger.info("Using FTP from environment: %d", ftp)
    else:
        ftp = int(provider_ftp) if provider_ftp else settings.FALLBACK_FTP
        logger.info(
            "Using FTP: %d (%s)",
            ftp,
            "intervals.icu" if provider_ftp else "Fallback",
        )

    if is_hr_overridden:
        hr_zones = None

    stmt = pg_insert(AthleteSettings).values(
        athlete_id=athlete_id,
        max_hr=max_hr,
        rest_hr=rest_hr,
        hr_zones=hr_zones,
        ftp=ftp,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["athlete_id"],
        set_={
            "max_hr": stmt.excluded.max_hr,
            "rest_hr": stmt.excluded.rest_hr,
            "hr_zones": stmt.excluded.hr_zones,
            "ftp": stmt.excluded.ftp,
        },
    )
    await session.execute(stmt)
    await session.commit()

    result = await session.execute(
        select(AthleteSettings).where(AthleteSettings.athlete_id == athlete_id)
    )
    return result.scalar_one()


async def resolve_athlete_id(
    session: AsyncSession, *, icu_client: IntervalsClient | None = None
) -> int:
    """Pick a stable local athlete_id (prefer intervals.icu id)."""
    existing = await session.execute(select(AthleteSettings).limit(1))
    row = existing.scalar_one_or_none()
    if row is not None:
        return row.athlete_id

    if icu_client is not None:
        athlete = await icu_client.get_athlete()
        _snapshot_rate_limits(icu_client)
        aid = athlete.get("id")
        if aid is not None:
            # intervals ids may be "i123" or numeric
            digits = re.sub(r"\D", "", str(aid))
            if digits:
                return int(digits)

    return 1


# ── Field mapping: intervals.icu ────────────────────────────────────


def _provider_updated_at_from_payload(a: dict) -> datetime | None:
    for key in ("updated", "updated_at", "icu_ignore_entry"):
        dt = _parse_dt(a.get(key))
        if dt:
            return dt
    return None


def _activity_row_from_intervals(a: dict, athlete_id: int) -> dict:
    raw_id = a.get("id")
    start = _parse_dt(a.get("start_date") or a.get("start_date_local"))
    if start is None:
        raise ValueError(f"Activity {raw_id} missing start_date")

    moving = int(a.get("moving_time") or a.get("icu_moving_time") or 0)
    elapsed = int(a.get("elapsed_time") or a.get("icu_elapsed_time") or moving)

    avg_hr = a.get("average_heartrate") or a.get("icu_average_heartrate")
    max_hr = a.get("max_heartrate") or a.get("icu_max_heartrate")
    has_hr = bool(a.get("has_heartrate") or avg_hr or max_hr)

    avg_watts = a.get("average_watts") or a.get("icu_average_watts")
    max_watts = a.get("max_watts") or a.get("icu_max_watts")
    wavg = a.get("weighted_average_watts") or a.get("icu_weighted_avg_watts")
    if wavg is not None:
        wavg = float(wavg)

    device_watts = bool(
        a.get("device_watts")
        or a.get("icu_pm_ftp")
        or avg_watts
        or wavg
    )

    gear = a.get("gear")
    gear_name = None
    gear_id = _normal_gear_id(a.get("gear_id") or a.get("icu_gear_id"))
    if isinstance(gear, dict):
        gear_name = (gear.get("name") or gear.get("nickname") or "").strip() or None
        if not gear_id:
            gear_id = _normal_gear_id(gear.get("id"))
    elif isinstance(gear, str) and gear.strip():
        gear_name = gear.strip()[:255]

    sport = a.get("type") or a.get("sport_type") or a.get("activity_type") or ""
    training_load = a.get("icu_training_load") or a.get("training_load")

    dev = a.get("device_name") or a.get("icu_device")
    if isinstance(dev, str):
        dev = dev[:255]
    else:
        dev = None

    return {
        "id": _icu_activity_id(raw_id),
        "source": SOURCE_ICU,
        "athlete_id": athlete_id,
        "name": a.get("name") or "",
        "description": a.get("description"),
        "private_note": a.get("private_note") or a.get("coach_notes"),
        "sport_type": str(sport),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": moving,
        "distance": float(a.get("distance") or 0.0),
        "average_heartrate": float(avg_hr) if avg_hr is not None else None,
        "max_heartrate": float(max_hr) if max_hr is not None else None,
        "has_heartrate": has_hr,
        "average_watts": float(avg_watts) if avg_watts is not None else None,
        "max_watts": float(max_watts) if max_watts is not None else None,
        "average_speed": a.get("average_speed"),
        "total_elevation_gain": a.get("total_elevation_gain"),
        "kilojoules": a.get("kilojoules"),
        "calories": a.get("calories") or a.get("icu_joules"),
        "device_watts": device_watts,
        "device_name": dev,
        "gear_id": gear_id,
        "gear_name": gear_name,
        "weighted_average_watts": wavg,
        "training_load": float(training_load) if training_load is not None else None,
        "provider_updated_at": _provider_updated_at_from_payload(a),
    }


def _normalize_icu_streams(raw: dict) -> dict:
    """Normalize intervals stream payload to {type: [samples...]}."""
    stream_data: dict = {}
    key_aliases = {
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

    # Format A: { "time": [...], "watts": [...] }
    # Format B: { "time": {"data": [...]}, ... }
    # Format C: list already converted by client to {type: {data|...}}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        canon = key_aliases.get(key, key)
        samples = None
        if isinstance(value, list):
            samples = value
        elif isinstance(value, dict):
            if "data" in value and isinstance(value["data"], list):
                samples = value["data"]
            elif "values" in value and isinstance(value["values"], list):
                samples = value["values"]
        if samples is not None:
            stream_data[canon] = samples
    return stream_data


# ── Field mapping: Google Health ───────────────────────────────────


def _exercise_payload(point: dict) -> dict:
    if isinstance(point.get("exercise"), dict):
        return point["exercise"]
    data = point.get("data")
    if isinstance(data, dict) and isinstance(data.get("exercise"), dict):
        return data["exercise"]
    return point


def _google_health_sport_type(exercise_type: object) -> str:
    if not exercise_type:
        return "Workout"
    s = str(exercise_type)
    # Title-case enum: WEIGHTLIFTING → Weightlifting
    return "".join(
        w.capitalize() if i == 0 else w.lower()
        for i, w in enumerate(s.replace("-", "_").split("_"))
    ) or "Workout"


def _activity_row_from_google_health(point: dict, athlete_id: int) -> dict | None:
    """Map a Google Health exercise dataPoint to an Activity row dict, or None to skip."""
    name = point.get("name") or ""
    exercise = _exercise_payload(point)
    if not isinstance(exercise, dict):
        exercise = {}

    exercise_type = exercise.get("exerciseType") or exercise.get("exercise_type") or ""
    if str(exercise_type).upper() in _BIKE_EXERCISE_TYPES:
        # Prefer intervals.icu for rides; skip bike-like Google Health sessions
        logger.info("Skipping Google Health bike-like exercise %s (%s)", name, exercise_type)
        return None

    interval = (
        exercise.get("interval")
        or point.get("interval")
        or point.get("timeInterval")
        or {}
    )
    start = _parse_dt(
        interval.get("startTime")
        or interval.get("start_time")
        or point.get("startTime")
    )
    end = _parse_dt(
        interval.get("endTime") or interval.get("end_time") or point.get("endTime")
    )
    if start is None:
        logger.warning("Google Health exercise missing start time: %s", name)
        return None

    active = _parse_duration_seconds(
        exercise.get("activeDuration") or exercise.get("active_duration")
    )
    if active <= 0 and end is not None:
        active = max(0, int((end - start).total_seconds()))
    elapsed = active
    if end is not None:
        elapsed = max(active, int((end - start).total_seconds()))

    summary = exercise.get("metricsSummary") or exercise.get("metrics_summary") or {}
    avg_hr = summary.get("averageHeartRate") or summary.get("average_heart_rate")
    max_hr = summary.get("maxHeartRate") or summary.get("max_heart_rate")
    calories = summary.get("calories") or summary.get("energyBurned")
    distance = summary.get("distanceMeters") or summary.get("distance") or 0.0
    if isinstance(calories, dict):
        calories = calories.get("value") or calories.get("kcal")

    display = exercise.get("displayName") or exercise.get("display_name") or ""
    raw_id = name or point.get("id") or f"{start.isoformat()}"

    updated = _parse_dt(
        point.get("updateTime")
        or point.get("update_time")
        or exercise.get("updateTime")
    )

    has_hr = avg_hr is not None or max_hr is not None

    return {
        "id": _google_health_activity_id(raw_id),
        "source": SOURCE_GOOGLE_HEALTH,
        "athlete_id": athlete_id,
        "name": display or _google_health_sport_type(exercise_type),
        "description": exercise.get("notes"),
        "private_note": None,
        "sport_type": _google_health_sport_type(exercise_type),
        "start_date": start,
        "elapsed_time": elapsed,
        "moving_time": active or elapsed,
        "distance": float(distance or 0.0),
        "average_heartrate": float(avg_hr) if avg_hr is not None else None,
        "max_heartrate": float(max_hr) if max_hr is not None else None,
        "has_heartrate": has_hr,
        "average_watts": None,
        "max_watts": None,
        "average_speed": None,
        "total_elevation_gain": None,
        "kilojoules": None,
        "calories": float(calories) if calories is not None else None,
        "device_watts": False,
        "device_name": "Google Health",
        "gear_id": None,
        "gear_name": None,
        "weighted_average_watts": None,
        "training_load": None,
        "provider_updated_at": updated,
        # Stash end time for stream fetch (not a DB column)
        "_end_date": end or (start + timedelta(seconds=elapsed or 0)),
        "_resource_name": name,
    }


def _hr_points_to_streams(
    points: list[dict], start: datetime
) -> dict:
    """Convert Google Health heart-rate dataPoints to time/heartrate streams."""
    samples: list[tuple[int, float]] = []
    for p in points:
        hr = p.get("heartRate") or p.get("heart_rate") or {}
        if isinstance(hr, dict):
            bpm = hr.get("beatsPerMinute") or hr.get("beats_per_minute")
        else:
            bpm = None
        if bpm is None and "value" in p:
            bpm = p["value"]
        ts = _parse_dt(
            (p.get("timeInterval") or p.get("interval") or {}).get("startTime")
            or p.get("startTime")
            or (p.get("instantaneousTime") if "instantaneousTime" in p else None)
        )
        if bpm is None or ts is None:
            continue
        offset = max(0, int((ts - start).total_seconds()))
        samples.append((offset, float(bpm)))

    samples.sort(key=lambda x: x[0])
    if not samples:
        return {}
    return {
        "time": [s[0] for s in samples],
        "heartrate": [s[1] for s in samples],
    }


# ── Upsert / refresh helpers ────────────────────────────────────────


def _activity_list_fields_differ(existing: Activity, row: dict) -> bool:
    def _eq(field: str) -> bool:
        a = getattr(existing, field)
        b = row.get(field)
        if field in (
            "distance",
            "average_heartrate",
            "max_heartrate",
            "average_watts",
            "max_watts",
            "kilojoules",
            "average_speed",
            "total_elevation_gain",
            "weighted_average_watts",
            "training_load",
        ):
            if a is None and b is None:
                return False
            if a is None or b is None:
                return True
            return float(a) != float(b)
        return a != b

    for field in (
        "name",
        "sport_type",
        "start_date",
        "elapsed_time",
        "moving_time",
        "distance",
        "average_heartrate",
        "max_heartrate",
        "has_heartrate",
        "average_watts",
        "max_watts",
        "gear_id",
        "kilojoules",
        "device_watts",
        "training_load",
    ):
        if _eq(field):
            return True
    return False


def _should_full_refresh_activity(existing: Activity, row: dict) -> bool:
    provider_at = row.get("provider_updated_at")
    if provider_at is not None and existing.provider_updated_at is not None:
        if provider_at > existing.provider_updated_at:
            return True
    return _activity_list_fields_differ(existing, row)


async def _mark_activity_for_detail_refresh(
    session: AsyncSession, activity_id: str
) -> None:
    await session.execute(
        update(Activity)
        .where(Activity.id == activity_id)
        .values(detail_synced=False)
    )
    await session.commit()


async def _mark_activity_for_full_refresh(
    session: AsyncSession, activity_id: str
) -> None:
    await session.execute(
        update(Activity)
        .where(Activity.id == activity_id)
        .values(
            detail_synced=False,
            synced_streams=False,
            trimp=None,
            hr_zone_seconds=None,
            power_zone_seconds=None,
            best_20min_power=None,
            power_curve=None,
        )
    )
    await session.commit()


_LIST_UPSERT_FIELDS = (
    "name",
    "source",
    "athlete_id",
    "sport_type",
    "start_date",
    "elapsed_time",
    "moving_time",
    "distance",
    "average_heartrate",
    "max_heartrate",
    "has_heartrate",
    "average_watts",
    "max_watts",
    "average_speed",
    "total_elevation_gain",
    "kilojoules",
    "device_watts",
    "gear_id",
    "weighted_average_watts",
    "training_load",
    "provider_updated_at",
)


async def _upsert_activity_rows(session: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    # Strip private keys
    clean = []
    for r in rows:
        clean.append({k: v for k, v in r.items() if not k.startswith("_")})

    stmt = pg_insert(Activity).values(clean)
    upsert_set = {f: getattr(stmt.excluded, f) for f in _LIST_UPSERT_FIELDS}
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=upsert_set)
    await session.execute(stmt)
    await session.commit()
    return len(clean)


# ── intervals.icu sync phases ───────────────────────────────────────


async def _icu_upsert_from_list(
    session: AsyncSession, activities: list[dict], athlete_id: int
) -> int:
    rows = []
    for a in activities:
        try:
            rows.append(_activity_row_from_intervals(a, athlete_id))
        except Exception as e:
            logger.warning("Skip intervals activity: %s", e)
    return await _upsert_activity_rows(session, rows)


def _date_windows(
    oldest: date, newest: date, window_days: int
) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cur = oldest
    while cur <= newest:
        end = min(cur + timedelta(days=window_days - 1), newest)
        windows.append((cur, end))
        cur = end + timedelta(days=1)
    return windows


async def _icu_backfill(
    client: IntervalsClient, session: AsyncSession, athlete_id: int
) -> None:
    sync_state.phase = "backfilling"
    sync_state.provider = SOURCE_ICU
    newest = date.today()
    oldest = newest - timedelta(days=365 * settings.SYNC_BACKFILL_YEARS)
    windows = _date_windows(oldest, newest, settings.SYNC_ICU_WINDOW_DAYS)
    total_stored = 0
    logger.info(
        "intervals.icu backfill: %s → %s (%d windows)",
        oldest,
        newest,
        len(windows),
    )
    for w_oldest, w_newest in windows:
        activities = await client.list_activities(
            w_oldest.isoformat(), w_newest.isoformat()
        )
        _snapshot_rate_limits(client)
        stored = await _icu_upsert_from_list(session, activities, athlete_id)
        total_stored += stored
        sync_state.total_activities = total_stored
        sync_state.synced_activities = total_stored
        logger.info(
            "Backfill window %s..%s: %d activities (total %d)",
            w_oldest,
            w_newest,
            len(activities),
            total_stored,
        )
    logger.info("intervals.icu backfill complete: %d activities", total_stored)


async def _icu_incremental(
    client: IntervalsClient, session: AsyncSession, athlete_id: int
) -> None:
    sync_state.phase = "incremental"
    sync_state.provider = SOURCE_ICU
    result = await session.execute(
        select(func.max(Activity.start_date)).where(
            Activity.athlete_id == athlete_id,
            Activity.source == SOURCE_ICU,
        )
    )
    latest = result.scalar_one_or_none()
    newest = date.today() + timedelta(days=1)
    if latest:
        oldest = (latest - timedelta(days=1)).date()
    else:
        oldest = newest - timedelta(days=settings.SYNC_RECONCILE_DAYS or 90)

    activities = await client.list_activities(
        oldest.isoformat(), newest.isoformat()
    )
    _snapshot_rate_limits(client)
    stored = await _icu_upsert_from_list(session, activities, athlete_id)
    logger.info("intervals.icu incremental: %d activities", stored)


async def _icu_reconcile(
    client: IntervalsClient, session: AsyncSession, athlete_id: int
) -> None:
    if settings.SYNC_RECONCILE_DAYS <= 0:
        return
    sync_state.phase = "reconciling"
    sync_state.provider = SOURCE_ICU
    newest = date.today() + timedelta(days=1)
    oldest = newest - timedelta(days=settings.SYNC_RECONCILE_DAYS)
    activities = await client.list_activities(
        oldest.isoformat(), newest.isoformat()
    )
    _snapshot_rate_limits(client)

    ids = [_icu_activity_id(a["id"]) for a in activities if a.get("id") is not None]
    existing_result = await session.execute(
        select(Activity).where(
            Activity.athlete_id == athlete_id,
            Activity.id.in_(ids) if ids else False,
        )
    )
    existing_by_id = {row.id: row for row in existing_result.scalars().all()}

    full_refresh: list[str] = []
    detail_refresh: list[str] = []
    for a in activities:
        try:
            row = _activity_row_from_intervals(a, athlete_id)
        except Exception:
            continue
        existing = existing_by_id.get(row["id"])
        if existing is None:
            continue
        if _should_full_refresh_activity(existing, row):
            full_refresh.append(row["id"])
        else:
            detail_refresh.append(row["id"])

    for aid in full_refresh:
        await _mark_activity_for_full_refresh(session, aid)
    for aid in detail_refresh:
        await _mark_activity_for_detail_refresh(session, aid)

    await _icu_upsert_from_list(session, activities, athlete_id)
    logger.info(
        "intervals.icu reconcile: %d listed, %d full, %d detail",
        len(activities),
        len(full_refresh),
        len(detail_refresh),
    )


async def _apply_one_icu_detail(
    session: AsyncSession, client: IntervalsClient, activity_id: str
) -> bool:
    try:
        detail = await client.get_activity(activity_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            await session.execute(
                update(Activity)
                .where(Activity.id == activity_id)
                .values(detail_synced=True)
            )
            await session.commit()
            _snapshot_rate_limits(client)
            return True
        raise
    except IntervalsRateLimitExceeded:
        _snapshot_rate_limits(client)
        raise
    except Exception as e:
        logger.warning("GET activity %s failed: %s", activity_id, e)
        return False

    _snapshot_rate_limits(client)
    act = await session.get(Activity, activity_id)
    if act is None:
        return False
    row = _activity_row_from_intervals(detail, act.athlete_id)
    row_id = row.pop("id")
    row.pop("source", None)
    row["detail_synced"] = True
    # Also merge detail-only fields
    for extra in ("description", "private_note", "calories", "device_name", "gear_name"):
        if extra in row:
            pass  # already in row
    await session.execute(update(Activity).where(Activity.id == row_id).values(**row))
    await session.commit()
    return True


async def _drain_icu_detail_queue(
    session: AsyncSession, client: IntervalsClient, athlete_id: int
) -> None:
    sync_state.phase = "detailing"
    sync_state.provider = SOURCE_ICU
    counts_row = (
        await session.execute(
            select(
                func.count(Activity.id),
                func.count(Activity.id).filter(Activity.detail_synced == False),  # noqa: E712
            ).where(Activity.athlete_id == athlete_id, Activity.source == SOURCE_ICU)
        )
    ).one()
    total_activities, pending_initial = counts_row
    sync_state.details_fetched = max(0, total_activities - pending_initial)
    sync_state.details_pending = pending_initial
    if pending_initial == 0:
        return

    concurrency = settings.SYNC_DETAIL_CONCURRENCY
    counter_lock = asyncio.Lock()
    no_progress_batches = 0

    while True:
        result = await session.execute(
            select(Activity.id)
            .where(
                Activity.athlete_id == athlete_id,
                Activity.source == SOURCE_ICU,
                Activity.detail_synced == False,  # noqa: E712
            )
            .order_by(Activity.start_date.desc())
            .limit(max(100, concurrency * 20))
        )
        ids = [row[0] for row in result.all()]
        if not ids:
            break

        batch_merged = 0

        async def _worker(activity_id: str) -> None:
            nonlocal batch_merged
            async with async_session() as worker_session:
                # IntervalsClient is HTTP-stateless aside from rate counters;
                # share the parent client for rate-limit tracking.
                merged = await _apply_one_icu_detail(
                    worker_session, client, activity_id
                )
            if merged:
                async with counter_lock:
                    batch_merged += 1
                    sync_state.details_fetched += 1
                    if sync_state.details_pending > 0:
                        sync_state.details_pending -= 1

        completed = await _run_with_concurrency(ids, _worker, concurrency)
        if not completed:
            sync_state.last_error = (
                "intervals.icu rate limit while merging details; will resume next sync."
            )
            break
        if batch_merged == 0:
            no_progress_batches += 1
            if no_progress_batches >= 2:
                break
        else:
            no_progress_batches = 0


async def _process_icu_activity_streams(
    client: IntervalsClient,
    session: AsyncSession,
    activity: Activity,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
) -> None:
    needs_hr_power = _activity_needs_streams(activity)
    try:
        raw = await client.get_activity_streams(activity.id)
        _snapshot_rate_limits(client)
        stream_data = _normalize_icu_streams(raw if isinstance(raw, dict) else {})
        stream_types = list(stream_data.keys())
        sample_count = max((len(v) for v in stream_data.values()), default=0)

        if stream_data:
            stream_stmt = pg_insert(ActivityStream).values(
                activity_id=activity.id,
                data=stream_data,
                stream_types=",".join(stream_types),
                sample_count=sample_count,
            )
            stream_stmt = stream_stmt.on_conflict_do_update(
                index_elements=["activity_id"],
                set_={
                    "data": stream_stmt.excluded.data,
                    "stream_types": stream_stmt.excluded.stream_types,
                    "sample_count": stream_stmt.excluded.sample_count,
                },
            )
            await session.execute(stream_stmt)

        if not needs_hr_power:
            trimp = estimate_trimp_without_hr(
                activity.sport_type, activity.moving_time
            )
            await session.execute(
                update(Activity)
                .where(Activity.id == activity.id)
                .values(
                    trimp=trimp,
                    hr_zone_seconds=None,
                    power_zone_seconds=None,
                    best_20min_power=None,
                    power_curve=None,
                    synced_streams=True,
                )
            )
            await session.commit()
            return

        time_stream = stream_data.get("time") or []
        hr_stream = stream_data.get("heartrate") or []
        power_stream = stream_data.get("watts") or []

        if activity.has_heartrate and time_stream and hr_stream and len(
            time_stream
        ) == len(hr_stream):
            trimp, hr_zones = calculate_trimp_from_streams(
                time_stream, hr_stream, hr_zone_boundaries
            )
        else:
            hr_zones = None
            trimp = estimate_trimp_without_hr(
                activity.sport_type, activity.moving_time
            )

        best_20min = None
        power_curve = None
        power_zones = None
        if power_stream:
            best_20min = calculate_best_interval(power_stream, 1200)
            power_curve = calculate_power_curve(power_stream)
            if time_stream:
                min_len = min(len(time_stream), len(power_stream))
                if min_len > 0:
                    power_zones = calculate_power_zones(
                        time_stream[:min_len],
                        power_stream[:min_len],
                        power_zone_boundaries,
                    )

        await session.execute(
            update(Activity)
            .where(Activity.id == activity.id)
            .values(
                trimp=trimp,
                hr_zone_seconds=hr_zones,
                power_zone_seconds=power_zones,
                best_20min_power=best_20min,
                power_curve=power_curve,
                synced_streams=True,
            )
        )
        await session.commit()
    except IntervalsRateLimitExceeded:
        _snapshot_rate_limits(client)
        raise
    except Exception as e:
        logger.warning("Failed streams for %s: %s", activity.id, e)
        trimp = estimate_trimp_without_hr(activity.sport_type, activity.moving_time)
        await session.execute(
            update(Activity)
            .where(Activity.id == activity.id)
            .values(trimp=trimp, synced_streams=True)
        )
        await session.commit()


async def _process_pending_icu_streams(
    client: IntervalsClient,
    session: AsyncSession,
    athlete_id: int,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
) -> None:
    sync_state.phase = "streaming"
    sync_state.provider = SOURCE_ICU
    result = await session.execute(
        select(Activity.id)
        .where(
            Activity.athlete_id == athlete_id,
            Activity.source == SOURCE_ICU,
            Activity.synced_streams == False,  # noqa: E712
        )
        .order_by(Activity.start_date)
    )
    pending_ids = [row[0] for row in result.all()]
    total_activities = (
        await session.execute(
            select(func.count(Activity.id)).where(
                Activity.athlete_id == athlete_id, Activity.source == SOURCE_ICU
            )
        )
    ).scalar_one()
    sync_state.streams_pending = len(pending_ids)
    sync_state.streams_fetched = max(0, total_activities - len(pending_ids))
    if not pending_ids:
        return

    concurrency = settings.SYNC_STREAMS_CONCURRENCY
    counter_lock = asyncio.Lock()

    async def _worker(activity_id: str) -> None:
        async with async_session() as worker_session:
            row = await worker_session.get(Activity, activity_id)
            if row is None:
                return
            await _process_icu_activity_streams(
                client,
                worker_session,
                row,
                hr_zone_boundaries,
                power_zone_boundaries,
            )
        async with counter_lock:
            sync_state.streams_fetched += 1
            if sync_state.streams_pending > 0:
                sync_state.streams_pending -= 1

    completed = await _run_with_concurrency(pending_ids, _worker, concurrency)
    if not completed:
        raise IntervalsRateLimitExceeded(reset_after=900)


# ── Google Health sync ─────────────────────────────────────────────


async def _google_health_overlaps_icu(
    session: AsyncSession, athlete_id: int, start: datetime, end: datetime
) -> bool:
    """True if an intervals.icu activity overlaps this window (skip Google Health duplicate)."""
    window_start = start - timedelta(minutes=15)
    window_end = end + timedelta(minutes=15)
    result = await session.execute(
        select(Activity).where(
            Activity.athlete_id == athlete_id,
            Activity.source == SOURCE_ICU,
            Activity.start_date >= window_start,
            Activity.start_date <= window_end,
        )
    )
    for act in result.scalars().all():
        act_end = act.start_date + timedelta(seconds=act.elapsed_time or 0)
        if act.start_date < end and act_end > start:
            return True
    return False


async def _google_health_sync(
    client: GoogleHealthClient,
    session: AsyncSession,
    athlete_id: int,
    *,
    force_resync: bool,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
) -> None:
    sync_state.phase = "incremental"
    sync_state.provider = SOURCE_GOOGLE_HEALTH

    newest = datetime.now(timezone.utc) + timedelta(days=1)
    if force_resync:
        oldest = newest - timedelta(days=365 * settings.SYNC_BACKFILL_YEARS)
        sync_state.phase = "backfilling"
    else:
        result = await session.execute(
            select(func.max(Activity.start_date)).where(
                Activity.athlete_id == athlete_id,
                Activity.source == SOURCE_GOOGLE_HEALTH,
            )
        )
        latest = result.scalar_one_or_none()
        if latest:
            oldest = latest - timedelta(days=2)
        else:
            oldest = newest - timedelta(days=max(settings.SYNC_RECONCILE_DAYS, 90))

    # Chunk by ~90 days to keep responses manageable
    cursor = oldest
    total = 0
    while cursor < newest:
        chunk_end = min(cursor + timedelta(days=settings.SYNC_ICU_WINDOW_DAYS), newest)
        try:
            points = await client.list_exercises(cursor, chunk_end)
        except GoogleHealthRateLimitExceeded:
            raise
        except Exception as e:
            logger.warning("Google Health list_exercises failed (%s..%s): %s", cursor, chunk_end, e)
            points = []

        rows: list[dict] = []
        for point in points:
            row = _activity_row_from_google_health(point, athlete_id)
            if row is None:
                continue
            end = row.get("_end_date") or (
                row["start_date"] + timedelta(seconds=row["elapsed_time"])
            )
            if await _google_health_overlaps_icu(
                session, athlete_id, row["start_date"], end
            ):
                logger.info(
                    "Skipping Google Health %s — overlaps intervals.icu activity", row["id"]
                )
                continue
            rows.append(row)

        # Mark new rows as needing streams; details come from list payload
        stored_rows = []
        for r in rows:
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            clean["detail_synced"] = True  # list/detail already in exercise payload
            stored_rows.append(clean)

        if stored_rows:
            # Upsert then ensure new ones get streams
            existing_ids = {
                row.id
                for row in (
                    await session.execute(
                        select(Activity).where(
                            Activity.id.in_([r["id"] for r in stored_rows])
                        )
                    )
                ).scalars().all()
            }
            await _upsert_activity_rows(session, stored_rows)
            for r in stored_rows:
                if r["id"] not in existing_ids:
                    await session.execute(
                        update(Activity)
                        .where(Activity.id == r["id"])
                        .values(synced_streams=False, detail_synced=True)
                    )
            await session.commit()
            total += len(stored_rows)

        # Process pending Google Health streams for this athlete
        await _process_pending_google_health_streams(
            client,
            session,
            athlete_id,
            hr_zone_boundaries,
            power_zone_boundaries,
        )

        sync_state.synced_activities = total
        sync_state.total_activities = total
        cursor = chunk_end

    logger.info("Google Health sync stored/updated ~%d activities", total)


async def _process_pending_google_health_streams(
    client: GoogleHealthClient,
    session: AsyncSession,
    athlete_id: int,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
) -> None:
    sync_state.phase = "streaming"
    sync_state.provider = SOURCE_GOOGLE_HEALTH
    result = await session.execute(
        select(Activity)
        .where(
            Activity.athlete_id == athlete_id,
            Activity.source == SOURCE_GOOGLE_HEALTH,
            Activity.synced_streams == False,  # noqa: E712
        )
        .order_by(Activity.start_date)
        .limit(50)
    )
    pending = list(result.scalars().all())
    for activity in pending:
        end = activity.start_date + timedelta(seconds=activity.elapsed_time or 0)
        try:
            hr_points = await client.list_heart_rate(activity.start_date, end)
            stream_data = _hr_points_to_streams(hr_points, activity.start_date)
            if stream_data:
                stream_stmt = pg_insert(ActivityStream).values(
                    activity_id=activity.id,
                    data=stream_data,
                    stream_types=",".join(stream_data.keys()),
                    sample_count=len(stream_data.get("time") or []),
                )
                stream_stmt = stream_stmt.on_conflict_do_update(
                    index_elements=["activity_id"],
                    set_={
                        "data": stream_stmt.excluded.data,
                        "stream_types": stream_stmt.excluded.stream_types,
                        "sample_count": stream_stmt.excluded.sample_count,
                    },
                )
                await session.execute(stream_stmt)
                activity.has_heartrate = True

            time_stream = stream_data.get("time") or []
            hr_stream = stream_data.get("heartrate") or []
            if time_stream and hr_stream and len(time_stream) == len(hr_stream):
                trimp, hr_zones = calculate_trimp_from_streams(
                    time_stream, hr_stream, hr_zone_boundaries
                )
            else:
                hr_zones = None
                trimp = estimate_trimp_without_hr(
                    activity.sport_type, activity.moving_time
                )

            await session.execute(
                update(Activity)
                .where(Activity.id == activity.id)
                .values(
                    trimp=trimp,
                    hr_zone_seconds=hr_zones,
                    has_heartrate=bool(hr_stream) or activity.has_heartrate,
                    synced_streams=True,
                )
            )
            await session.commit()
            sync_state.streams_fetched += 1
        except GoogleHealthRateLimitExceeded:
            raise
        except Exception as e:
            logger.warning("Google Health streams for %s failed: %s", activity.id, e)
            trimp = estimate_trimp_without_hr(
                activity.sport_type, activity.moving_time
            )
            await session.execute(
                update(Activity)
                .where(Activity.id == activity.id)
                .values(trimp=trimp, synced_streams=True)
            )
            await session.commit()


# ── Shared post-processing ──────────────────────────────────────────


async def _recalculate_estimated_ftp(session: AsyncSession, athlete_id: int) -> None:
    result = await session.execute(
        select(func.max(Activity.best_20min_power)).where(
            Activity.athlete_id == athlete_id
        )
    )
    best_20min = result.scalar_one_or_none()
    if best_20min:
        estimated_ftp = int(best_20min * 0.95)
        logger.info(
            "Recalculated estimated FTP: %d W (best 20min %.1f W)",
            estimated_ftp,
            best_20min,
        )
        await session.execute(
            update(AthleteSettings)
            .where(AthleteSettings.athlete_id == athlete_id)
            .values(estimated_ftp=estimated_ftp)
        )
        await session.commit()


async def _backfill_power_curves(session: AsyncSession, athlete_id: int) -> None:
    result = await session.execute(
        select(Activity, ActivityStream.data)
        .join(ActivityStream, Activity.id == ActivityStream.activity_id)
        .where(
            Activity.athlete_id == athlete_id,
            Activity.power_curve == None,  # noqa: E711
        )
    )
    to_process = result.all()
    if not to_process:
        return
    for activity, stream_data in to_process:
        power_stream = stream_data.get("watts")
        if power_stream:
            curve = calculate_power_curve(power_stream)
            if curve:
                await session.execute(
                    update(Activity)
                    .where(Activity.id == activity.id)
                    .values(power_curve=curve)
                )
    await session.commit()


async def force_full_resync(session: AsyncSession, athlete_id: int) -> None:
    logger.info("Resetting sync state for athlete %d", athlete_id)
    await session.execute(
        update(Activity)
        .where(Activity.athlete_id == athlete_id)
        .values(
            synced_streams=False,
            detail_synced=False,
            trimp=None,
            hr_zone_seconds=None,
            power_zone_seconds=None,
            best_20min_power=None,
            power_curve=None,
        )
    )
    await session.commit()


# ── Main entry ──────────────────────────────────────────────────────


async def run_sync(session: AsyncSession, force_resync: bool = False) -> None:
    """Sync all enabled providers, then recalculate daily metrics once."""
    if sync_state.is_running:
        logger.info("Sync already in progress, skipping")
        return

    sync_state.is_running = True
    sync_state.last_error = None
    sync_state.synced_activities = 0
    sync_state.total_activities = 0
    sync_state.provider = ""

    icu_client: IntervalsClient | None = None
    google_health_client: GoogleHealthClient | None = None

    try:
        icu_on = settings.icu_enabled()
        google_health_on = settings.google_health_enabled()

        if not icu_on and not google_health_on:
            sync_state.phase = "idle"
            sync_state.last_error = (
                "No activity sources configured. Set INTERVALS_ICU_API_KEY and/or "
                "connect Google Health OAuth."
            )
            return

        if icu_on:
            icu_client = IntervalsClient()
            # Fail fast with a clear message if the API key is wrong
            try:
                await icu_client.get_athlete()
                _snapshot_rate_limits(icu_client)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    sync_state.phase = "idle"
                    sync_state.last_error = (
                        "intervals.icu rejected the API key (HTTP "
                        f"{e.response.status_code}). Check INTERVALS_ICU_API_KEY."
                    )
                    logger.error(sync_state.last_error)
                    return
                raise
        if google_health_on:
            google_health_client = GoogleHealthClient(session)
            if not await google_health_client.is_connected():
                google_health_client = None
                if not icu_on:
                    sync_state.last_error = (
                        "Google Health is enabled but not connected. Authorize from the home page."
                    )
                    return

        athlete_id = await resolve_athlete_id(session, icu_client=icu_client)

        if force_resync:
            await force_full_resync(session, athlete_id)

        athlete_settings = await fetch_and_store_athlete_settings(
            session, athlete_id, icu_client=icu_client
        )
        hr_zone_boundaries = get_hr_zone_boundaries(
            athlete_settings.max_hr,
            athlete_settings.rest_hr,
            athlete_settings.hr_zones,
        )
        power_zone_boundaries = get_power_zone_boundaries(athlete_settings.ftp)

        # ── intervals.icu ──
        if icu_client is not None:
            count_result = await session.execute(
                select(func.count(Activity.id)).where(
                    Activity.athlete_id == athlete_id,
                    Activity.source == SOURCE_ICU,
                )
            )
            existing_icu = count_result.scalar_one()
            if existing_icu == 0 or force_resync:
                await _icu_backfill(icu_client, session, athlete_id)
            else:
                await _icu_incremental(icu_client, session, athlete_id)
                await _icu_reconcile(icu_client, session, athlete_id)

            await _drain_icu_detail_queue(session, icu_client, athlete_id)
            await _process_pending_icu_streams(
                icu_client,
                session,
                athlete_id,
                hr_zone_boundaries,
                power_zone_boundaries,
            )

        # ── Google Health ──
        if google_health_client is not None:
            await _google_health_sync(
                google_health_client,
                session,
                athlete_id,
                force_resync=force_resync,
                hr_zone_boundaries=hr_zone_boundaries,
                power_zone_boundaries=power_zone_boundaries,
            )

        await _recalculate_estimated_ftp(session, athlete_id)
        await _backfill_power_curves(session, athlete_id)

        sync_state.phase = "calculating"
        sync_state.provider = ""
        await recalculate_daily_metrics(session, athlete_id)

        sync_state.phase = "idle"
        sync_state.last_sync = datetime.now(timezone.utc)
        logger.info("Sync completed successfully")

    except (IntervalsRateLimitExceeded, GoogleHealthRateLimitExceeded) as e:
        sync_state.last_error = str(e)
        logger.warning("Sync paused due to rate limit: %s", e)
    except Exception as e:
        sync_state.last_error = str(e)
        logger.exception("Sync failed: %s", e)
    finally:
        sync_state.is_running = False
        sync_state.provider = ""
        if icu_client is not None:
            await icu_client.close()
        if google_health_client is not None:
            await google_health_client.close()
