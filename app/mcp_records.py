"""MCP tools for body measurements and nutrition.

Read the core ``body_measurements`` and ``daily_nutrition`` tables, visible
rows only, so a day reported by two providers is never counted twice. Dates
are local calendar days in ``athlete.timezone``, matching the ``day`` column.
"""

from __future__ import annotations

from datetime import date
from typing import Any, AsyncContextManager, Callable

from mcp.server.fastmcp import FastMCP
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import BodyMeasurement, DailyNutrition

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

SessionFactory = Callable[[], AsyncContextManager[AsyncSession]]


def _required_date(value: str, field_name: str) -> date:
    if not value:
        raise ValueError(f"{field_name} is required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date like YYYY-MM-DD") from exc


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return min(max(1, value), MAX_LIMIT)


def _round(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _visible_between(stmt: Select, model, start: date, end: date) -> Select:
    return stmt.where(
        model.superseded_by == None,  # noqa: E711
        model.day >= start,
        model.day <= end,
    )


# ── Body ────────────────────────────────────────────────────────────


def _weight_dict(row: BodyMeasurement) -> dict[str, Any]:
    return {
        "time": row.measured_at.isoformat(),
        "day": row.day.isoformat(),
        "weight_kg": _round(row.weight_kg, 2),
        "fat_pct": _round(row.fat_pct),
        "muscle_mass_kg": _round(row.muscle_mass_kg, 2),
        "provider": row.provider,
        "device": row.device,
    }


async def _weight_summary(
    readonly: SessionFactory, *, start_date: date, end_date: date
) -> dict[str, Any]:
    stmt = _visible_between(
        select(
            func.count(BodyMeasurement.id).label("count"),
            func.min(BodyMeasurement.weight_kg).label("min_kg"),
            func.max(BodyMeasurement.weight_kg).label("max_kg"),
            func.avg(BodyMeasurement.weight_kg).label("avg_kg"),
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
    async with readonly() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "measurement_count": int(row.count or 0),
        "min_kg": _round(row.min_kg, 2),
        "max_kg": _round(row.max_kg, 2),
        "avg_kg": _round(row.avg_kg, 2),
        "latest": _weight_dict(latest) if latest else None,
        "trend_kg": (
            _round(float(latest.weight_kg) - float(row.avg_kg), 2)
            if latest and row.avg_kg is not None
            else None
        ),
    }


# ── Nutrition ───────────────────────────────────────────────────────


def _nutrition_dict(row: DailyNutrition) -> dict[str, Any]:
    return {
        "date": row.day.isoformat(),
        "kcal": _round(row.kcal),
        "protein_g": _round(row.protein_g),
        "carbs_g": _round(row.carbs_g),
        "sugar_g": _round(row.sugar_g),
        "fat_g": _round(row.fat_g),
        "fiber_g": _round(row.fiber_g),
        "provider": row.provider,
    }


async def _nutrition_summary(
    readonly: SessionFactory, *, start_date: date, end_date: date
) -> dict[str, Any]:
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
    async with readonly() as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "day_count": int(row.day_count or 0),
        "avg_kcal": _round(row.avg_kcal),
        "total_kcal": _round(row.total_kcal),
        "min_kcal": _round(row.min_kcal),
        "max_kcal": _round(row.max_kcal),
        "avg_protein_g": _round(row.avg_protein_g),
        "avg_carbs_g": _round(row.avg_carbs_g),
        "avg_fat_g": _round(row.avg_fat_g),
        "avg_sugar_g": _round(row.avg_sugar_g),
        "avg_fiber_g": _round(row.avg_fiber_g),
        "latest_day": _nutrition_dict(latest) if latest else None,
    }


async def record_coverage(session: AsyncSession) -> dict[str, Any]:
    """Visible row counts and date coverage, for ``get_sync_health``."""
    coverage: dict[str, Any] = {}
    for key, model in (("body", BodyMeasurement), ("nutrition", DailyNutrition)):
        row = (
            await session.execute(
                select(
                    func.count(model.id).filter(model.superseded_by == None),  # noqa: E711
                    func.count(model.id).filter(model.superseded_by != None),  # noqa: E711
                    func.min(model.day),
                    func.max(model.day),
                )
            )
        ).one()
        by_provider = dict(
            (
                await session.execute(
                    select(model.provider, func.count(model.id))
                    .where(model.superseded_by == None)  # noqa: E711
                    .group_by(model.provider)
                )
            ).all()
        )
        visible, hidden, earliest, latest = row
        coverage[key] = {
            "count": int(visible or 0),
            "by_provider": {name: int(count) for name, count in by_provider.items()},
            "superseded_duplicates": int(hidden or 0),
            "earliest_date": earliest.isoformat() if earliest else None,
            "latest_date": latest.isoformat() if latest else None,
        }
    return coverage


def register_record_tools(mcp: FastMCP, readonly: SessionFactory) -> None:
    """Register body and nutrition tools on ``mcp``."""

    @mcp.tool()
    async def get_weight_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize body-weight measurements for an ISO date range."""
        return await _weight_summary(
            readonly,
            start_date=_required_date(start_date, "start_date"),
            end_date=_required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_weight_measurements(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List body-weight measurements in a date range (newest first)."""
        start = _required_date(start_date, "start_date")
        end = _required_date(end_date, "end_date")
        stmt = _visible_between(
            select(BodyMeasurement)
            .order_by(BodyMeasurement.measured_at.desc())
            .limit(_limit(limit)),
            BodyMeasurement,
            start,
            end,
        )
        async with readonly() as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "measurements": [_weight_dict(row) for row in rows],
        }

    @mcp.tool()
    async def get_latest_weight() -> dict[str, Any]:
        """Return the most recent body-weight measurement."""
        async with readonly() as session:
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
        return {"available": True, "measurement": _weight_dict(row)}

    @mcp.tool()
    async def compare_weight_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average body weight between two date ranges."""
        a = await _weight_summary(
            readonly,
            start_date=_required_date(period_a_start, "period_a_start"),
            end_date=_required_date(period_a_end, "period_a_end"),
        )
        b = await _weight_summary(
            readonly,
            start_date=_required_date(period_b_start, "period_b_start"),
            end_date=_required_date(period_b_end, "period_b_end"),
        )
        avg_a, avg_b = a.get("avg_kg"), b.get("avg_kg")
        delta = _round(avg_b - avg_a, 2) if avg_a is not None and avg_b is not None else None
        return {"period_a": a, "period_b": b, "avg_kg_delta": delta}

    @mcp.tool()
    async def get_nutrition_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize daily nutrition (kcal and macros) for an ISO date range."""
        return await _nutrition_summary(
            readonly,
            start_date=_required_date(start_date, "start_date"),
            end_date=_required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_daily_nutrition(
        start_date: str, end_date: str, limit: int | None = None
    ) -> dict[str, Any]:
        """List daily nutrition rows in a date range (newest first)."""
        start = _required_date(start_date, "start_date")
        end = _required_date(end_date, "end_date")
        stmt = _visible_between(
            select(DailyNutrition)
            .order_by(DailyNutrition.day.desc())
            .limit(_limit(limit)),
            DailyNutrition,
            start,
            end,
        )
        async with readonly() as session:
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
        """Compare average daily kcal between two date ranges."""
        a = await _nutrition_summary(
            readonly,
            start_date=_required_date(period_a_start, "period_a_start"),
            end_date=_required_date(period_a_end, "period_a_end"),
        )
        b = await _nutrition_summary(
            readonly,
            start_date=_required_date(period_b_start, "period_b_start"),
            end_date=_required_date(period_b_end, "period_b_end"),
        )
        avg_a, avg_b = a.get("avg_kcal"), b.get("avg_kcal")
        delta = _round(avg_b - avg_a) if avg_a is not None and avg_b is not None else None
        return {"period_a": a, "period_b": b, "avg_kcal_delta": delta}
