"""MCP tools for body measurements and nutrition.

Read the core ``body_measurements`` and ``daily_nutrition`` tables, visible
rows only, so a day reported by two providers is never counted twice. Dates
are local calendar days in ``athlete.timezone``, matching the ``day`` column.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import Select, func, select

from mcp_server.common import (
    check_range,
    clamp_limit,
    local_time,
    period_deltas,
    readonly_session,
    required_date,
    round_or_none,
)
from models import BodyMeasurement, DailyNutrition


def _visible_between(stmt: Select, model, start: date, end: date) -> Select:
    check_range(start, end)
    return stmt.where(
        model.superseded_by == None,  # noqa: E711
        model.day >= start,
        model.day <= end,
    )


# ── Body ────────────────────────────────────────────────────────────


BODY_DELTA_FIELDS = ["avg_weight_kg", "avg_fat_pct", "avg_muscle_mass_kg"]


def _body_dict(row: BodyMeasurement) -> dict[str, Any]:
    return {
        "measured_at": local_time(row.measured_at),
        "day": row.day.isoformat(),
        "weight_kg": round_or_none(row.weight_kg, 2),
        "fat_pct": round_or_none(row.fat_pct),
        "muscle_mass_kg": round_or_none(row.muscle_mass_kg, 2),
        "provider": row.provider,
        "device": row.device,
    }


async def _body_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        select(
            func.count(BodyMeasurement.id).label("count"),
            func.min(BodyMeasurement.weight_kg).label("min_kg"),
            func.max(BodyMeasurement.weight_kg).label("max_kg"),
            func.avg(BodyMeasurement.weight_kg).label("avg_kg"),
            func.avg(BodyMeasurement.fat_pct).label("avg_fat_pct"),
            func.avg(BodyMeasurement.muscle_mass_kg).label("avg_muscle_kg"),
        ),
        BodyMeasurement,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        select(BodyMeasurement).order_by(BodyMeasurement.measured_at.desc()).limit(1),
        BodyMeasurement,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "measurement_count": int(row.count or 0),
        "min_weight_kg": round_or_none(row.min_kg, 2),
        "max_weight_kg": round_or_none(row.max_kg, 2),
        "avg_weight_kg": round_or_none(row.avg_kg, 2),
        "avg_fat_pct": round_or_none(row.avg_fat_pct),
        "avg_muscle_mass_kg": round_or_none(row.avg_muscle_kg, 2),
        "latest": _body_dict(latest) if latest else None,
        # How far the last weigh-in sits from the period average.
        "latest_vs_avg_kg": (
            round_or_none(float(latest.weight_kg) - float(row.avg_kg), 2)
            if latest and row.avg_kg is not None
            else None
        ),
    }


# ── Nutrition ───────────────────────────────────────────────────────


NUTRITION_DELTA_FIELDS = [
    "avg_kcal",
    "avg_protein_g",
    "avg_carbs_g",
    "avg_fat_g",
    "avg_sugar_g",
    "avg_fiber_g",
]


def _nutrition_dict(row: DailyNutrition) -> dict[str, Any]:
    return {
        "day": row.day.isoformat(),
        "kcal": round_or_none(row.kcal),
        "protein_g": round_or_none(row.protein_g),
        "carbs_g": round_or_none(row.carbs_g),
        "sugar_g": round_or_none(row.sugar_g),
        "fat_g": round_or_none(row.fat_g),
        "fiber_g": round_or_none(row.fiber_g),
        "provider": row.provider,
    }


async def _nutrition_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        select(
            func.count(DailyNutrition.id).label("day_count"),
            func.avg(DailyNutrition.kcal).label("avg_kcal"),
            func.sum(DailyNutrition.kcal).label("total_kcal"),
            func.min(DailyNutrition.kcal).label("min_kcal"),
            func.max(DailyNutrition.kcal).label("max_kcal"),
            func.avg(DailyNutrition.protein_g).label("avg_protein_g"),
            func.avg(DailyNutrition.carbs_g).label("avg_carbs_g"),
            func.avg(DailyNutrition.fat_g).label("avg_fat_g"),
            func.avg(DailyNutrition.sugar_g).label("avg_sugar_g"),
            func.avg(DailyNutrition.fiber_g).label("avg_fiber_g"),
        ),
        DailyNutrition,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        select(DailyNutrition).order_by(DailyNutrition.day.desc()).limit(1),
        DailyNutrition,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "day_count": int(row.day_count or 0),
        "avg_kcal": round_or_none(row.avg_kcal),
        "total_kcal": round_or_none(row.total_kcal),
        "min_kcal": round_or_none(row.min_kcal),
        "max_kcal": round_or_none(row.max_kcal),
        "avg_protein_g": round_or_none(row.avg_protein_g),
        "avg_carbs_g": round_or_none(row.avg_carbs_g),
        "avg_fat_g": round_or_none(row.avg_fat_g),
        "avg_sugar_g": round_or_none(row.avg_sugar_g),
        "avg_fiber_g": round_or_none(row.avg_fiber_g),
        "latest_day": _nutrition_dict(latest) if latest else None,
    }


def register_record_tools(mcp: FastMCP) -> None:
    """Register body and nutrition tools on ``mcp``."""

    @mcp.tool()
    async def get_body_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize body measurements (weight min/max/avg, average fat % and
        muscle mass, latest weigh-in) for a local date range."""
        return await _body_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_body_measurements(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List body measurements (weight, fat %, muscle mass) in a local date
        range, newest first. At most ``limit`` rows (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            select(BodyMeasurement)
            .order_by(BodyMeasurement.measured_at.desc())
            .limit(clamp_limit(limit)),
            BodyMeasurement,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "measurements": [_body_dict(row) for row in rows],
        }

    @mcp.tool()
    async def get_latest_body_measurement() -> dict[str, Any]:
        """Return the most recent body measurement, whenever it was taken."""
        async with readonly_session() as session:
            row = (
                await session.scalars(
                    select(BodyMeasurement)
                    .where(BodyMeasurement.superseded_by == None)  # noqa: E711
                    .order_by(BodyMeasurement.measured_at.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return {"available": False}
        return {"available": True, "measurement": _body_dict(row)}

    @mcp.tool()
    async def compare_body_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average weight, fat %, and muscle mass between two local
        date ranges. Period A is the baseline; ``delta_b_minus_a`` is B minus A.
        """
        a = await _body_summary(
            start_date=required_date(period_a_start, "period_a_start"),
            end_date=required_date(period_a_end, "period_a_end"),
        )
        b = await _body_summary(
            start_date=required_date(period_b_start, "period_b_start"),
            end_date=required_date(period_b_end, "period_b_end"),
        )
        return {
            "period_a": a,
            "period_b": b,
            "delta_b_minus_a": period_deltas(a, b, BODY_DELTA_FIELDS),
        }

    @mcp.tool()
    async def get_nutrition_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize daily nutrition (kcal and macros) for a local date range."""
        return await _nutrition_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_daily_nutrition(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List daily nutrition totals in a local date range, newest first.
        At most ``limit`` rows (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            select(DailyNutrition)
            .order_by(DailyNutrition.day.desc())
            .limit(clamp_limit(limit)),
            DailyNutrition,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "days": [_nutrition_dict(row) for row in rows],
        }

    @mcp.tool()
    async def compare_nutrition_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average daily kcal and macros between two local date ranges.
        Period A is the baseline; ``delta_b_minus_a`` is B minus A."""
        a = await _nutrition_summary(
            start_date=required_date(period_a_start, "period_a_start"),
            end_date=required_date(period_a_end, "period_a_end"),
        )
        b = await _nutrition_summary(
            start_date=required_date(period_b_start, "period_b_start"),
            end_date=required_date(period_b_end, "period_b_end"),
        )
        return {
            "period_a": a,
            "period_b": b,
            "delta_b_minus_a": period_deltas(a, b, NUTRITION_DELTA_FIELDS),
        }
