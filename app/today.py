"""The "Today" summary on the status page: the numbers worth a glance on a phone.

Reads only values the core already stores (sleep, daily wellness, the derived
sleep and recovery scores, CTL/ATL/TSB), today's visible nutrition row, and
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

from models import DailyMetrics, DailyNutrition, DailyScores, DailyWellness, SleepSession

#: Older nights or metrics are not "today" any more; the tile shows nothing.
MAX_AGE_DAYS = 3


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
    hrv_ms: int | None
    #: HRV as a share of its 30-day median, in %.
    hrv_pct_of_usual: int | None
    resting_hr: int | None
    #: Resting heart rate minus its 30-day median, in bpm.
    resting_hr_delta: int | None


@dataclass(frozen=True)
class FormView:
    day: date
    fitness: int
    fatigue: int
    form: int


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
        recovery = RecoveryView(
            score=_int(scores.recovery_score) if scores else None,
            hrv_ms=_int(wellness.hrv_ms) if wellness else None,
            hrv_pct_of_usual=None if hrv_ratio is None else round(100 * hrv_ratio),
            resting_hr=_int(wellness.resting_hr) if wellness else None,
            resting_hr_delta=_int(_part_value(parts, "resting_hr")),
        )
        if recovery == RecoveryView(None, None, None, None, None):
            recovery = None

    metrics = await session.scalar(
        select(DailyMetrics)
        .where(DailyMetrics.date >= oldest, DailyMetrics.date <= today)
        .order_by(DailyMetrics.date.desc())
        .limit(1)
    )
    form = (
        FormView(
            day=metrics.date,
            fitness=round(metrics.ctl),
            fatigue=round(metrics.atl),
            form=round(metrics.tsb),
        )
        if metrics is not None
        else None
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
        nutrition_today=nutrition_today,
        nutrition=nutrition,
    )
