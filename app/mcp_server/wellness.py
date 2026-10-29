"""MCP tools for sleep, daily wellness, scores, and intraday samples.

Read the core ``sleep_sessions``, ``daily_wellness``, and ``wellness_samples``
tables, visible rows only, so a day reported by two providers is never
counted twice, plus ``daily_scores``, which TrainingPulse derives from them
the same way for every provider (see :mod:`wellness_scores`). A night
belongs to the local day it ends, matching the ``day`` column.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import Select, func, select

from mcp_server.common import (
    check_range,
    clamp_limit,
    downsample,
    local_day_bounds,
    local_time,
    period_deltas,
    readonly_session,
    required_date,
    round_or_none,
    seconds_to_hours,
    seconds_to_minutes,
)
from models import DailyScores, DailyWellness, SleepSession, WellnessSample
from providers.feeds import SAMPLE_METRICS, SAMPLE_UNITS

DEFAULT_MAX_POINTS = 300
MAX_POINTS = 2000


def _visible_between(stmt: Select, model, start: date, end: date) -> Select:
    check_range(start, end)
    return stmt.where(
        model.superseded_by == None,  # noqa: E711
        model.day >= start,
        model.day <= end,
    )


def _max_points(value: int | None) -> int:
    if value is None:
        return DEFAULT_MAX_POINTS
    return min(max(2, value), MAX_POINTS)


def _check_metric(metric: str) -> str:
    if metric not in SAMPLE_METRICS:
        raise ValueError(f"metric must be one of {', '.join(SAMPLE_METRICS)}")
    return metric


def _scores_dict(scores: DailyScores | None, which: str) -> dict[str, Any] | None:
    """One score with its parts, or None when it could not be computed."""
    if scores is None:
        return None
    value = getattr(scores, f"{which}_score")
    if value is None:
        return None
    return {
        "score": round_or_none(value),
        "parts": getattr(scores, f"{which}_score_parts"),
    }


# ── Sleep ───────────────────────────────────────────────────────────


SLEEP_DELTA_FIELDS = [
    "avg_asleep_hours",
    "avg_deep_minutes",
    "avg_rem_minutes",
    "avg_sleep_score",
    "avg_sleeping_hr",
]


def _sleep_dict(row: SleepSession, scores: DailyScores | None) -> dict[str, Any]:
    return {
        "day": row.day.isoformat(),
        "start": local_time(row.start_at),
        "end": local_time(row.end_at),
        "in_bed_hours": seconds_to_hours((row.end_at - row.start_at).total_seconds()),
        "asleep_hours": seconds_to_hours(row.asleep_seconds),
        "deep_minutes": seconds_to_minutes(row.deep_seconds),
        "light_minutes": seconds_to_minutes(row.light_seconds),
        "rem_minutes": seconds_to_minutes(row.rem_seconds),
        "awake_minutes": seconds_to_minutes(row.awake_seconds),
        "nap_minutes": seconds_to_minutes(row.nap_seconds),
        "has_stage_timeline": bool(row.stages),
        "sleeping_hr_avg": round_or_none(scores.sleep_avg_hr) if scores else None,
        "sleeping_hr_min": round_or_none(scores.sleep_min_hr) if scores else None,
        "sleep_score": _scores_dict(scores, "sleep"),
        "provider": row.provider,
    }


def _with_scores(stmt: Select) -> Select:
    return stmt.outerjoin(DailyScores, DailyScores.day == SleepSession.day)


async def _sleep_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        _with_scores(
            select(
                func.count(SleepSession.id).label("nights"),
                func.avg(SleepSession.asleep_seconds).label("asleep"),
                func.avg(SleepSession.deep_seconds).label("deep"),
                func.avg(SleepSession.light_seconds).label("light"),
                func.avg(SleepSession.rem_seconds).label("rem"),
                func.avg(SleepSession.awake_seconds).label("awake"),
                func.sum(SleepSession.nap_seconds).label("naps"),
                func.avg(DailyScores.sleep_score).label("score"),
                func.avg(DailyScores.sleep_avg_hr).label("sleeping_hr"),
            )
        ),
        SleepSession,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        _with_scores(select(SleepSession, DailyScores))
        .order_by(SleepSession.day.desc())
        .limit(1),
        SleepSession,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.execute(latest_stmt)).first()

    nights = int(row.nights or 0)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "nights": nights,
        "avg_asleep_hours": seconds_to_hours(row.asleep) if nights else None,
        "avg_deep_minutes": seconds_to_minutes(row.deep) if nights else None,
        "avg_light_minutes": seconds_to_minutes(row.light) if nights else None,
        "avg_rem_minutes": seconds_to_minutes(row.rem) if nights else None,
        "avg_awake_minutes": seconds_to_minutes(row.awake) if nights else None,
        "avg_sleep_score": round_or_none(row.score),
        "avg_sleeping_hr": round_or_none(row.sleeping_hr),
        "total_nap_minutes": seconds_to_minutes(row.naps),
        "latest_night": _sleep_dict(*latest) if latest else None,
    }


# ── Daily wellness ──────────────────────────────────────────────────


WELLNESS_DELTA_FIELDS = [
    "avg_resting_hr",
    "avg_hrv_ms",
    "avg_respiration",
    "avg_spo2",
    "avg_skin_temp_deviation_c",
    "avg_steps",
    "avg_recovery_score",
]


def _wellness_dict(row: DailyWellness, scores: DailyScores | None) -> dict[str, Any]:
    return {
        "day": row.day.isoformat(),
        "resting_hr": round_or_none(row.resting_hr),
        "hrv_ms": round_or_none(row.hrv_ms),
        "respiration": round_or_none(row.respiration),
        "spo2_avg": round_or_none(row.spo2_avg),
        "spo2_min": round_or_none(row.spo2_min),
        "skin_temp_deviation_c": round_or_none(row.skin_temp_deviation_c, 2),
        "steps": row.steps,
        "active_kcal": round_or_none(row.active_kcal),
        "total_kcal": round_or_none(row.total_kcal),
        "recovery_score": _scores_dict(scores, "recovery"),
        "provider": row.provider,
    }


def _wellness_with_scores(stmt: Select) -> Select:
    return stmt.outerjoin(DailyScores, DailyScores.day == DailyWellness.day)


async def _wellness_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        _wellness_with_scores(
            select(
                func.count(DailyWellness.id).label("days"),
                func.avg(DailyWellness.resting_hr).label("resting_hr"),
                func.min(DailyWellness.resting_hr).label("min_resting_hr"),
                func.max(DailyWellness.resting_hr).label("max_resting_hr"),
                func.avg(DailyWellness.hrv_ms).label("hrv"),
                func.avg(DailyWellness.respiration).label("respiration"),
                func.avg(DailyWellness.spo2_avg).label("spo2"),
                func.avg(DailyWellness.skin_temp_deviation_c).label("skin_temp"),
                func.avg(DailyWellness.steps).label("steps"),
                func.sum(DailyWellness.steps).label("total_steps"),
                func.avg(DailyScores.recovery_score).label("recovery"),
            )
        ),
        DailyWellness,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        _wellness_with_scores(select(DailyWellness, DailyScores))
        .order_by(DailyWellness.day.desc())
        .limit(1),
        DailyWellness,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.execute(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "day_count": int(row.days or 0),
        "avg_resting_hr": round_or_none(row.resting_hr),
        "min_resting_hr": round_or_none(row.min_resting_hr),
        "max_resting_hr": round_or_none(row.max_resting_hr),
        "avg_hrv_ms": round_or_none(row.hrv),
        "avg_respiration": round_or_none(row.respiration),
        "avg_spo2": round_or_none(row.spo2),
        "avg_skin_temp_deviation_c": round_or_none(row.skin_temp, 2),
        "avg_steps": round_or_none(row.steps, 0),
        "total_steps": int(row.total_steps) if row.total_steps is not None else None,
        "avg_recovery_score": round_or_none(row.recovery),
        "latest_day": _wellness_dict(*latest) if latest else None,
    }


# ── Samples ─────────────────────────────────────────────────────────


async def _samples(
    session, metric: str, lower: datetime, upper: datetime, max_points: int
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(WellnessSample.measured_at, WellnessSample.value)
            .where(
                WellnessSample.superseded_by == None,  # noqa: E711
                WellnessSample.metric == metric,
                WellnessSample.measured_at >= lower,
                WellnessSample.measured_at < upper,
            )
            .order_by(WellnessSample.measured_at)
        )
    ).all()
    values = [value for _, value in rows]
    sampled = downsample(list(rows), max_points)
    return {
        "unit": SAMPLE_UNITS[metric],
        "count": len(rows),
        "returned": len(sampled),
        "min": round_or_none(min(values)) if values else None,
        "max": round_or_none(max(values)) if values else None,
        "avg": round_or_none(sum(values) / len(values)) if values else None,
        "points": [[local_time(at), round_or_none(value)] for at, value in sampled],
    }


def register_wellness_tools(mcp: FastMCP) -> None:
    """Register sleep, wellness, and intraday tools on ``mcp``."""

    @mcp.tool()
    async def get_sleep_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize sleep for a local date range (nights are dated by the day
        they end): average time asleep, stage minutes, sleep score, sleeping
        heart rate, nap minutes, and the latest night."""
        return await _sleep_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_sleep(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List nights in a local date range, newest first, with stage
        durations, naps, sleeping heart rate, and the sleep score with its
        parts. At most ``limit`` nights (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            _with_scores(select(SleepSession, DailyScores))
            .order_by(SleepSession.day.desc())
            .limit(clamp_limit(limit)),
            SleepSession,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.execute(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "nights": [_sleep_dict(*row) for row in rows],
        }

    @mcp.tool()
    async def get_night(day: str, max_points: int | None = None) -> dict[str, Any]:
        """Return one night (dated by the day it ends): its summary and sleep
        score, the stage timeline when the provider has one, that day's
        wellness values and recovery score, and the heart rate, HRV, and SpO2
        samples between falling asleep and waking. Each series is downsampled
        to at most ``max_points`` (default 300, max 2000)."""
        night_day = required_date(day, "day")
        points = _max_points(max_points)
        async with readonly_session() as session:
            found = (
                await session.execute(
                    _with_scores(select(SleepSession, DailyScores)).where(
                        SleepSession.superseded_by == None,  # noqa: E711
                        SleepSession.day == night_day,
                    )
                )
            ).first()
            if found is None:
                return {"available": False, "day": night_day.isoformat()}
            night, scores = found
            wellness = (
                await session.scalars(
                    select(DailyWellness).where(
                        DailyWellness.superseded_by == None,  # noqa: E711
                        DailyWellness.day == night_day,
                    )
                )
            ).first()
            series = {
                metric: await _samples(
                    session, metric, night.start_at, night.end_at, points
                )
                for metric in SAMPLE_METRICS
            }
        return {
            "available": True,
            "night": _sleep_dict(night, scores),
            "stages": [
                {
                    "start": local_time(datetime.fromisoformat(stage["start"])),
                    "end": local_time(datetime.fromisoformat(stage["end"])),
                    "stage": stage["stage"],
                }
                for stage in night.stages or []
            ],
            "wellness": _wellness_dict(wellness, scores) if wellness else None,
            "series": {name: data for name, data in series.items() if data["count"]},
        }

    @mcp.tool()
    async def compare_sleep_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average sleep (time asleep, deep and REM minutes, sleep
        score, sleeping heart rate) between two local date ranges. Period A
        is the baseline; ``delta_b_minus_a`` is B minus A."""
        a = await _sleep_summary(
            start_date=required_date(period_a_start, "period_a_start"),
            end_date=required_date(period_a_end, "period_a_end"),
        )
        b = await _sleep_summary(
            start_date=required_date(period_b_start, "period_b_start"),
            end_date=required_date(period_b_end, "period_b_end"),
        )
        return {
            "period_a": a,
            "period_b": b,
            "delta_b_minus_a": period_deltas(a, b, SLEEP_DELTA_FIELDS),
        }

    @mcp.tool()
    async def get_wellness_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize daily wellness for a local date range: resting heart
        rate, overnight HRV, respiration, SpO2, skin temperature deviation,
        steps, and the recovery score, plus the latest day."""
        return await _wellness_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_daily_wellness(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List daily wellness values in a local date range, newest first:
        resting heart rate, HRV, respiration, SpO2, skin temperature
        deviation, steps, calories, and the recovery score with its parts.
        At most ``limit`` days (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            _wellness_with_scores(select(DailyWellness, DailyScores))
            .order_by(DailyWellness.day.desc())
            .limit(clamp_limit(limit)),
            DailyWellness,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.execute(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "days": [_wellness_dict(*row) for row in rows],
        }

    @mcp.tool()
    async def compare_wellness_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average resting heart rate, HRV, respiration, SpO2, skin
        temperature deviation, steps, and recovery score between two local
        date ranges. Period A is the baseline; ``delta_b_minus_a`` is B
        minus A."""
        a = await _wellness_summary(
            start_date=required_date(period_a_start, "period_a_start"),
            end_date=required_date(period_a_end, "period_a_end"),
        )
        b = await _wellness_summary(
            start_date=required_date(period_b_start, "period_b_start"),
            end_date=required_date(period_b_end, "period_b_end"),
        )
        return {
            "period_a": a,
            "period_b": b,
            "delta_b_minus_a": period_deltas(a, b, WELLNESS_DELTA_FIELDS),
        }

    @mcp.tool()
    async def get_intraday_samples(
        metric: str, start_date: str, end_date: str, max_points: int | None = None
    ) -> dict[str, Any]:
        """Return one intraday series for whole local days start_date..end_date:
        ``heart_rate`` (bpm, two-minute averages), ``hrv`` (ms, every 5 min
        while asleep), or ``spo2`` (%, during sleep, when the device records
        it). Downsampled to at most ``max_points`` (default 300, max 2000)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        check_range(start, end)
        lower, upper = local_day_bounds(start, end)
        async with readonly_session() as session:
            data = await _samples(
                session, _check_metric(metric), lower, upper, _max_points(max_points)
            )
        return {
            "metric": metric,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            **data,
        }
