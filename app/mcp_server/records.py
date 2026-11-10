"""MCP tools for body measurements, blood pressure, and nutrition.

Read the core ``body_measurements``, ``blood_pressure_readings`` and
``daily_nutrition`` tables, visible
rows only, so a day reported by two providers is never counted twice. Dates
are local calendar days in ``athlete.timezone``, matching the ``day`` column.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
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
from models import BloodPressureReading, BodyMeasurement, DailyNutrition


def _visible_between(stmt: Select, model, start: date, end: date) -> Select:
    check_range(start, end)
    return stmt.where(
        model.superseded_by == None,  # noqa: E711
        model.day >= start,
        model.day <= end,
    )


# ── Body ────────────────────────────────────────────────────────────


BODY_DELTA_FIELDS = [
    "avg_weight_kg",
    "avg_fat_pct",
    "avg_muscle_mass_kg",
    "avg_bone_mass_kg",
    "avg_body_water_pct",
    "avg_visceral_fat",
]


def _body_dict(row: BodyMeasurement) -> dict[str, Any]:
    return {
        "measured_at": local_time(row.measured_at),
        "day": row.day.isoformat(),
        "weight_kg": round_or_none(row.weight_kg, 2),
        "fat_pct": round_or_none(row.fat_pct),
        "muscle_mass_kg": round_or_none(row.muscle_mass_kg, 2),
        "bone_mass_kg": round_or_none(row.bone_mass_kg, 2),
        "body_water_pct": round_or_none(row.body_water_pct),
        "visceral_fat": round_or_none(row.visceral_fat),
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
            func.avg(BodyMeasurement.bone_mass_kg).label("avg_bone_kg"),
            func.avg(BodyMeasurement.body_water_pct).label("avg_water_pct"),
            func.avg(BodyMeasurement.visceral_fat).label("avg_visceral_fat"),
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
        "avg_bone_mass_kg": round_or_none(row.avg_bone_kg, 2),
        "avg_body_water_pct": round_or_none(row.avg_water_pct),
        "avg_visceral_fat": round_or_none(row.avg_visceral_fat),
        "latest": _body_dict(latest) if latest else None,
        # How far the last weigh-in sits from the period average.
        "latest_vs_avg_kg": (
            round_or_none(float(latest.weight_kg) - float(row.avg_kg), 2)
            if latest and row.avg_kg is not None
            else None
        ),
    }


# ── Blood pressure ──────────────────────────────────────────────────


def _blood_pressure_dict(row: BloodPressureReading) -> dict[str, Any]:
    return {
        "measured_at": local_time(row.measured_at),
        "day": row.day.isoformat(),
        "systolic_mmhg": round_or_none(row.systolic_mmhg),
        "diastolic_mmhg": round_or_none(row.diastolic_mmhg),
        "pulse_bpm": round_or_none(row.pulse_bpm),
        "provider": row.provider,
        "device": row.device,
    }


async def _blood_pressure_summary(*, start_date: date, end_date: date) -> dict[str, Any]:
    stmt = _visible_between(
        select(
            func.count(BloodPressureReading.id).label("count"),
            func.avg(BloodPressureReading.systolic_mmhg).label("avg_sys"),
            func.min(BloodPressureReading.systolic_mmhg).label("min_sys"),
            func.max(BloodPressureReading.systolic_mmhg).label("max_sys"),
            func.avg(BloodPressureReading.diastolic_mmhg).label("avg_dia"),
            func.min(BloodPressureReading.diastolic_mmhg).label("min_dia"),
            func.max(BloodPressureReading.diastolic_mmhg).label("max_dia"),
            func.avg(BloodPressureReading.pulse_bpm).label("avg_pulse"),
        ),
        BloodPressureReading,
        start_date,
        end_date,
    )
    latest_stmt = _visible_between(
        select(BloodPressureReading)
        .order_by(BloodPressureReading.measured_at.desc())
        .limit(1),
        BloodPressureReading,
        start_date,
        end_date,
    )
    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "reading_count": int(row.count or 0),
        "avg_systolic_mmhg": round_or_none(row.avg_sys),
        "min_systolic_mmhg": round_or_none(row.min_sys),
        "max_systolic_mmhg": round_or_none(row.max_sys),
        "avg_diastolic_mmhg": round_or_none(row.avg_dia),
        "min_diastolic_mmhg": round_or_none(row.min_dia),
        "max_diastolic_mmhg": round_or_none(row.max_dia),
        "avg_pulse_bpm": round_or_none(row.avg_pulse),
        "latest": _blood_pressure_dict(latest) if latest else None,
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


def register_record_tools(mcp: MCPServer) -> None:
    """Register body, blood pressure, and nutrition tools on ``mcp``."""

    @mcp.tool()
    async def get_body_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize body measurements (weight min/max/avg, average fat %,
        muscle mass, bone mass, body water % and visceral fat rating, latest
        weigh-in) for a local date range. Fields a scale doesn't measure are
        null.

        Averages count every measurement once, so a day with several
        weigh-ins weighs more. ``latest_vs_avg_kg`` is the latest weight
        minus the period average. ``latest`` has the fields of
        list_body_measurements."""
        return await _body_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_body_measurements(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List body measurements (weight, fat %, muscle mass, bone mass, body
        water %, visceral fat rating) in a local date range, newest first. At
        most ``limit`` rows (default 25, max 100).

        ``fat_pct`` and ``body_water_pct`` are percent of body weight.
        ``visceral_fat`` is the scale's own rating without a unit; its range
        depends on the manufacturer, so compare it only for one ``device``."""
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
        """Return the most recent body measurement, whenever it was taken.
        ``available`` is false when none is stored; ``measurement`` has the
        fields of list_body_measurements."""
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
        """Compare average weight, fat %, muscle mass, bone mass, body water %
        and visceral fat rating between two local date ranges. Period A is the
        baseline; ``delta_b_minus_a`` is B minus A. Each period has the
        fields of get_body_summary.
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
    async def get_blood_pressure_summary(
        start_date: str, end_date: str
    ) -> dict[str, Any]:
        """Summarize blood pressure readings (systolic and diastolic
        average/min/max in mmHg, average pulse, latest reading) for a local
        date range. Averages count every reading once. ``latest`` has the
        fields of list_blood_pressure_readings."""
        return await _blood_pressure_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_blood_pressure_readings(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List blood pressure readings (systolic and diastolic in mmHg, the
        pulse during the reading in bpm) in a local date range, newest
        first. At most ``limit`` rows (default 25, max 100)."""
        start = required_date(start_date, "start_date")
        end = required_date(end_date, "end_date")
        stmt = _visible_between(
            select(BloodPressureReading)
            .order_by(BloodPressureReading.measured_at.desc())
            .limit(clamp_limit(limit)),
            BloodPressureReading,
            start,
            end,
        )
        async with readonly_session() as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "readings": [_blood_pressure_dict(row) for row in rows],
        }

    @mcp.tool()
    async def get_nutrition_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize daily nutrition (kcal and macros) for a local date range.

        Averages are per logged day: ``day_count`` is the number of days
        with a food log, and days without one are left out, not counted as
        zero. ``total_kcal``, ``min_kcal`` and ``max_kcal`` are the sum and
        the lowest and highest logged day. ``latest_day`` has the fields of
        list_daily_nutrition."""
        return await _nutrition_summary(
            start_date=required_date(start_date, "start_date"),
            end_date=required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_daily_nutrition(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List daily nutrition totals in a local date range, newest first:
        energy eaten in kcal, and protein, carbohydrates, sugar (part of the
        carbohydrates), fat and fiber in grams. A day holds what was logged,
        so a low value can be an incomplete log. At most ``limit`` rows
        (default 25, max 100)."""
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
        Period A is the baseline; ``delta_b_minus_a`` is B minus A. Each
        period has the fields of get_nutrition_summary."""
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
