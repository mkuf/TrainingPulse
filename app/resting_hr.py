"""The resting heart rate TrainingPulse calculates with.

Imported daily values win over ``athlete.rest_hr``, whichever provider they
come from: the median of the visible ``daily_wellness.resting_hr`` values in
the seven days up to the day in question. The median keeps one bad night (an
illness, a late meal) from moving it. Without imported values in that week,
the configured value applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import DailyWellness

#: Days, ending with the day in question, whose values form the median.
WINDOW_DAYS = 7


@dataclass(frozen=True)
class RestingHr:
    bpm: int
    #: ``"imported"`` or ``"config"``.
    source: str
    #: Imported days the median was taken over; 0 for the config value.
    days: int = 0
    #: Provider of the latest imported day used, if any.
    provider: str | None = None

    def as_payload(self) -> dict:
        return {
            "bpm": self.bpm,
            "source": self.source,
            "days": self.days,
            "provider": self.provider,
        }


async def resting_hr_on(
    session: AsyncSession, day: date, *, fallback: int, max_hr: int | None = None
) -> RestingHr:
    """Resting heart rate for ``day``; ``fallback`` is ``athlete.rest_hr``.

    An imported value at or above ``max_hr`` is ignored, since heart-rate
    reserve would be zero or negative.
    """
    visible = (
        DailyWellness.superseded_by == None,  # noqa: E711
        DailyWellness.resting_hr != None,  # noqa: E711
        DailyWellness.day > day - timedelta(days=WINDOW_DAYS),
        DailyWellness.day <= day,
    )
    median, count = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(DailyWellness.resting_hr),
                func.count(DailyWellness.id),
            ).where(*visible)
        )
    ).one()
    if not count or median is None or (max_hr is not None and median >= max_hr):
        return RestingHr(bpm=fallback, source="config")
    provider = await session.scalar(
        select(DailyWellness.provider)
        .where(*visible)
        .order_by(DailyWellness.day.desc())
        .limit(1)
    )
    return RestingHr(
        bpm=round(float(median)), source="imported", days=int(count), provider=provider
    )
