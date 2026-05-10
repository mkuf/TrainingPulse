"""Activity sync engine — handles backfilling and incremental sync from Strava."""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
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
    """Tracks the current state of the sync process."""

    def __init__(self):
        self.is_running: bool = False
        self.phase: str = "idle"  # idle, backfilling, incremental, calculating
        self.total_activities: int = 0
        self.synced_activities: int = 0
        self.streams_fetched: int = 0
        self.last_error: str | None = None
        self.last_sync: datetime | None = None


# Global sync state (shared across the app)
sync_state = SyncState()


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


async def _store_activities(session: AsyncSession, activities: list[dict]) -> int:
    """Store a batch of activities in the database (upsert). Returns count stored."""
    if not activities:
        return 0

    values = []
    for a in activities:
        values.append(
            {
                "id": a["id"],
                "athlete_id": a["athlete"]["id"],
                "name": a.get("name", ""),
                "description": a.get("description"),
                "sport_type": a.get("sport_type", a.get("type", "")),
                "start_date": datetime.fromisoformat(
                    a["start_date"].replace("Z", "+00:00")
                ),
                "elapsed_time": a.get("elapsed_time", 0),
                "moving_time": a.get("moving_time", 0),
                "distance": a.get("distance", 0.0),
                "average_heartrate": a.get("average_heartrate"),
                "max_heartrate": a.get("max_heartrate"),
                "has_heartrate": a.get("has_heartrate", False),
                "average_watts": a.get("average_watts"),
                "max_watts": a.get("max_watts"),
                "suffer_score": a.get("suffer_score"),
            }
        )

    stmt = pg_insert(Activity).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "description": stmt.excluded.description,
            "sport_type": stmt.excluded.sport_type,
            "average_heartrate": stmt.excluded.average_heartrate,
            "max_heartrate": stmt.excluded.max_heartrate,
            "has_heartrate": stmt.excluded.has_heartrate,
            "average_watts": stmt.excluded.average_watts,
            "max_watts": stmt.excluded.max_watts,
            "suffer_score": stmt.excluded.suffer_score,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(values)


async def _process_activity_streams(
    client: StravaClient,
    session: AsyncSession,
    activity: Activity,
    hr_zone_boundaries: list[tuple[float, float]],
    power_zone_boundaries: list[tuple[float, float]],
):
    """Fetch all stream data for an activity, store it, and calculate TRIMP/Zones."""
    if not activity.has_heartrate:
        # Estimate TRIMP from duration and sport type
        trimp = estimate_trimp_without_hr(activity.sport_type, activity.moving_time)
        await session.execute(
            update(Activity)
            .where(Activity.id == activity.id)
            .values(trimp=trimp, synced_streams=True)
        )
        await session.commit()
        return

    try:
        streams = await client.get_activity_streams(activity.id)

        # Store all raw stream data for later visualization
        if streams:
            stream_data = {}
            stream_types = []
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

        # Calculate TRIMP from HR stream
        time_stream = streams.get("time", {}).get("data", [])
        hr_stream = streams.get("heartrate", {}).get("data", [])
        power_stream = streams.get("watts", {}).get("data", [])

        trimp = activity.trimp
        hr_zones = activity.hr_zone_seconds
        power_zones = activity.power_zone_seconds

        if time_stream and hr_stream and len(time_stream) == len(hr_stream):
            trimp, hr_zones = calculate_trimp_from_streams(
                time_stream, hr_stream, hr_zone_boundaries
            )
        elif not trimp:
            # Fall back to estimation if TRIMP not yet set
            trimp = estimate_trimp_without_hr(
                activity.sport_type, activity.moving_time
            )

        best_20min = None
        power_curve = None
        if time_stream and power_stream and len(time_stream) == len(power_stream):
            power_zones = calculate_power_zones(
                time_stream, power_stream, power_zone_boundaries
            )
            # Calculate best 20 minute power and full power curve
            best_20min = calculate_best_interval(power_stream, 1200)
            power_curve = calculate_power_curve(power_stream)
        else:
            power_zones = None

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
        raise
    except Exception as e:
        logger.warning(
            "Failed to fetch streams for activity %d: %s", activity.id, e
        )
        # Fall back to estimation
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
            # Full backfill
            await _backfill(client, session, athlete_id, hr_zone_boundaries, power_zone_boundaries)
        else:
            # Incremental sync
            await _incremental_sync(client, session, athlete_id, hr_zone_boundaries, power_zone_boundaries)

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
        if not activities:
            break

        stored = await _store_activities(session, activities)
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
        if not activities:
            break

        stored = await _store_activities(session, activities)
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
    """Process HR streams for activities that haven't been processed yet."""
    sync_state.phase = "backfilling"

    result = await session.execute(
        select(Activity)
        .where(
            Activity.athlete_id == athlete_id,
            Activity.synced_streams == False,  # noqa: E712
        )
        .order_by(Activity.start_date)
    )
    pending = result.scalars().all()

    if not pending:
        return

    sync_state.total_activities = len(pending)
    sync_state.streams_fetched = 0

    logger.info("Processing HR streams for %d activities...", len(pending))

    for i, activity in enumerate(pending):
        try:
            await _process_activity_streams(
                client, session, activity, hr_zone_boundaries, power_zone_boundaries
            )
            sync_state.streams_fetched = i + 1

            if (i + 1) % 50 == 0:
                logger.info(
                    "Processed streams: %d/%d", i + 1, len(pending)
                )

        except RateLimitExceeded:
            logger.warning(
                "Rate limit hit during stream processing at %d/%d. "
                "Will resume on next sync.",
                i + 1,
                len(pending),
            )
            raise

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
            trimp=None,
            hr_zone_seconds=None,
            power_zone_seconds=None,
            best_20min_power=None,
            power_curve=None,
        )
    )
    await session.commit()
