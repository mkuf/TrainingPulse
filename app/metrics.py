"""Training metrics calculation engine.

Implements:
- TRIMP (Training Impulse) — zone-weighted heart rate load per activity
- CTL (Chronic Training Load) — 42-day exponential moving average ("Fitness")
- ATL (Acute Training Load) — 7-day exponential moving average ("Fatigue")
- TSB (Training Stress Balance) — CTL - ATL ("Form")
- Weekly load target — 7 × start-of-week CTL, the weekly TRIMP that keeps CTL flat
- Normalized power — 4th-power mean of 30-second rolling power
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import ACTIVITY_VISIBLE, Activity, AthleteSettings, DailyMetrics

logger = logging.getLogger(__name__)

# ── TRIMP zone weights ──────────────────────────────────────────────
# Time in each zone is multiplied by this weight.
# Zones 1-4 follow Edwards' linear weights. Zone 5 is weighted 6 instead of
# 5, which approximates Banister's exponential curve at that intensity.
ZONE_WEIGHTS = {
    1: 1.0,  # Recovery   (50-60% max HR)
    2: 2.0,  # Endurance  (60-70% max HR)
    3: 3.0,  # Tempo      (70-80% max HR)
    4: 4.0,  # Threshold  (80-90% max HR)
    5: 6.0,  # VO2max    (90-100% max HR)
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

# ── Form bands ──────────────────────────────────────────────────────
# Form in percent: TSB divided by the CTL it was computed from. Fixed TSB
# limits fit only one scale of load, so the bands use this ratio instead.
#: Lowest form in percent of each status, highest first. The Form panel on
#: the Fitness dashboard uses the same limits (checked by a test).
FORM_STATUSES = (
    (25.0, "transition"),
    (5.0, "fresh"),
    (-10.0, "neutral"),
    (-30.0, "optimal_training"),
    (float("-inf"), "high_risk"),
)


def form_pct(tsb: float | None, ctl: float | None) -> float | None:
    """TSB in percent of ``ctl``, the fitness that TSB was computed from.

    ``None`` without TSB or without fitness (CTL 0 or less).
    """
    if tsb is None or ctl is None or ctl <= 0:
        return None
    return 100.0 * tsb / ctl


def form_status(pct: float | None) -> str | None:
    """The status a form in percent reads as, ``None`` without a value."""
    if pct is None:
        return None
    return next(name for lowest, name in FORM_STATUSES if pct >= lowest)


# ── Weekly load target ──────────────────────────────────────────────
# CTL is an average of daily TRIMP, so a week with 7 × CTL of TRIMP keeps
# fitness where it is. The target of a Monday-to-Sunday week is 7 × the CTL
# at the end of the Sunday before, so it does not move during the week.
#: Weekly TRIMP in multiples of 7 × start-of-week CTL. At the target, fitness
#: stays where it is; up to the upper limit it grows by up to about 5 % a
#: week; above it, load rises faster than is usually sustainable.
WEEK_TARGET = 1.0
WEEK_UPPER_LIMIT = 1.3
#: Days of training-load history before a week's start CTL counts as a
#: target: one CTL time constant. Before that, CTL is still climbing from 0.
WEEK_TARGET_MIN_DAYS = 42
#: Statuses of a week. ``in_progress`` is a week that is not over and has not
#: reached its target yet; when it is over, it becomes ``below_target``.
WEEK_STATUSES = ("in_progress", "below_target", "on_target", "above_limit")


@dataclass(frozen=True)
class TrainingWeek:
    """The TRIMP of one Monday-to-Sunday week against its load target."""

    start: date
    #: Last day counted: Sunday, or today while the week is not over.
    through: date
    load: float
    #: CTL at the end of the Sunday before; None without enough history.
    start_ctl: float | None
    #: Today is past Sunday.
    finished: bool

    @property
    def end(self) -> date:
        return self.start + timedelta(days=6)

    @property
    def target(self) -> float | None:
        return None if self.start_ctl is None else 7 * self.start_ctl * WEEK_TARGET

    @property
    def upper_limit(self) -> float | None:
        return (
            None if self.start_ctl is None else 7 * self.start_ctl * WEEK_UPPER_LIMIT
        )

    @property
    def pct_of_target(self) -> float | None:
        return None if self.target is None else 100.0 * self.load / self.target

    @property
    def remaining(self) -> float | None:
        """TRIMP still needed to reach the target, 0 once reached."""
        return None if self.target is None else max(0.0, self.target - self.load)

    @property
    def status(self) -> str | None:
        return week_status(self.load, self.target, self.upper_limit, self.finished)


def week_status(
    load: float, target: float | None, upper_limit: float | None, finished: bool
) -> str | None:
    """The status of a week's load, ``None`` without a target."""
    if target is None or upper_limit is None:
        return None
    if load > upper_limit:
        return "above_limit"
    if load >= target:
        return "on_target"
    return "below_target" if finished else "in_progress"


def week_start(day: date) -> date:
    """The Monday of the week ``day`` is in."""
    return day - timedelta(days=day.weekday())


async def training_weeks(
    session: AsyncSession, first: date, last: date, today: date
) -> list[TrainingWeek]:
    """The weeks from the one ``first`` is in to the one ``last`` is in.

    Load is the sum of daily TRIMP (visible activities, local days) up to
    today. Weeks after today and weeks without any training-load history
    yet are left out.
    """
    first_monday = week_start(first)
    last_monday = week_start(min(last, today))
    history_start = await session.scalar(select(func.min(DailyMetrics.date)))
    if history_start is None or last_monday < first_monday:
        return []
    rows = (
        await session.execute(
            select(DailyMetrics.date, DailyMetrics.daily_trimp, DailyMetrics.ctl).where(
                DailyMetrics.date >= first_monday - timedelta(days=1),
                DailyMetrics.date <= min(last_monday + timedelta(days=6), today),
            )
        )
    ).all()
    trimp_by_day = {row.date: row.daily_trimp for row in rows}
    ctl_by_day = {row.date: row.ctl for row in rows}

    weeks = []
    monday = max(first_monday, week_start(history_start))
    while monday <= last_monday:
        sunday = monday + timedelta(days=6)
        through = min(sunday, today)
        start_ctl = ctl_by_day.get(monday - timedelta(days=1))
        if (
            start_ctl is None
            or start_ctl <= 0
            or (monday - history_start).days < WEEK_TARGET_MIN_DAYS
        ):
            start_ctl = None
        load = sum(
            trimp_by_day.get(monday + timedelta(days=offset), 0.0)
            for offset in range((through - monday).days + 1)
        )
        weeks.append(
            TrainingWeek(
                start=monday,
                through=through,
                load=load,
                start_ctl=start_ctl,
                finished=today > sunday,
            )
        )
        monday += timedelta(days=7)
    return weeks


def get_hr_zone_boundaries(
    max_hr: int, rest_hr: int, custom_zones: list[dict] | None = None
) -> list[tuple[float, float]]:
    """Return HR zone boundaries as (min_hr, max_hr) tuples.

    If custom zones are provided, use those. Otherwise compute
    standard percentage-based zones.
    """
    if custom_zones:
        # Zones come as [{"min": 0, "max": 123}, {"min": 123, "max": 153}, ...]
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


# Standard durations for the power curve (in seconds)
POWER_CURVE_DURATIONS = [
    1,
    2,
    5,
    10,
    15,
    30,
    60,  # 1m
    120,  # 2m
    300,  # 5m
    600,  # 10m
    1200,  # 20m
    1800,  # 30m
    3600,  # 1h
    7200,  # 2h
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


#: Rolling window, in samples, that normalized power smooths over.
NORMALIZED_POWER_WINDOW = 30


def calculate_normalized_power(power_stream: list[int]) -> float | None:
    """Normalized power: the 4th-power mean of 30-second rolling averages.

    Assumes 1 Hz samples, like :func:`calculate_best_interval`.
    """
    window = NORMALIZED_POWER_WINDOW
    if not power_stream or len(power_stream) < window:
        return None

    current_sum = float(sum(power_stream[:window]))
    fourth_powers = (current_sum / window) ** 4
    count = 1
    for i in range(len(power_stream) - window):
        current_sum = current_sum - power_stream[i] + power_stream[i + window]
        fourth_powers += (current_sum / window) ** 4
        count += 1

    normalized = (fourth_powers / count) ** 0.25
    return normalized if normalized > 0 else None


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


def calculate_power_curve(power_stream: list[int]) -> dict[str, float]:
    """Calculate best average power for standard durations."""
    curve = {}
    for duration in POWER_CURVE_DURATIONS:
        best = calculate_best_interval(power_stream, duration)
        if best is not None:
            curve[str(duration)] = round(best, 1)
    return curve


def estimate_trimp_without_hr(sport_type: str, moving_time_seconds: int) -> float:
    """Estimate TRIMP for activities without heart rate data."""
    minutes = moving_time_seconds / 60.0
    rate = SPORT_TRIMP_PER_MIN.get(sport_type, DEFAULT_TRIMP_PER_MIN)
    return minutes * rate


async def recalculate_daily_metrics(
    session: AsyncSession,
    athlete_id: int,
    from_date: date | None = None,
    *,
    tz: ZoneInfo,
):
    """Recalculate CTL, ATL, TSB for all days from from_date to today.

    Days are local calendar days in ``tz`` (``athlete.timezone``), the same
    days body and nutrition rows and the MCP tools use.

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
        from_date = earliest.astimezone(tz).date()

    today = datetime.now(tz).date()

    # Get all visible activities grouped by local date
    activity_date = func.date(func.timezone(tz.key, Activity.start_date))
    result = await session.execute(
        select(
            activity_date.label("activity_date"),
            func.coalesce(func.sum(Activity.trimp), 0.0).label("daily_trimp"),
            func.count(Activity.id).label("activity_count"),
        )
        .where(Activity.athlete_id == athlete_id)
        .where(
            Activity.start_date >= datetime.combine(from_date, time.min, tzinfo=tz)
        )
        .where(ACTIVITY_VISIBLE)
        .group_by(activity_date)
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

        # TSB = start-of-day form (Coggan PMC model):
        # yesterday's fitness minus yesterday's fatigue
        tsb = ctl - atl

        # Update CTL and ATL with today's training load
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
