"""The "Today" summary on the status page: the numbers worth a glance on a phone.

Reads only values the core already stores (sleep, daily wellness, the derived
sleep and recovery scores, CTL/ATL/TSB and form in percent), this week's
training load against its target, today's visible nutrition row, and
the average of the visible nutrition rows of the current week (Monday to
Sunday). Today is still being logged, so the
average covers the finished days only; on a Monday that is last week. Nothing
here is stored or derived anew.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from metrics import (
    FORM_STATUSES,
    WEEK_TARGET,
    WEEK_UPPER_LIMIT,
    form_pct,
    form_status,
    training_weeks,
)
from models import DailyMetrics, DailyNutrition, DailyScores, DailyWellness, SleepSession
from wellness_scores import RECOVERY_STATUSES, recovery_status

#: Older nights or metrics are not "today" any more; the tile shows nothing.
MAX_AGE_DAYS = 3


RECOVERY_STATUS_LABELS = {
    "above_normal": "Above normal",
    "normal": "Normal",
    "below_normal": "Below normal",
    "well_below_normal": "Well below normal",
}

FORM_STATUS_LABELS = {
    "transition": "Transition",
    "fresh": "Fresh",
    "neutral": "Neutral",
    "optimal_training": "Optimal training",
    "high_risk": "High risk",
}

WEEK_STATUS_LABELS = {
    "in_progress": "In progress",
    "below_target": "Below target",
    "on_target": "Target reached",
    "above_limit": "Above upper limit",
}

#: End of the week bar, in percent of the target. Load can go past it; the
#: marker then stays at the end.
WEEK_BAR_MAX = 160

#: Ends of the form bar, in percent. Form can go past them; the marker then
#: stays at the end.
FORM_BAR_MIN = -50
FORM_BAR_MAX = 40


def recovery_bands() -> tuple[tuple[str, str, int], ...]:
    """(status, label with its score range, width in points), lowest first.

    The bands of the tile's range bar, read from the score's own limits.
    """
    bands = []
    upper = 100
    for lowest, status in RECOVERY_STATUSES:
        low = int(lowest)
        top = upper if upper == 100 else upper - 1
        label = f"{RECOVERY_STATUS_LABELS[status]}: {low}–{top}"
        bands.append((status, label, upper - low))
        upper = low
    return tuple(reversed(bands))


def _pct(value: float) -> str:
    return f"+{value:g}" if value > 0 else f"{value:g}".replace("-", "\u2212")


def form_bands() -> tuple[tuple[str, str, int], ...]:
    """(status, label with its range, width in percent points), lowest first.

    The bands of the form bar, read from ``FORM_STATUSES`` and cut to the
    bar's ends.
    """
    bands = []
    upper: float | None = None
    for lowest, status in FORM_STATUSES:
        low = None if lowest == float("-inf") else lowest
        name = FORM_STATUS_LABELS[status]
        if upper is None:
            label = f"{name}: from {_pct(low)} %"
        elif low is None:
            label = f"{name}: below {_pct(upper)} %"
        else:
            label = f"{name}: {_pct(low)} to {_pct(upper)} %"
        width = (FORM_BAR_MAX if upper is None else upper) - (
            FORM_BAR_MIN if low is None else low
        )
        bands.append((status, label, int(width)))
        upper = low
    return tuple(reversed(bands))


def form_marker(pct: int) -> float:
    """Where ``pct`` sits on the form bar, in percent of its width."""
    clamped = min(FORM_BAR_MAX, max(FORM_BAR_MIN, pct))
    return round(100 * (clamped - FORM_BAR_MIN) / (FORM_BAR_MAX - FORM_BAR_MIN), 1)


def week_bands() -> tuple[tuple[str, str, int], ...]:
    """(band, label with its range, width in percent points), lowest first.

    The bands of the week bar in percent of the target, read from
    ``WEEK_TARGET`` and ``WEEK_UPPER_LIMIT``.
    """
    target = round(100 * WEEK_TARGET)
    upper = round(100 * WEEK_UPPER_LIMIT)
    return (
        ("below_target", f"Below target: under {target} %", target),
        ("on_target", f"Target reached: {target} to {upper} %", upper - target),
        ("above_limit", f"Above upper limit: over {upper} %", WEEK_BAR_MAX - upper),
    )


def week_marker(pct: int) -> float:
    """Where ``pct`` of the target sits on the week bar, in percent of its width."""
    clamped = min(WEEK_BAR_MAX, max(0, pct))
    return round(100 * clamped / WEEK_BAR_MAX, 1)


@dataclass(frozen=True)
class SleepView:
    day: date
    score: int | None
    asleep: str | None
    window: str
    deep: str | None
    rem: str | None
    light: str | None
    awake: str | None
    sleeping_hr: int | None
    #: (stage, seconds) in display order, for the night's stage bar. Empty
    #: without all of deep, REM and light.
    stages: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class RecoveryView:
    score: int | None
    #: The status the score falls in, e.g. "below_normal".
    status: str | None
    #: The same for reading, e.g. "Below normal".
    status_label: str | None
    hrv_ms: int | None
    #: HRV as a share of its 30-day median, in %.
    hrv_pct_of_usual: int | None
    resting_hr: int | None
    #: Resting heart rate minus its 30-day median, in bpm.
    resting_hr_delta: int | None

    @property
    def bands(self) -> tuple[tuple[str, str, int], ...]:
        return recovery_bands()


@dataclass(frozen=True)
class FormView:
    day: date
    fitness: int
    fatigue: int
    #: Start-of-day TSB in percent of the previous day's CTL; None without it.
    form_pct: int | None
    #: The band form falls in, e.g. "fresh".
    status: str | None
    #: The same for reading, e.g. "Fresh".
    status_label: str | None

    @property
    def bands(self) -> tuple[tuple[str, str, int], ...]:
        return form_bands()

    @property
    def marker(self) -> float | None:
        return None if self.form_pct is None else form_marker(self.form_pct)


@dataclass(frozen=True)
class WeekLoadView:
    start: date
    #: Last day counted (today).
    through: date
    #: TRIMP of the week so far.
    load: int
    #: 7 × CTL at the start of the week; None without enough history.
    target: int | None
    upper_limit: int | None
    pct_of_target: int | None
    #: TRIMP still needed to reach the target.
    remaining: int | None
    status: str | None
    status_label: str | None

    @property
    def bands(self) -> tuple[tuple[str, str, int], ...]:
        return week_bands()

    @property
    def band(self) -> str | None:
        """The band of the bar the load is in."""
        return "below_target" if self.status == "in_progress" else self.status

    @property
    def marker(self) -> float | None:
        return None if self.pct_of_target is None else week_marker(self.pct_of_target)


@dataclass(frozen=True)
class DayNutritionView:
    kcal: int | None
    protein_g: int | None
    carbs_g: int | None
    fat_g: int | None


@dataclass(frozen=True)
class WeekNutritionView:
    #: True on a Monday, when the average is over last week.
    last_week: bool
    start: date
    #: Last day in the average: yesterday, or Sunday of last week.
    through: date
    days_logged: int
    kcal: int | None
    protein_g: int | None
    carbs_g: int | None
    fat_g: int | None


@dataclass(frozen=True)
class TodayView:
    today: date
    sleep: SleepView | None
    recovery: RecoveryView | None
    form: FormView | None
    #: This week's training load against its target.
    week_load: WeekLoadView | None
    #: What is logged for today so far.
    nutrition_today: DayNutritionView | None
    nutrition: WeekNutritionView | None


def nutrition_window(today: date) -> tuple[date, date, bool]:
    """First and last day to average, and whether that is last week.

    The finished days of this Monday-to-Sunday week, or all of last week when
    today is Monday and no day of this week is finished yet.
    """
    monday = today - timedelta(days=today.weekday())
    if today == monday:
        return monday - timedelta(days=7), monday - timedelta(days=1), True
    return monday, today - timedelta(days=1), False


def duration(seconds: float | None) -> str | None:
    """``7:42`` for hours and minutes, ``None`` without a value."""
    if seconds is None:
        return None
    minutes = round(seconds / 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


def sleep_stages(
    deep: float | None, rem: float | None, light: float | None, awake: float | None
) -> tuple[tuple[str, float], ...]:
    """The stages of a night to draw as shares of a bar, in display order.

    A provider without stage data leaves them ``None``. A bar without one of
    the three sleep stages would overstate the others, so there is none.
    ``awake`` is optional because it is not part of the time asleep.
    """
    if deep is None or rem is None or light is None:
        return ()
    stages = (("deep", deep), ("rem", rem), ("light", light), ("awake", awake or 0))
    return tuple(stage for stage in stages if stage[1] > 0)


def _int(value: float | None) -> int | None:
    return None if value is None else round(value)


def _part_value(parts: dict | None, name: str) -> float | None:
    part = (parts or {}).get(name) or {}
    value = part.get("value")
    return value if isinstance(value, (int, float)) else None


async def today_view(session: AsyncSession, tz: ZoneInfo) -> TodayView:
    today = datetime.now(tz).date()
    oldest = today - timedelta(days=MAX_AGE_DAYS)

    night = (
        await session.execute(
            select(SleepSession, DailyScores)
            .outerjoin(DailyScores, DailyScores.day == SleepSession.day)
            .where(
                SleepSession.superseded_by == None,  # noqa: E711
                SleepSession.day >= oldest,
                SleepSession.day <= today,
            )
            .order_by(SleepSession.day.desc())
            .limit(1)
        )
    ).first()

    sleep = recovery = None
    if night is not None:
        row, scores = night
        sleep = SleepView(
            day=row.day,
            score=_int(scores.sleep_score) if scores else None,
            asleep=duration(row.asleep_seconds),
            window=(
                f"{row.start_at.astimezone(tz):%H:%M}–{row.end_at.astimezone(tz):%H:%M}"
            ),
            deep=duration(row.deep_seconds),
            rem=duration(row.rem_seconds),
            light=duration(row.light_seconds),
            awake=duration(row.awake_seconds),
            sleeping_hr=_int(scores.sleep_avg_hr) if scores else None,
            stages=sleep_stages(
                row.deep_seconds, row.rem_seconds, row.light_seconds, row.awake_seconds
            ),
        )
        wellness = await session.scalar(
            select(DailyWellness).where(
                DailyWellness.superseded_by == None,  # noqa: E711
                DailyWellness.day == row.day,
            )
        )
        parts = scores.recovery_score_parts if scores else None
        hrv_ratio = _part_value(parts, "hrv")
        recovery_score = _int(scores.recovery_score) if scores else None
        recovery = RecoveryView(
            score=recovery_score,
            status=recovery_status(recovery_score),
            status_label=RECOVERY_STATUS_LABELS.get(recovery_status(recovery_score)),
            hrv_ms=_int(wellness.hrv_ms) if wellness else None,
            hrv_pct_of_usual=None if hrv_ratio is None else round(100 * hrv_ratio),
            resting_hr=_int(wellness.resting_hr) if wellness else None,
            resting_hr_delta=_int(_part_value(parts, "resting_hr")),
        )
        if recovery == RecoveryView(None, None, None, None, None, None, None):
            recovery = None

    metrics = await session.scalar(
        select(DailyMetrics)
        .where(DailyMetrics.date >= oldest, DailyMetrics.date <= today)
        .order_by(DailyMetrics.date.desc())
        .limit(1)
    )
    form = None
    if metrics is not None:
        prior_ctl = await session.scalar(
            select(DailyMetrics.ctl).where(
                DailyMetrics.athlete_id == metrics.athlete_id,
                DailyMetrics.date == metrics.date - timedelta(days=1),
            )
        )
        pct = form_pct(metrics.tsb, prior_ctl)
        status = form_status(pct)
        form = FormView(
            day=metrics.date,
            fitness=round(metrics.ctl),
            fatigue=round(metrics.atl),
            form_pct=None if pct is None else round(pct),
            status=status,
            status_label=FORM_STATUS_LABELS.get(status),
        )

    week_load = None
    weeks = await training_weeks(session, today, today, today)
    if weeks:
        this_week = weeks[-1]
        pct = this_week.pct_of_target
        week_load = WeekLoadView(
            start=this_week.start,
            through=this_week.through,
            load=round(this_week.load),
            target=_int(this_week.target),
            upper_limit=_int(this_week.upper_limit),
            pct_of_target=_int(pct),
            remaining=_int(this_week.remaining),
            status=this_week.status,
            status_label=WEEK_STATUS_LABELS.get(this_week.status),
        )

    logged_today = await session.scalar(
        select(DailyNutrition).where(
            DailyNutrition.superseded_by == None,  # noqa: E711
            DailyNutrition.day == today,
        )
    )
    nutrition_today = (
        DayNutritionView(
            kcal=_int(logged_today.kcal),
            protein_g=_int(logged_today.protein_g),
            carbs_g=_int(logged_today.carbs_g),
            fat_g=_int(logged_today.fat_g),
        )
        if logged_today is not None
        else None
    )

    start, through, last_week = nutrition_window(today)
    week = (
        await session.execute(
            select(
                func.count(DailyNutrition.id),
                func.avg(DailyNutrition.kcal),
                func.avg(DailyNutrition.protein_g),
                func.avg(DailyNutrition.carbs_g),
                func.avg(DailyNutrition.fat_g),
            ).where(
                DailyNutrition.superseded_by == None,  # noqa: E711
                DailyNutrition.day >= start,
                DailyNutrition.day <= through,
            )
        )
    ).one()
    days_logged = int(week[0] or 0)
    nutrition = (
        WeekNutritionView(
            last_week=last_week,
            start=start,
            through=through,
            days_logged=days_logged,
            kcal=_int(week[1]),
            protein_g=_int(week[2]),
            carbs_g=_int(week[3]),
            fat_g=_int(week[4]),
        )
        if days_logged
        else None
    )

    return TodayView(
        today=today,
        sleep=sleep,
        recovery=recovery,
        form=form,
        week_load=week_load,
        nutrition_today=nutrition_today,
        nutrition=nutrition,
    )
