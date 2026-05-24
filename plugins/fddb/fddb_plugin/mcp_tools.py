"""MCP tools for FDDB nutrition data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from trainingpulse_common import readonly_session

from fddb_plugin.models import DailyNutrition
from fddb_plugin.sync import get_status

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _parse_date(value: str | None, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date like YYYY-MM-DD") from exc


def _required_date(value: str, field_name: str) -> date:
    parsed = _parse_date(value, field_name)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def _apply_date_filters(
    stmt: Select,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Select:
    if start_date is not None:
        stmt = stmt.where(DailyNutrition.date >= start_date)
    if end_date is not None:
        stmt = stmt.where(DailyNutrition.date <= end_date)
    return stmt


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return min(max(1, value), MAX_LIMIT)


def _round(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _row_dict(row: DailyNutrition) -> dict[str, Any]:
    return {
        "date": row.date.isoformat(),
        "kcal": _round(row.kcal),
        "protein_g": _round(row.protein_g),
        "carbs_g": _round(row.carbs_g),
        "sugar_g": _round(row.sugar_g),
        "fat_g": _round(row.fat_g),
        "fiber_g": _round(row.fiber_g),
    }


async def _nutrition_summary(
    session_factory: async_sessionmaker,
    *,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    stmt = select(
        func.count(DailyNutrition.id).label("day_count"),
        func.avg(DailyNutrition.kcal).label("avg_kcal"),
        func.sum(DailyNutrition.kcal).label("total_kcal"),
        func.avg(DailyNutrition.protein_g).label("avg_protein_g"),
        func.avg(DailyNutrition.carbs_g).label("avg_carbs_g"),
        func.avg(DailyNutrition.fat_g).label("avg_fat_g"),
        func.avg(DailyNutrition.sugar_g).label("avg_sugar_g"),
        func.avg(DailyNutrition.fiber_g).label("avg_fiber_g"),
        func.min(DailyNutrition.kcal).label("min_kcal"),
        func.max(DailyNutrition.kcal).label("max_kcal"),
    )
    stmt = _apply_date_filters(stmt, start_date=start_date, end_date=end_date)
    latest_stmt = (
        select(DailyNutrition).order_by(DailyNutrition.date.desc()).limit(1)
    )
    latest_stmt = _apply_date_filters(
        latest_stmt, start_date=start_date, end_date=end_date
    )

    async with readonly_session(session_factory) as session:
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
        "latest_day": _row_dict(latest) if latest else None,
    }


def register_mcp_tools(mcp: FastMCP, session_factory: async_sessionmaker) -> None:
    @mcp.tool()
    async def get_nutrition_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize FDDB daily nutrition (kcal and macros) for an ISO date range."""
        return await _nutrition_summary(
            session_factory,
            start_date=_required_date(start_date, "start_date"),
            end_date=_required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_daily_nutrition(
        start_date: str,
        end_date: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List FDDB daily nutrition rows in a date range (newest first)."""
        start = _required_date(start_date, "start_date")
        end = _required_date(end_date, "end_date")
        lim = _limit(limit)
        stmt = (
            select(DailyNutrition).order_by(DailyNutrition.date.desc()).limit(lim)
        )
        stmt = _apply_date_filters(stmt, start_date=start, end_date=end)
        async with readonly_session(session_factory) as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "days": [_row_dict(r) for r in rows],
        }

    @mcp.tool()
    async def compare_nutrition_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average daily kcal between two date ranges (FDDB)."""
        a = await _nutrition_summary(
            session_factory,
            start_date=_required_date(period_a_start, "period_a_start"),
            end_date=_required_date(period_a_end, "period_a_end"),
        )
        b = await _nutrition_summary(
            session_factory,
            start_date=_required_date(period_b_start, "period_b_start"),
            end_date=_required_date(period_b_end, "period_b_end"),
        )
        avg_a = a.get("avg_kcal")
        avg_b = b.get("avg_kcal")
        delta = _round(avg_b - avg_a) if avg_a is not None and avg_b is not None else None
        return {"period_a": a, "period_b": b, "avg_kcal_delta": delta}

    @mcp.tool()
    async def get_fddb_sync_health() -> dict[str, Any]:
        """Return FDDB plugin sync status and stored day coverage."""
        async with readonly_session(session_factory) as session:
            status = await get_status(session)
        sync = status["sync"]
        return {
            "configured": status["configured"],
            "day_count": status["day_count"],
            "earliest_date": status["earliest_date"],
            "latest_date": status["latest_date"],
            "sync_running": sync.get("running", False),
            "last_sync_at": sync.get("last_run_at"),
            "last_upserted": sync.get("last_upserted", 0),
            "last_error": sync.get("last_error"),
        }
