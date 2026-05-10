"""Activity sync engine — handles backfilling and incremental sync from Strava."""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from metrics import (
    calculate_trimp_from_streams,
    estimate_trimp_without_hr,
    get_hr_zone_boundaries,
    recalculate_daily_metrics,
)
from models import Activity, AthleteSettings, StravaToken
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
    """Fetch HR zones from Strava and store them in the database."""
    hr_zones = None
    max_hr = settings.DEFAULT_MAX_HR
    rest_hr = settings.DEFAULT_REST_HR

    try:
        zones_data = await client.get_athlete_zones()
        # zones_data has "heart_rate" key with "zones" list and
        # optionally "custom_zones" boolean
        if "heart_rate" in zones_data:
            hr_data = zones_data["heart_rate"]
            hr_zones = hr_data.get("zones")
            if hr_zones:
                # Infer max HR from the last zone's max value
                last_zone = hr_zones[-1]
                if last_zone.get("max", 0) > 0:
                    max_hr = last_zone["max"]
                elif len(hr_zones) >= 2:
                    # Sometimes the last zone has max=-1, use the previous zone
                    max_hr = hr_zones[-2].get("max", max_hr)
                logger.info(
                    "Fetched HR zones from Strava: %d zones, max_hr=%d",
                    len(hr_zones),
                    max_hr,
                )
    except Exception as e:
        logger.warning("Could not fetch HR zones from Strava: %s. Using defaults.", e)

    # Upsert athlete settings
    stmt = pg_insert(AthleteSettings).values(
        athlete_id=athlete_id,
        max_hr=max_hr,
        rest_hr=rest_hr,
        hr_zones=hr_zones,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["athlete_id"],
        set_={
            "max_hr": stmt.excluded.max_hr,
            "rest_hr": stmt.excluded.rest_hr,
            "hr_zones": stmt.excluded.hr_zones,
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
                "suffer_score": a.get("suffer_score"),
            }
        )

    stmt = pg_insert(Activity).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "sport_type": stmt.excluded.sport_type,
            "average_heartrate": stmt.excluded.average_heartrate,
            "max_heartrate": stmt.excluded.max_heartrate,
            "has_heartrate": stmt.excluded.has_heartrate,
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
    zone_boundaries: list[tuple[float, float]],
):
    """Fetch HR stream data for an activity and calculate TRIMP."""
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

        time_stream = streams.get("time", {}).get("data", [])
        hr_stream = streams.get("heartrate", {}).get("data", [])

        if time_stream and hr_stream and len(time_stream) == len(hr_stream):
            trimp, zone_seconds = calculate_trimp_from_streams(
                time_stream, hr_stream, zone_boundaries
            )
            await session.execute(
                update(Activity)
                .where(Activity.id == activity.id)
                .values(
                    trimp=trimp,
                    hr_zone_seconds=zone_seconds,
                    synced_streams=True,
                )
            )
        else:
            # HR stream not available despite has_heartrate=True
            trimp = estimate_trimp_without_hr(
                activity.sport_type, activity.moving_time
            )
            await session.execute(
                update(Activity)
                .where(Activity.id == activity.id)
                .values(trimp=trimp, synced_streams=True)
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


async def run_sync(session: AsyncSession):
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

        # Fetch/update athlete settings (HR zones)
        athlete_settings = await fetch_and_store_athlete_settings(
            client, session, athlete_id
        )
        zone_boundaries = get_hr_zone_boundaries(
            athlete_settings.max_hr,
            athlete_settings.rest_hr,
            athlete_settings.hr_zones,
        )

        # Check what we already have
        count_result = await session.execute(
            select(func.count(Activity.id)).where(
                Activity.athlete_id == athlete_id
            )
        )
        existing_count = count_result.scalar_one()

        if existing_count == 0:
            # Full backfill
            await _backfill(client, session, athlete_id, zone_boundaries)
        else:
            # Incremental sync
            await _incremental_sync(client, session, athlete_id, zone_boundaries)

        # Process any activities that don't have streams yet
        await _process_pending_streams(client, session, athlete_id, zone_boundaries)

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
    zone_boundaries: list[tuple[float, float]],
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
    zone_boundaries: list[tuple[float, float]],
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
    zone_boundaries: list[tuple[float, float]],
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
                client, session, activity, zone_boundaries
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
