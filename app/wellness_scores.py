"""Sleep and recovery scores TrainingPulse computes itself.

Providers' own scores (Garmin's sleep score, Body Battery, stress) are not
imported: they are not comparable across providers and most APIs do not
expose them. These scores use only the baseline every sleep and wellness
provider fills, read from the visible (deduplicated) rows, so they mean the
same whichever device recorded the night.

**Sleep score** (0–100), weighted parts, each scored 0–100:

=============  ======  =====================================================
Part           Weight  Full marks → zero
=============  ======  =====================================================
duration        35 %   asleep ≥ ``athlete.sleep_need_hours`` → half of it
efficiency      20 %   asleep / in bed ≥ 95 % → 75 %
restfulness     15 %   awake ≤ 10 min → 60 min
regularity      10 %   bedtime spread over the last 7 nights ≤ 15 → 90 min
physiology      20 %   mean of: heart-rate dip asleep vs. awake ≥ 15 % → 0 %,
                       and HRV ≥ the 30-day median → 80 % of it
=============  ======  =====================================================

Stage shares (deep, REM) are left out on purpose: devices detect stages
differently, so a stage-heavy score would jump when switching devices.

**Recovery score** (0–100): how far the night's HRV (60 %) and the day's
resting heart rate (40 %) are from the athlete's usual values, measured in
the athlete's own day-to-day variation. The usual value is the median of the
previous 30 days and the variation their standard deviation. A usual night
scores 70; each standard deviation better (higher HRV, lower resting heart
rate) adds 15 points and each one worse takes 15 away. The sleep score is not
part of it: it has its own scale and is shown beside it.

The score reads as a status: 85 or more is above normal, 60–84 normal, 40–59
below normal, under 40 well below normal.

A part without data is left out and the remaining weights are scaled up. The
sleep score's HRV comparison needs at least 7 of the previous 30 days; the
recovery score needs 14, because a standard deviation of fewer days is not
stable.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from catalog import DataKind
from models import DailyScores, DailyWellness, SleepSession

logger = logging.getLogger(__name__)

#: Kinds whose rows feed the scores; the sync rescores after each of them.
SCORED_KINDS = frozenset({DataKind.SLEEP, DataKind.WELLNESS, DataKind.INTRADAY})

SLEEP_WEIGHTS = {
    "duration": 35,
    "efficiency": 20,
    "restfulness": 15,
    "regularity": 10,
    "physiology": 20,
}
RECOVERY_WEIGHTS = {"hrv": 60, "resting_hr": 40}

#: The recovery score of a day that is exactly as usual.
RECOVERY_NORMAL = 70.0
#: Points for one standard deviation better or worse than usual.
RECOVERY_POINTS_PER_SD = 15.0
#: Lowest score of each status, best first.
RECOVERY_STATUSES = (
    (85.0, "above_normal"),
    (60.0, "normal"),
    (40.0, "below_normal"),
    (0.0, "well_below_normal"),
)
#: Days of the previous 30 that the recovery score needs.
RECOVERY_MIN_DAYS = 14
#: Smallest day-to-day variation the score assumes, so that a few very even
#: days do not turn the next small change into a large deviation: HRV as a
#: share of its median, resting heart rate in bpm.
HRV_MIN_SPREAD_RATIO = 0.05
RESTING_HR_MIN_SPREAD = 1.0

BASELINE_DAYS = 30
BASELINE_MIN_DAYS = 7
REGULARITY_NIGHTS = 7
REGULARITY_MIN_NIGHTS = 3
#: Heart-rate samples before falling asleep that count as "awake".
AWAKE_WINDOW = timedelta(hours=16)
#: About two hours of two-minute samples.
AWAKE_MIN_SAMPLES = 60


def scale(value: float | None, zero: float, full: float) -> float | None:
    """Linear 0–100 between ``zero`` and ``full`` (either direction), clamped."""
    if value is None:
        return None
    fraction = (value - zero) / (full - zero)
    return round(100.0 * min(1.0, max(0.0, fraction)), 1)


def weighted(parts: dict[str, dict], weights: dict[str, int]) -> float | None:
    """Weighted mean of the parts that have a score, weights renormalized."""
    present = {
        name: part["score"] for name, part in parts.items() if part["score"] is not None
    }
    total = sum(weights[name] for name in present)
    if not total:
        return None
    return round(
        sum(weights[name] * score for name, score in present.items()) / total, 1
    )


def _part(value: Any, score: float | None, weight: int) -> dict:
    return {"value": value, "score": score, "weight": weight}


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 1) if present else None


@dataclass(frozen=True)
class NightInputs:
    asleep_seconds: int | None
    in_bed_seconds: float
    awake_seconds: int | None
    #: Standard deviation of bedtimes over the recent nights, in minutes.
    bedtime_spread_min: float | None
    #: How much lower the sleeping heart rate is than the awake one, in %.
    hr_dip_pct: float | None
    #: Tonight's HRV divided by its 30-day median.
    hrv_ratio: float | None


def sleep_score(
    night: NightInputs, *, sleep_need_hours: float
) -> tuple[float | None, dict]:
    """Pure. ``None`` when the night has no time asleep to judge."""
    if not night.asleep_seconds:
        return None, {}
    need = sleep_need_hours * 3600.0
    efficiency = (
        night.asleep_seconds / night.in_bed_seconds
        if night.in_bed_seconds > 0
        else None
    )
    awake_min = None if night.awake_seconds is None else night.awake_seconds / 60.0
    physiology = _mean(
        [scale(night.hr_dip_pct, 0.0, 15.0), scale(night.hrv_ratio, 0.8, 1.0)]
    )
    parts = {
        "duration": _part(
            round(night.asleep_seconds / 3600.0, 2),
            scale(night.asleep_seconds / need, 0.5, 1.0),
            SLEEP_WEIGHTS["duration"],
        ),
        "efficiency": _part(
            None if efficiency is None else round(100 * efficiency, 1),
            scale(efficiency, 0.75, 0.95),
            SLEEP_WEIGHTS["efficiency"],
        ),
        "restfulness": _part(
            None if awake_min is None else round(awake_min, 1),
            scale(awake_min, 60.0, 10.0),
            SLEEP_WEIGHTS["restfulness"],
        ),
        "regularity": _part(
            night.bedtime_spread_min,
            scale(night.bedtime_spread_min, 90.0, 15.0),
            SLEEP_WEIGHTS["regularity"],
        ),
        "physiology": _part(
            {"hr_dip_pct": night.hr_dip_pct, "hrv_ratio": night.hrv_ratio},
            physiology,
            SLEEP_WEIGHTS["physiology"],
        ),
    }
    return weighted(parts, SLEEP_WEIGHTS), parts


@dataclass(frozen=True)
class Usual:
    """What a value usually is for the athlete: median and day-to-day spread."""

    median: float
    spread: float


def deviation_score(deviation: float | None) -> float | None:
    """0–100 for a deviation from usual in standard deviations, better = positive."""
    if deviation is None:
        return None
    score = RECOVERY_NORMAL + RECOVERY_POINTS_PER_SD * deviation
    return round(min(100.0, max(0.0, score)), 1)


def recovery_status(score: float | None) -> str | None:
    """The status a recovery score reads as, ``None`` without a score."""
    if score is None:
        return None
    return next(name for lowest, name in RECOVERY_STATUSES if score >= lowest)


def recovery_score(
    *,
    hrv_ms: float | None,
    hrv_usual: Usual | None,
    resting_hr: float | None,
    resting_hr_usual: Usual | None,
) -> tuple[float | None, dict]:
    """Pure. ``None`` when neither value can be compared with a usual one.

    The part values stay readable on their own: HRV as a ratio of its median,
    resting heart rate as bpm above (+) or below (-) its median.
    """
    hrv_ratio = hrv_deviation = rest_delta = rest_deviation = None
    if hrv_ms is not None and hrv_usual is not None and hrv_usual.median:
        hrv_ratio = round(hrv_ms / hrv_usual.median, 3)
        hrv_deviation = (hrv_ms - hrv_usual.median) / hrv_usual.spread
    if resting_hr is not None and resting_hr_usual is not None:
        rest_delta = round(resting_hr - resting_hr_usual.median, 1)
        # A lower resting heart rate is the better direction.
        rest_deviation = (resting_hr_usual.median - resting_hr) / resting_hr_usual.spread
    if hrv_deviation is None and rest_deviation is None:
        return None, {}
    parts = {
        "hrv": _part(hrv_ratio, deviation_score(hrv_deviation), RECOVERY_WEIGHTS["hrv"]),
        "resting_hr": _part(
            rest_delta,
            deviation_score(rest_deviation),
            RECOVERY_WEIGHTS["resting_hr"],
        ),
    }
    return weighted(parts, RECOVERY_WEIGHTS), parts


def _window(history: dict[date, float], day: date) -> list[float]:
    """The values in the ``BASELINE_DAYS`` before ``day``."""
    return [
        value
        for when, value in history.items()
        if day - timedelta(days=BASELINE_DAYS) <= when < day
    ]


def baseline(history: dict[date, float], day: date) -> float | None:
    """Median of the values in the ``BASELINE_DAYS`` before ``day``."""
    window = _window(history, day)
    if len(window) < BASELINE_MIN_DAYS:
        return None
    return statistics.median(window)


def usual(history: dict[date, float], day: date, *, min_spread: float) -> Usual | None:
    """Median and standard deviation of the ``BASELINE_DAYS`` before ``day``.

    ``None`` with fewer than ``RECOVERY_MIN_DAYS`` values. The spread is never
    below ``min_spread``.
    """
    window = _window(history, day)
    if len(window) < RECOVERY_MIN_DAYS:
        return None
    return Usual(statistics.median(window), max(statistics.stdev(window), min_spread))


def bedtime_spread(
    bedtimes: dict[date, datetime], day: date, tz: ZoneInfo
) -> float | None:
    """Standard deviation, in minutes, of the last week's local bedtimes.

    Bedtimes are measured from noon, so 23:30 and 00:30 are an hour apart
    rather than 23 hours.
    """
    recent = [
        start
        for when, start in bedtimes.items()
        if day - timedelta(days=REGULARITY_NIGHTS) < when <= day
    ]
    if len(recent) < REGULARITY_MIN_NIGHTS:
        return None
    minutes = []
    for start in recent:
        local = start.astimezone(tz)
        minutes.append((local.hour * 60 + local.minute - 720) % 1440)
    return round(statistics.pstdev(minutes), 1)


# ── Database pass ───────────────────────────────────────────────────


_SLEEP_HEART_RATE = text(
    """
    SELECT s.day,
           avg(asleep.value) AS sleep_avg,
           min(asleep.value) AS sleep_min,
           awake.avg_value AS awake_avg,
           awake.n AS awake_n
    FROM active_sleep_sessions s
    LEFT JOIN active_wellness_samples asleep
      ON asleep.metric = 'heart_rate'
     AND asleep.measured_at >= s.start_at AND asleep.measured_at < s.end_at
    LEFT JOIN LATERAL (
      SELECT avg(value) AS avg_value, count(*) AS n
      FROM active_wellness_samples w
      WHERE w.metric = 'heart_rate'
        AND w.measured_at >= s.start_at - make_interval(secs => :awake_seconds)
        AND w.measured_at < s.start_at
    ) awake ON true
    GROUP BY s.day, awake.avg_value, awake.n
    """
)


async def recompute_daily_scores(
    session: AsyncSession, *, tz: ZoneInfo, sleep_need_hours: float
) -> int:
    """Recompute ``daily_scores`` for every day with sleep or wellness data.

    Cheap enough to redo in full after every sync (a few thousand days), which
    keeps baselines right when older days arrive late.
    """
    sleeps = (
        await session.execute(
            select(
                SleepSession.day,
                SleepSession.start_at,
                SleepSession.end_at,
                SleepSession.asleep_seconds,
                SleepSession.awake_seconds,
            ).where(SleepSession.superseded_by == None)  # noqa: E711
        )
    ).all()
    wellness = (
        await session.execute(
            select(
                DailyWellness.day, DailyWellness.resting_hr, DailyWellness.hrv_ms
            ).where(
                DailyWellness.superseded_by == None  # noqa: E711
            )
        )
    ).all()
    heart = {
        row.day: row
        for row in (
            await session.execute(
                _SLEEP_HEART_RATE, {"awake_seconds": AWAKE_WINDOW.total_seconds()}
            )
        ).all()
    }

    sleep_by_day = {row.day: row for row in sleeps}
    bedtimes = {row.day: row.start_at for row in sleeps}
    hrv_history = {row.day: row.hrv_ms for row in wellness if row.hrv_ms is not None}
    rest_history = {
        row.day: row.resting_hr for row in wellness if row.resting_hr is not None
    }

    rows: list[dict] = []
    for day in sorted(set(sleep_by_day) | {row.day for row in wellness}):
        hrv_base = baseline(hrv_history, day)
        hrv_ratio = (
            round(hrv_history[day] / hrv_base, 3)
            if day in hrv_history and hrv_base
            else None
        )

        night = sleep_by_day.get(day)
        hr = heart.get(day)
        sleep_avg = (
            float(hr.sleep_avg) if hr is not None and hr.sleep_avg is not None else None
        )
        awake_avg = (
            float(hr.awake_avg)
            if hr is not None
            and hr.awake_avg is not None
            and hr.awake_n >= AWAKE_MIN_SAMPLES
            else None
        )
        score, parts = (None, {})
        if night is not None:
            score, parts = sleep_score(
                NightInputs(
                    asleep_seconds=night.asleep_seconds,
                    in_bed_seconds=(night.end_at - night.start_at).total_seconds(),
                    awake_seconds=night.awake_seconds,
                    bedtime_spread_min=bedtime_spread(bedtimes, day, tz),
                    hr_dip_pct=(
                        round(100.0 * (awake_avg - sleep_avg) / awake_avg, 1)
                        if sleep_avg is not None and awake_avg
                        else None
                    ),
                    hrv_ratio=hrv_ratio,
                ),
                sleep_need_hours=sleep_need_hours,
            )
        recovery, recovery_parts = recovery_score(
            hrv_ms=hrv_history.get(day),
            hrv_usual=usual(
                hrv_history, day, min_spread=HRV_MIN_SPREAD_RATIO * (hrv_base or 0.0)
            ),
            resting_hr=rest_history.get(day),
            resting_hr_usual=usual(
                rest_history, day, min_spread=RESTING_HR_MIN_SPREAD
            ),
        )
        rows.append(
            {
                "day": day,
                "sleep_avg_hr": None if sleep_avg is None else round(sleep_avg, 1),
                "sleep_min_hr": (
                    float(hr.sleep_min)
                    if hr is not None and hr.sleep_min is not None
                    else None
                ),
                "awake_avg_hr": None if awake_avg is None else round(awake_avg, 1),
                "sleep_score": score,
                "sleep_score_parts": parts or None,
                "recovery_score": recovery,
                "recovery_score_parts": recovery_parts or None,
            }
        )

    days = [row["day"] for row in rows]
    await session.execute(
        delete(DailyScores).where(DailyScores.day.not_in(days))
        if days
        else delete(DailyScores)
    )
    for offset in range(0, len(rows), 1000):
        stmt = pg_insert(DailyScores).values(rows[offset : offset + 1000])
        stmt = stmt.on_conflict_do_update(
            index_elements=["day"],
            set_={
                name: getattr(stmt.excluded, name) for name in rows[0] if name != "day"
            },
        )
        await session.execute(stmt)
    await session.commit()
    logger.info("Daily scores recomputed for %d days", len(rows))
    return len(rows)
