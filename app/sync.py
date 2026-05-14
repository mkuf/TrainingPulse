"""Activity sync engine — handles backfilling and incremental sync from Strava."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, TypeVar

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import async_session
from metrics import (
    calculate_trimp_from_streams,
    calculate_power_zones,
    calculate_best_interval,
    calculate_power_curve,
    estimate_trimp_without_hr,
    get_hr_zone_boundaries,
    get_power_zone_boundaries,
    recalculate_daily_metrics,
)
from models import Activity, ActivityStream, AthleteSettings, StravaToken
from strava_client import RateLimitExceeded, StravaClient

logger = logging.getLogger(__name__)


class SyncState:
    """Tracks the current state of the sync process.

    All counters are in-memory only; they reset on process restart but reflect
    DB truth on the next sync run.
    """

    def __init__(self):
        self.is_running: bool = False
        self.phase: str = "idle"  # idle, backfilling, detailing, streaming, incremental, calculating
        # List backfill progress (from GET /athlete/activities)
        self.total_activities: int = 0
        self.synced_activities: int = 0
        # Detail merge progress (from GET /activities/{id})
        self.details_fetched: int = 0
        self.details_pending: int = 0
        # Stream fetch progress (from GET /activities/{id}/streams)
        self.streams_fetched: int = 0
        self.streams_pending: int = 0
        # Strava rate-limit usage snapshot (updated after each API call)
        self.rate_limit_15min_usage: int = 0
        self.rate_limit_15min_limit: int = 100
        self.rate_limit_daily_usage: int = 0
        self.rate_limit_daily_limit: int = 1000
        self.last_error: str | None = None
        self.last_sync: datetime | None = None


# Global sync state (shared across the app)
sync_state = SyncState()


T = TypeVar("T")


def _snapshot_rate_limits(client: StravaClient) -> None:
    """Copy the client's last-seen rate-limit counters into sync_state."""
    sync_state.rate_limit_15min_usage = client._rate_limit_usage_15min
    sync_state.rate_limit_15min_limit = client._rate_limit_limit_15min
    sync_state.rate_limit_daily_usage = client._rate_limit_usage_daily
    sync_state.rate_limit_daily_limit = client._rate_limit_limit_daily


async def _run_with_concurrency(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[None]],
    concurrency: int,
) -> bool:
    """Run ``worker(item)`` over ``items`` with at most ``concurrency`` in flight.

    Returns ``True`` if all workers completed, ``False`` if any task hit
    ``RateLimitExceeded`` (in which case the remaining work is cancelled and
    the caller should stop this phase; the next scheduled sync resumes it).
    """
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
            except RateLimitExceeded:
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
        if isinstance(r, RateLimitExceeded):
            return False
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            raise r
    return not rate_limited.is_set()


def _activity_needs_streams(activity: Activity) -> bool:
    """True when we derive TRIMP/HR zones and power metrics from stream data (HR or power meter)."""
    return bool(activity.has_heartrate or activity.device_watts)


async def fetch_and_store_athlete_settings(
    client: StravaClient, session: AsyncSession, athlete_id: int
) -> AthleteSettings:
    """Fetch HR zones/FTP from Strava and store them, allowing for environment overrides."""
    # Priority 1: Environment Overrides
    max_hr = settings.MAX_HR
    rest_hr = settings.REST_HR
    ftp = settings.FTP

    hr_zones = None
    strava_max_hr = None
    strava_ftp = None

    try:
        # Fetch from Strava to get custom zones and profile data
        zones_data = await client.get_athlete_zones()
        if "heart_rate" in zones_data:
            hr_data = zones_data["heart_rate"]
            hr_zones = hr_data.get("zones")
            if hr_zones:
                last_zone = hr_zones[-1]
                if last_zone.get("max", 0) > 0:
                    strava_max_hr = last_zone["max"]
                elif len(hr_zones) >= 2:
                    strava_max_hr = hr_zones[-2].get("max")

        try:
            athlete_data = await client.get_athlete()
            strava_ftp = athlete_data.get("ftp")
        except Exception:
            pass

    except Exception as e:
        logger.warning("Could not fetch athlete data from Strava: %s", e)

    # Resolution logic: Env > Strava > Fallback
    is_hr_overridden = False

    if max_hr is not None:
        logger.info("Using MAX_HR from environment: %d", max_hr)
        is_hr_overridden = True
    else:
        max_hr = strava_max_hr or settings.FALLBACK_MAX_HR
        logger.info("Using MAX_HR: %d (%s)", max_hr, "Strava" if strava_max_hr else "Fallback")

    if rest_hr is not None:
        logger.info("Using REST_HR from environment: %d", rest_hr)
        is_hr_overridden = True
    else:
        rest_hr = settings.FALLBACK_REST_HR
        logger.info("Using REST_HR: %d (Fallback)", rest_hr)

    if ftp is not None:
        logger.info("Using FTP from environment: %d", ftp)
    else:
        ftp = strava_ftp or settings.FALLBACK_FTP
        logger.info("Using FTP: %d (%s)", ftp, "Strava" if strava_ftp else "Fallback")

    # Zone Consistency: If HR is overridden, ignore Strava custom zones to force recalculation
    if is_hr_overridden and hr_zones is not None:
        logger.info("HR overridden in environment; ignoring Strava custom zones for consistency.")
        hr_zones = None

    # Upsert athlete settings
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


def _normal_gear_id(raw: object) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() == "none":
        return None
    return s


async def _activity_row_from_strava(
    a: dict, client: StravaClient, *, resolve_gear: bool = True
) -> dict:
    """Map a Strava activity JSON object (summary or detailed) to Activity row fields."""
    gear = a.get("gear")
    gear_name_from_payload: str | None = None
    if isinstance(gear, dict):
        gear_name_from_payload = (
            (gear.get("nickname") or gear.get("name") or "").strip() or None
        )
    gid = _normal_gear_id(a.get("gear_id"))
    if not gid and isinstance(gear, dict):
        gid = _normal_gear_id(gear.get("id"))

    resolved_gear_name = gear_name_from_payload
    if resolve_gear and not resolved_gear_name and gid:
        resolved_gear_name = await client.get_gear_display_name(gid)

    wavg = a.get("weighted_average_watts")
    if wavg is not None:
        wavg = float(wavg)

    dev = a.get("device_name")
    if isinstance(dev, str):
        dev = dev[:255] if len(dev) > 255 else dev
    else:
        dev = None

    athlete = a.get("athlete")
    if isinstance(athlete, dict):
        athlete_id = athlete["id"]
    else:
        athlete_id = int(athlete)

    return {
        "id": a["id"],
        "athlete_id": athlete_id,
        "name": a.get("name", ""),
        "description": a.get("description"),
        "sport_type": a.get("sport_type", a.get("type", "")),
        "start_date": datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")),
        "elapsed_time": a.get("elapsed_time", 0),
        "moving_time": a.get("moving_time", 0),
        "distance": a.get("distance", 0.0),
        "average_heartrate": a.get("average_heartrate"),
        "max_heartrate": a.get("max_heartrate"),
        "has_heartrate": a.get("has_heartrate", False),
        "average_watts": a.get("average_watts"),
        "max_watts": a.get("max_watts"),
        "average_speed": a.get("average_speed"),
        "total_elevation_gain": a.get("total_elevation_gain"),
        "kilojoules": a.get("kilojoules"),
        "calories": a.get("calories"),
        "device_watts": bool(a.get("device_watts", False)),
        "device_name": dev,
        "gear_id": gid,
        "gear_name": resolved_gear_name,
        "weighted_average_watts": wavg,
        "suffer_score": a.get("suffer_score"),
    }


async def _apply_one_strava_detail(
    session: AsyncSession, client: StravaClient, activity_id: int
) -> bool:
    """GET /activities/{id} and merge fields; set strava_detail_synced when done.

    Returns True if the row was updated (including 404 tombstone). False on
    transient errors so the caller can move on and retry on a later sync.
    """
    try:
        detail = await client.get_activity(activity_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            logger.warning("GET /activities/%s returned 404", activity_id)
            await session.execute(
                update(Activity)
                .where(Activity.id == activity_id)
                .values(strava_detail_synced=True)
            )
            await session.commit()
            _snapshot_rate_limits(client)
            return True
        raise
    except RateLimitExceeded:
        _snapshot_rate_limits(client)
        raise
    except Exception as e:
        logger.warning("GET /activities/%s failed: %s", activity_id, e)
        return False

    _snapshot_rate_limits(client)
    if not isinstance(detail, dict):
        return False

    row = await _activity_row_from_strava(detail, client)
    row_id = row.pop("id")
    row["strava_detail_synced"] = True
    await session.execute(update(Activity).where(Activity.id == row_id).values(**row))
    await session.commit()
    return True


async def _upsert_activities_from_list(
    session: AsyncSession, activities: list[dict], client: StravaClient
) -> int:
    """Upsert activities from GET /athlete/activities payloads only (no per-activity detail calls)."""
    if not activities:
        return 0

    values = []
    for a in activities:
        row = await _activity_row_from_strava(a, client, resolve_gear=False)
        row["strava_detail_synced"] = False
        values.append(row)

    stmt = pg_insert(Activity).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "description": stmt.excluded.description,
            "sport_type": stmt.excluded.sport_type,
            "start_date": stmt.excluded.start_date,
            "elapsed_time": stmt.excluded.elapsed_time,
            "moving_time": stmt.excluded.moving_time,
            "distance": stmt.excluded.distance,
            "average_heartrate": stmt.excluded.average_heartrate,
            "max_heartrate": stmt.excluded.max_heartrate,
            "has_heartrate": stmt.excluded.has_heartrate,
            "average_watts": stmt.excluded.average_watts,
            "max_watts": stmt.excluded.max_watts,
            "average_speed": stmt.excluded.average_speed,
            "total_elevation_gain": stmt.excluded.total_elevation_gain,
            "kilojoules": stmt.excluded.kilojoules,
            "calories": stmt.excluded.calories,
            "device_watts": stmt.excluded.device_watts,
            "device_name": stmt.excluded.device_name,
            "gear_id": stmt.excluded.gear_id,
            "gear_name": stmt.excluded.gear_name,
            "weighted_average_watts": stmt.excluded.weighted_average_watts,
            "suffer_score": stmt.excluded.suffer_score,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(values)


async def _drain_strava_detail_queue(
    session: AsyncSession, client: StravaClient, athlete_id: int
) -> None:
    """Merge GET /activities/{id} for rows with strava_detail_synced=False.

    Runs with bounded concurrency (``settings.SYNC_DETAIL_CONCURRENCY``). The
    run stops when the queue is empty, when the Strava rate limit is hit, or
    when two consecutive batches make no progress. Remaining work resumes on
    the next scheduled sync.
    """
    sync_state.phase = "detailing"

    pending_result = await session.execute(
        select(func.count(Activity.id)).where(
            Activity.athlete_id == athlete_id,
            Activity.strava_detail_synced == False,  # noqa: E712
        )
    )
    pending_initial = pending_result.scalar_one()
    if pending_initial == 0:
        sync_state.details_pending = 0
        return

    sync_state.details_fetched = 0
    sync_state.details_pending = pending_initial

    concurrency = settings.SYNC_DETAIL_CONCURRENCY
    logger.info(
        "Merging Strava activity details (%d pending, concurrency %d)...",
        pending_initial,
        concurrency,
    )

    counter_lock = asyncio.Lock()
    no_progress_batches = 0

    while True:
        result = await session.execute(
            select(Activity.id)
            .where(
                Activity.athlete_id == athlete_id,
                Activity.strava_detail_synced == False,  # noqa: E712
            )
            .order_by(Activity.start_date.desc())
            .limit(max(100, concurrency * 20))
        )
        ids = [row[0] for row in result.all()]
        if not ids:
            break

        batch_merged = 0

        async def _worker(activity_id: int) -> None:
            nonlocal batch_merged
            # Each worker uses its own session; the parent ``session`` is only
            # read above for queue selection and below for final counts.
            async with async_session() as worker_session:
                merged = await _apply_one_strava_detail(
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
                "Strava rate limit while merging activity details; "
                "will resume on the next sync."
            )
            logger.warning("Detail merge paused by rate limit; resuming on next sync")
            break

        if batch_merged == 0:
            no_progress_batches += 1
            if no_progress_batches >= 2:
                logger.warning(
                    "Detail merge: no successful merges in consecutive batches; "
                    "will retry on the next sync"
                )
                break
        else:
            no_progress_batches = 0

    remaining = await session.execute(
        select(func.count(Activity.id)).where(
            Activity.athlete_id == athlete_id,
            Activity.strava_detail_synced == False,  # noqa: E712
        )
    )
    n_left = remaining.scalar_one()
    sync_state.details_pending = n_left
    if n_left > 0:
        logger.info(
            "%d activities still queued for Strava detail merge; next sync continues",
            n_left,
        )


async def _process_activity_streams(
    client: StravaClient,
    session: AsyncSession,
    activity: Activity,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
):
    """Fetch Strava streams for every pending activity; persist raw data for maps; derive HR/power metrics only when applicable."""
    needs_hr_power = _activity_needs_streams(activity)

    try:
        streams = await client.get_activity_streams(activity.id)
        _snapshot_rate_limits(client)
        if not isinstance(streams, dict):
            streams = {}

        stream_data: dict = {}
        stream_types: list[str] = []
        sample_count = 0
        for key, value in streams.items():
            if isinstance(value, dict) and "data" in value:
                stream_data[key] = value["data"]
                stream_types.append(key)
                sample_count = max(sample_count, len(value["data"]))

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

        time_stream = streams.get("time", {}).get("data", [])
        hr_stream = streams.get("heartrate", {}).get("data", [])
        power_stream = streams.get("watts", {}).get("data", [])

        trimp = activity.trimp
        hr_zones = activity.hr_zone_seconds

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

    except RateLimitExceeded:
        _snapshot_rate_limits(client)
        raise
    except Exception as e:
        logger.warning(
            "Failed to fetch streams for activity %d: %s", activity.id, e
        )
        trimp = estimate_trimp_without_hr(activity.sport_type, activity.moving_time)
        await session.execute(
            update(Activity)
            .where(Activity.id == activity.id)
            .values(trimp=trimp, synced_streams=True)
        )
        await session.commit()


async def run_sync(session: AsyncSession, force_resync: bool = False):
    """Main sync entry point. Handles both backfill and incremental sync."""
    if sync_state.is_running:
        logger.info("Sync already in progress, skipping")
        return

    sync_state.is_running = True
    sync_state.last_error = None
    sync_state.synced_activities = 0
    sync_state.total_activities = 0
    sync_state.details_fetched = 0
    sync_state.details_pending = 0
    sync_state.streams_fetched = 0
    sync_state.streams_pending = 0

    client = StravaClient(session)

    try:
        # Get athlete ID from token
        token_result = await session.execute(select(StravaToken).limit(1))
        token = token_result.scalar_one_or_none()
        if token is None:
            sync_state.phase = "idle"
            sync_state.last_error = "Not authenticated. Please connect with Strava."
            return

        athlete_id = token.athlete_id

        if force_resync:
            logger.info("Forcing full resync for athlete %d", athlete_id)
            await force_full_resync(session, athlete_id)

        # Fetch/update athlete settings (HR/Power zones)
        athlete_settings = await fetch_and_store_athlete_settings(
            client, session, athlete_id
        )
        hr_zone_boundaries = get_hr_zone_boundaries(
            athlete_settings.max_hr,
            athlete_settings.rest_hr,
            athlete_settings.hr_zones,
        )
        power_zone_boundaries = get_power_zone_boundaries(athlete_settings.ftp)

        # Check what we already have
        count_result = await session.execute(
            select(func.count(Activity.id)).where(
                Activity.athlete_id == athlete_id
            )
        )
        existing_count = count_result.scalar_one()

        if existing_count == 0 or force_resync:
            # Full backfill (list pages only; detail merge runs after)
            await _backfill(client, session, athlete_id, hr_zone_boundaries, power_zone_boundaries)
        else:
            # Incremental sync (list pages only)
            await _incremental_sync(client, session, athlete_id, hr_zone_boundaries, power_zone_boundaries)

        await _drain_strava_detail_queue(session, client, athlete_id)
        # Process any activities that don't have streams yet
        await _process_pending_streams(client, session, athlete_id, hr_zone_boundaries, power_zone_boundaries)

        # Recalculate estimated FTP
        await _recalculate_estimated_ftp(session, token.athlete_id)

        # Backfill power curves for activities that have streams but no curve
        await _backfill_power_curves(session, athlete_id)

        # Recalculate daily metrics
        sync_state.phase = "calculating"
        await recalculate_daily_metrics(session, athlete_id)

        sync_state.phase = "idle"
        sync_state.last_sync = datetime.now(timezone.utc)
        logger.info("Sync completed successfully")

    except RateLimitExceeded as e:
        sync_state.last_error = str(e)
        logger.warning("Sync paused due to rate limit: %s", e)
    except Exception as e:
        sync_state.last_error = str(e)
        logger.exception("Sync failed: %s", e)
    finally:
        sync_state.is_running = False
        await client.close()


async def _backfill(
    client: StravaClient,
    session: AsyncSession,
    athlete_id: int,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
):
    """Fetch all historical activities from Strava."""
    sync_state.phase = "backfilling"
    logger.info("Starting full backfill of Strava activities...")

    page = 1
    total_stored = 0

    while True:
        activities = await client.get_activities(page=page, per_page=200)
        _snapshot_rate_limits(client)
        if not activities:
            break

        stored = await _upsert_activities_from_list(session, activities, client)
        total_stored += stored
        sync_state.total_activities = total_stored
        sync_state.synced_activities = total_stored

        logger.info(
            "Backfill page %d: fetched %d activities (total: %d)",
            page,
            len(activities),
            total_stored,
        )

        if len(activities) < 200:
            break

        page += 1

    logger.info("Backfill complete: %d activities stored", total_stored)


async def _incremental_sync(
    client: StravaClient,
    session: AsyncSession,
    athlete_id: int,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
):
    """Fetch only new activities since the last sync."""
    sync_state.phase = "incremental"

    # Get the timestamp of the most recent activity
    result = await session.execute(
        select(func.max(Activity.start_date)).where(
            Activity.athlete_id == athlete_id
        )
    )
    latest = result.scalar_one_or_none()

    after = None
    if latest:
        after = int(latest.timestamp())

    logger.info("Incremental sync: fetching activities after %s", latest)

    page = 1
    total_new = 0

    while True:
        activities = await client.get_activities(
            page=page, per_page=200, after=after
        )
        _snapshot_rate_limits(client)
        if not activities:
            break

        stored = await _upsert_activities_from_list(session, activities, client)
        total_new += stored

        if len(activities) < 200:
            break

        page += 1

    logger.info("Incremental sync: %d new activities", total_new)


async def _process_pending_streams(
    client: StravaClient,
    session: AsyncSession,
    athlete_id: int,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
):
    """Fetch and store Strava streams for activities not yet marked synced.

    Runs with bounded concurrency (``settings.SYNC_STREAMS_CONCURRENCY``).
    Stops on rate-limit; remaining work resumes on the next scheduled sync.
    """
    sync_state.phase = "streaming"

    result = await session.execute(
        select(Activity.id)
        .where(
            Activity.athlete_id == athlete_id,
            Activity.synced_streams == False,  # noqa: E712
        )
        .order_by(Activity.start_date)
    )
    pending_ids = [row[0] for row in result.all()]

    if not pending_ids:
        sync_state.streams_pending = 0
        return

    total = len(pending_ids)
    sync_state.streams_fetched = 0
    sync_state.streams_pending = total

    concurrency = settings.SYNC_STREAMS_CONCURRENCY
    logger.info(
        "Processing streams for %d activities (concurrency %d)...",
        total,
        concurrency,
    )

    counter_lock = asyncio.Lock()
    done_count = 0
    log_every = max(50, concurrency * 10)

    async def _worker(activity_id: int) -> None:
        nonlocal done_count
        async with async_session() as worker_session:
            row = await worker_session.get(Activity, activity_id)
            if row is None:
                return
            await _process_activity_streams(
                client, worker_session, row, hr_zone_boundaries, power_zone_boundaries
            )
        async with counter_lock:
            sync_state.streams_fetched += 1
            if sync_state.streams_pending > 0:
                sync_state.streams_pending -= 1
            done_count += 1
            n = done_count
        if n % log_every == 0:
            logger.info("Processed streams: %d/%d", n, total)

    completed = await _run_with_concurrency(pending_ids, _worker, concurrency)
    if not completed:
        logger.warning(
            "Rate limit hit during stream processing at %d/%d. Will resume on next sync.",
            sync_state.streams_fetched,
            total,
        )
        raise RateLimitExceeded(reset_after=900)

    logger.info("All pending streams processed")


async def _recalculate_estimated_ftp(session: AsyncSession, athlete_id: int):
    """Estimate FTP based on 95% of the best 20-minute power across all activities."""
    result = await session.execute(
        select(func.max(Activity.best_20min_power)).where(
            Activity.athlete_id == athlete_id
        )
    )
    best_20min = result.scalar_one_or_none()

    if best_20min:
        estimated_ftp = int(best_20min * 0.95)
        logger.info(
            "Recalculated estimated FTP: %d W (based on best 20min power of %.1f W)",
            estimated_ftp,
            best_20min,
        )

        await session.execute(
            update(AthleteSettings)
            .where(AthleteSettings.athlete_id == athlete_id)
            .values(estimated_ftp=estimated_ftp)
        )
        await session.commit()


async def _backfill_power_curves(session: AsyncSession, athlete_id: int):
    """Calculate power curves for activities that have streams but are missing the curve data."""
    # Find activities that have watts stream in ActivityStream but power_curve is null in Activity
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

    logger.info("Backfilling power curves for %d activities...", len(to_process))

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
    logger.info("Power curve backfill complete")

async def force_full_resync(session: AsyncSession, athlete_id: int):
    """Mark all activities for re-sync and clear calculated metrics."""
    logger.info("Resetting sync state for all activities of athlete %d", athlete_id)
    await session.execute(
        update(Activity)
        .where(Activity.athlete_id == athlete_id)
        .values(
            synced_streams=False,
            strava_detail_synced=False,
            trimp=None,
            hr_zone_seconds=None,
            power_zone_seconds=None,
            best_20min_power=None,
            power_curve=None,
        )
    )
    await session.commit()
