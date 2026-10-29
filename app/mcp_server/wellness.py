"""MCP tools for sleep, daily wellness, and intraday samples.

Read the core ``sleep_sessions``, ``daily_wellness``, and ``wellness_samples``
tables, visible rows only, so a day reported by two providers is never
counted twice. A night belongs to the local day it ends, matching the
``day`` column. Provider scores (sleep score, stress, Body Battery, HRV
status) are the provider's own and not comparable across providers.
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
from models import DailyWellness, SleepSession, WellnessSample
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


# ── Sleep ───────────────────────────────────────────────────────────


SLEEP_DELTA_FIELDS = [
    "avg_asleep_hours",
    "avg_deep_minutes",
    "avg_rem_minutes",
    "avg_score",
    "avg_heart_rate",
    "avg_respiration",
]


def _sleep_dict(row: SleepSession) -> dict[str, Any]:
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
        "awake_count": row.awake_count,
        "score": round_or_none(row.score),
        "avg_heart_rate": round_or_none(row.avg_heart_rate),
        "avg_respiration": round_or_none(row.avg_respiration),
        "min_respiration": round_or_none(row.min_respiration),
        "max_respiration": round_or_none(row.max_respiration),
        "avg_stress": round_or_none(row.avg_stress),
        "nap_minutes": seconds_to_minutes(row.nap_seconds),
        "provider": row.provider,
    }


async def _sleep_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        select(
            func.count(SleepSession.id).label("nights"),
            func.avg(SleepSession.asleep_seconds).label("asleep"),
            func.avg(SleepSession.deep_seconds).label("deep"),
            func.avg(SleepSession.light_seconds).label("light"),
            func.avg(SleepSession.rem_seconds).label("rem"),
            func.avg(SleepSession.awake_seconds).label("awake"),
            func.avg(SleepSession.score).label("score"),
            func.avg(SleepSession.avg_heart_rate).label("heart_rate"),
            func.avg(SleepSession.avg_respiration).label("respiration"),
            func.sum(SleepSession.nap_seconds).label("naps"),
        ),
        SleepSession,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        select(SleepSession).order_by(SleepSession.day.desc()).limit(1),
        SleepSession,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

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
        "avg_score": round_or_none(row.score),
        "avg_heart_rate": round_or_none(row.heart_rate),
        "avg_respiration": round_or_none(row.respiration),
        "total_nap_minutes": seconds_to_minutes(row.naps),
        "latest_night": _sleep_dict(latest) if latest else None,
    }


# ── Daily wellness ──────────────────────────────────────────────────


WELLNESS_DELTA_FIELDS = [
    "avg_resting_hr",
    "avg_hrv_ms",
    "avg_stress",
    "avg_body_battery_max",
    "avg_respiration",
    "avg_steps",
]


def _wellness_dict(row: DailyWellness) -> dict[str, Any]:
    return {
        "day": row.day.isoformat(),
        "resting_hr": round_or_none(row.resting_hr),
        "min_hr": round_or_none(row.min_hr),
        "max_hr": round_or_none(row.max_hr),
        "hrv_avg_ms": round_or_none(row.hrv_avg_ms),
        "hrv_5min_high_ms": round_or_none(row.hrv_5min_high_ms),
        "hrv_weekly_avg_ms": round_or_none(row.hrv_weekly_avg_ms),
        "hrv_baseline_ms": (
            [
                round_or_none(row.hrv_baseline_low_ms),
                round_or_none(row.hrv_baseline_high_ms),
            ]
            if row.hrv_baseline_low_ms is not None
            else None
        ),
        "hrv_status": row.hrv_status,
        "avg_stress": round_or_none(row.avg_stress),
        "max_stress": round_or_none(row.max_stress),
        "body_battery_min": round_or_none(row.body_battery_min),
        "body_battery_max": round_or_none(row.body_battery_max),
        "body_battery_charged": round_or_none(row.body_battery_charged),
        "body_battery_drained": round_or_none(row.body_battery_drained),
        "avg_respiration": round_or_none(row.avg_respiration),
        "steps": row.steps,
        "active_kcal": round_or_none(row.active_kcal),
        "total_kcal": round_or_none(row.total_kcal),
        "intensity_minutes": (
            None
            if row.moderate_intensity_minutes is None
            and row.vigorous_intensity_minutes is None
            else {
                "moderate": row.moderate_intensity_minutes,
                "vigorous": row.vigorous_intensity_minutes,
            }
        ),
        "provider": row.provider,
    }


async def _wellness_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        select(
            func.count(DailyWellness.id).label("days"),
            func.avg(DailyWellness.resting_hr).label("resting_hr"),
            func.min(DailyWellness.resting_hr).label("min_resting_hr"),
            func.max(DailyWellness.resting_hr).label("max_resting_hr"),
            func.avg(DailyWellness.hrv_avg_ms).label("hrv"),
            func.avg(DailyWellness.avg_stress).label("stress"),
            func.avg(DailyWellness.body_battery_max).label("battery_max"),
            func.avg(DailyWellness.body_battery_min).label("battery_min"),
            func.avg(DailyWellness.avg_respiration).label("respiration"),
            func.avg(DailyWellness.steps).label("steps"),
            func.sum(DailyWellness.steps).label("total_steps"),
        ),
        DailyWellness,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        select(DailyWellness).order_by(DailyWellness.day.desc()).limit(1),
        DailyWellness,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "day_count": int(row.days or 0),
        "avg_resting_hr": round_or_none(row.resting_hr),
        "min_resting_hr": round_or_none(row.min_resting_hr),
        "max_resting_hr": round_or_none(row.max_resting_hr),
        "avg_hrv_ms": round_or_none(row.hrv),
        "avg_stress": round_or_none(row.stress),
        "avg_body_battery_max": round_or_none(row.battery_max),
        "avg_body_battery_min": round_or_none(row.battery_min),
        "avg_respiration": round_or_none(row.respiration),
        "avg_steps": round_or_none(row.steps, 0),
        "total_steps": int(row.total_steps) if row.total_steps is not None else None,
        "latest_day": _wellness_dict(latest) if latest else None,
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
        they end): average time asleep, stage minutes, score, heart rate,
        respiration, nap minutes, and the latest night."""
        return await _sleep_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_sleep(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List nights in a local date range, newest first, with stage
        durations, score, heart rate, respiration, and stress. At most
        ``limit`` nights (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            select(SleepSession)
            .order_by(SleepSession.day.desc())
            .limit(clamp_limit(limit)),
            SleepSession,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "nights": [_sleep_dict(row) for row in rows],
        }

    @mcp.tool()
    async def get_night(day: str, max_points: int | None = None) -> dict[str, Any]:
        """Return one night (dated by the day it ends) with its summary, that
        day's wellness values, and the heart rate, HRV, and Body Battery
        samples recorded between falling asleep and waking. Each series is
        downsampled to at most ``max_points`` (default 300, max 2000). The
        provider has no sleep-stage timeline, only stage totals."""
        night_day = required_date(day, "day")
        points = _max_points(max_points)
        async with readonly_session() as session:
            night = (
                await session.scalars(
                    select(SleepSession).where(
                        SleepSession.superseded_by == None,  # noqa: E711
                        SleepSession.day == night_day,
                    )
                )
            ).first()
            if night is None:
                return {"available": False, "day": night_day.isoformat()}
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
            "night": _sleep_dict(night),
            "wellness": _wellness_dict(wellness) if wellness else None,
            "series": {name: data for name, data in series.items() if data["count"]},
        }

    @mcp.tool()
    async def compare_sleep_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average sleep (time asleep, deep and REM minutes, score,
        heart rate, respiration) between two local date ranges. Period A is
        the baseline; ``delta_b_minus_a`` is B minus A."""
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
        rate, overnight HRV, stress, Body Battery, respiration, and steps,
        plus the latest day."""
        return await _wellness_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_daily_wellness(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List daily wellness values in a local date range, newest first:
        resting/min/max heart rate, HRV (night average, baseline, status),
        stress, Body Battery, respiration, steps, calories, and intensity
        minutes. At most ``limit`` days (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            select(DailyWellness)
            .order_by(DailyWellness.day.desc())
            .limit(clamp_limit(limit)),
            DailyWellness,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "days": [_wellness_dict(row) for row in rows],
        }

    @mcp.tool()
    async def compare_wellness_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average resting heart rate, HRV, stress, Body Battery peak,
        respiration, and steps between two local date ranges. Period A is
        the baseline; ``delta_b_minus_a`` is B minus A."""
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
        ``heart_rate`` (bpm, every 2 min), ``hrv`` (ms, every 5 min while
        asleep), ``body_battery`` (a few points per day); ``stress``,
        ``respiration``, and ``spo2`` are reserved and empty for Garmin.
        Downsampled to at most ``max_points`` (default 300, max 2000)."""
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
