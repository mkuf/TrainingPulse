"""Training metrics calculation engine.

Implements:
- TRIMP (Training Impulse) — zone-weighted heart rate load per activity
- CTL (Chronic Training Load) — 42-day exponential moving average ("Fitness")
- ATL (Acute Training Load) — 7-day exponential moving average ("Fatigue")
- TSB (Training Stress Balance) — CTL - ATL ("Form")
"""

import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import Activity, AthleteSettings, DailyMetrics

logger = logging.getLogger(__name__)

# ── TRIMP zone weights ──────────────────────────────────────────────
# Time in each zone is multiplied by this weight.
# Zone 5 is weighted 8x Zone 1 to reflect the disproportionate
# cardiovascular stress of high-intensity work.
ZONE_WEIGHTS = {
    1: 1.0,  # Recovery   (50-60% max HR)
    2: 2.0,  # Endurance  (60-70% max HR)
    3: 3.0,  # Tempo      (70-80% max HR)
    4: 4.0,  # Threshold  (80-90% max HR)
    5: 8.0,  # VO2max     (90-100% max HR)
}

# Fallback TRIMP-per-minute estimates for activities without HR data
SPORT_TRIMP_PER_MIN = {
    "Run": 1.5,
    "Trail Run": 1.6,
    "VirtualRun": 1.4,
    "Ride": 1.2,
    "VirtualRide": 1.2,
    "MountainBikeRide": 1.4,
    "GravelRide": 1.3,
    "Swim": 1.3,
    "Walk": 0.8,
    "Hike": 1.0,
    "NordicSki": 1.4,
    "AlpineSki": 0.9,
    "WeightTraining": 1.2,
    "Workout": 1.2,
    "Yoga": 0.5,
    "Rowing": 1.3,
    "Kayaking": 1.1,
    "Crossfit": 1.5,
}
DEFAULT_TRIMP_PER_MIN = 1.0


def get_hr_zone_boundaries(
    max_hr: int, rest_hr: int, custom_zones: list[dict] | None = None
) -> list[tuple[float, float]]:
    """Return HR zone boundaries as (min_hr, max_hr) tuples.

    If Strava provides custom zones, use those. Otherwise compute
    standard percentage-based zones.
    """
    if custom_zones:
        # Strava zones come as [{"min": 0, "max": 123}, {"min": 123, "max": 153}, ...]
        return [(z["min"], z["max"]) for z in custom_zones]

    # Standard 5-zone model based on percentage of max HR
    return [
        (max_hr * 0.50, max_hr * 0.60),  # Zone 1
        (max_hr * 0.60, max_hr * 0.70),  # Zone 2
        (max_hr * 0.70, max_hr * 0.80),  # Zone 3
        (max_hr * 0.80, max_hr * 0.90),  # Zone 4
        (max_hr * 0.90, max_hr * 1.00),  # Zone 5
    ]


def classify_hr_to_zone(hr: float, zone_boundaries: list[tuple[float, float]]) -> int:
    """Return the 1-indexed zone number for a given heart rate value."""
    for i, (low, high) in enumerate(zone_boundaries):
        if hr < high or i == len(zone_boundaries) - 1:
            return i + 1
    return len(zone_boundaries)


def calculate_trimp_from_streams(
    time_data: list[int],
    hr_data: list[int],
    zone_boundaries: list[tuple[float, float]],
) -> tuple[float, dict[str, float]]:
    """Calculate TRIMP from HR stream data."""
    zone_seconds = {f"zone_{i}": 0.0 for i in range(1, 6)}

    for i in range(1, len(time_data)):
        dt = time_data[i] - time_data[i - 1]
        if dt <= 0:
            continue

        hr = hr_data[i]
        if hr <= 0:
            continue

        zone = classify_hr_to_zone(hr, zone_boundaries)
        zone_seconds[f"zone_{zone}"] += dt

    trimp = 0.0
    for zone_num in range(1, 6):
        seconds = zone_seconds[f"zone_{zone_num}"]
        trimp += (seconds / 60.0) * ZONE_WEIGHTS[zone_num]

    return trimp, zone_seconds


def get_power_zone_boundaries(ftp: int) -> list[tuple[float, float]]:
    """Return Power zone boundaries as (min, max) watts.
    Based on standard Coggan 7-zone model.
    """
    return [
        (0, ftp * 0.55),     # Zone 1: Active Recovery
        (ftp * 0.55, ftp * 0.75),  # Zone 2: Endurance
        (ftp * 0.75, ftp * 0.90),  # Zone 3: Tempo
        (ftp * 0.90, ftp * 1.05),  # Zone 4: Threshold
        (ftp * 1.05, ftp * 1.20),  # Zone 5: VO2 Max
        (ftp * 1.20, ftp * 1.50),  # Zone 6: Anaerobic Capacity
        (ftp * 1.50, ftp * 10.0),  # Zone 7: Neuromuscular Power
    ]


def calculate_best_interval(power_stream: list[int], window_seconds: int) -> float | None:
    """Find the highest average power for a given duration (seconds)."""
    if not power_stream or len(power_stream) < window_seconds:
        return None

    current_sum = sum(power_stream[:window_seconds])
    max_sum = current_sum

    for i in range(len(power_stream) - window_seconds):
        current_sum = current_sum - power_stream[i] + power_stream[i + window_seconds]
        if current_sum > max_sum:
            max_sum = current_sum

    return max_sum / window_seconds


def calculate_power_zones(
    time_data: list[int],
    power_data: list[int],
    zone_boundaries: list[tuple[float, float]],
) -> dict[str, float]:
    """Calculate time spent in each power zone."""
    zone_seconds = {f"zone_{i}": 0.0 for i in range(1, 8)}

    for i in range(1, len(time_data)):
        dt = time_data[i] - time_data[i - 1]
        if dt <= 0:
            continue

        power = power_data[i]
        if power < 0:
            continue

        # Classify power to zone
        zone = 7
        for z_idx, (low, high) in enumerate(zone_boundaries):
            if power < high:
                zone = z_idx + 1
                break

        zone_seconds[f"zone_{zone}"] += dt

    return zone_seconds


def estimate_trimp_without_hr(sport_type: str, moving_time_seconds: int) -> float:
    """Estimate TRIMP for activities without heart rate data."""
    minutes = moving_time_seconds / 60.0
    rate = SPORT_TRIMP_PER_MIN.get(sport_type, DEFAULT_TRIMP_PER_MIN)
    return minutes * rate


async def recalculate_daily_metrics(
    session: AsyncSession, athlete_id: int, from_date: date | None = None
):
    """Recalculate CTL, ATL, TSB for all days from from_date to today.

    Uses an exponentially weighted moving average:
        CTL_today = CTL_yesterday + (TRIMP_today - CTL_yesterday) / 42
        ATL_today = ATL_yesterday + (TRIMP_today - ATL_yesterday) / 7
        TSB_today = CTL_yesterday - ATL_yesterday
    """
    # Find the earliest activity date if no from_date specified
    if from_date is None:
        result = await session.execute(
            select(func.min(Activity.start_date)).where(
                Activity.athlete_id == athlete_id
            )
        )
        earliest = result.scalar_one_or_none()
        if earliest is None:
            logger.info("No activities found, skipping metric recalculation")
            return
        from_date = earliest.date()

    today = date.today()

    # Get all activities grouped by date
    result = await session.execute(
        select(
            func.date(Activity.start_date).label("activity_date"),
            func.coalesce(func.sum(Activity.trimp), 0.0).label("daily_trimp"),
            func.count(Activity.id).label("activity_count"),
        )
        .where(Activity.athlete_id == athlete_id)
        .where(Activity.start_date >= from_date)
        .group_by(func.date(Activity.start_date))
    )
    daily_trimp_map: dict[date, tuple[float, int]] = {}
    for row in result:
        daily_trimp_map[row.activity_date] = (row.daily_trimp, row.activity_count)

    # If recalculating from a point, get previous day's CTL/ATL
    ctl = 0.0
    atl = 0.0
    if from_date > date(2000, 1, 1):
        prev_date = from_date - timedelta(days=1)
        result = await session.execute(
            select(DailyMetrics).where(
                DailyMetrics.date == prev_date,
                DailyMetrics.athlete_id == athlete_id,
            )
        )
        prev_metrics = result.scalar_one_or_none()
        if prev_metrics:
            ctl = prev_metrics.ctl
            atl = prev_metrics.atl

    # Iterate day by day
    current_date = from_date
    batch = []
    while current_date <= today:
        trimp_today, count_today = daily_trimp_map.get(current_date, (0.0, 0))

        # TSB uses yesterday's values
        tsb = ctl - atl

        # Update CTL and ATL
        ctl = ctl + (trimp_today - ctl) / 42.0
        atl = atl + (trimp_today - atl) / 7.0

        batch.append(
            {
                "date": current_date,
                "athlete_id": athlete_id,
                "daily_trimp": trimp_today,
                "ctl": round(ctl, 2),
                "atl": round(atl, 2),
                "tsb": round(tsb, 2),
                "activity_count": count_today,
            }
        )

        current_date += timedelta(days=1)

    # Upsert all daily metrics
    if batch:
        stmt = pg_insert(DailyMetrics).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["date", "athlete_id"],
            set_={
                "daily_trimp": stmt.excluded.daily_trimp,
                "ctl": stmt.excluded.ctl,
                "atl": stmt.excluded.atl,
                "tsb": stmt.excluded.tsb,
                "activity_count": stmt.excluded.activity_count,
            },
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(
            "Recalculated %d days of metrics (from %s to %s)",
            len(batch),
            from_date,
            today,
        )
