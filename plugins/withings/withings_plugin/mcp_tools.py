"""MCP tools for Withings weight data."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from trainingpulse_common import readonly_session

from withings_plugin.models import WeightMeasurement

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


def _start_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _end_datetime(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.max, tzinfo=timezone.utc)


def _apply_date_filters(
    stmt: Select,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Select:
    start_at = _start_datetime(start_date)
    end_at = _end_datetime(end_date)
    if start_at is not None:
        stmt = stmt.where(WeightMeasurement.measured_at >= start_at)
    if end_at is not None:
        stmt = stmt.where(WeightMeasurement.measured_at <= end_at)
    return stmt


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return min(max(1, value), MAX_LIMIT)


def _round(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _iso_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _row_dict(row: WeightMeasurement) -> dict[str, Any]:
    return {
        "time": _iso_dt(row.measured_at),
        "weight_kg": _round(row.weight_kg),
        "fat_mass_pct": _round(row.fat_mass_pct, 1),
        "deviceid": row.deviceid,
    }


async def _weight_summary(
    session_factory: async_sessionmaker,
    *,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    stmt = select(
        func.count(WeightMeasurement.id).label("count"),
        func.min(WeightMeasurement.weight_kg).label("min_kg"),
        func.max(WeightMeasurement.weight_kg).label("max_kg"),
        func.avg(WeightMeasurement.weight_kg).label("avg_kg"),
    )
    stmt = _apply_date_filters(stmt, start_date=start_date, end_date=end_date)
    latest_stmt = (
        select(WeightMeasurement)
        .order_by(WeightMeasurement.measured_at.desc())
        .limit(1)
    )
    latest_stmt = _apply_date_filters(
        latest_stmt, start_date=start_date, end_date=end_date
    )

    async with readonly_session(session_factory) as session:
        row = (await session.execute(stmt)).one()
        latest = (await session.scalars(latest_stmt)).first()

    count = int(row.count or 0)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "measurement_count": count,
        "min_kg": _round(row.min_kg),
        "max_kg": _round(row.max_kg),
        "avg_kg": _round(row.avg_kg),
        "latest": _row_dict(latest) if latest else None,
        "trend_kg": (
            _round(float(latest.weight_kg) - float(row.avg_kg))
            if latest and row.avg_kg is not None
            else None
        ),
    }


def register_mcp_tools(mcp: FastMCP, session_factory: async_sessionmaker) -> None:
    @mcp.tool()
    async def get_weight_summary(start_date: str, end_date: str) -> dict[str, Any]:
        """Summarize Withings body-weight measurements for an ISO date range."""
        return await _weight_summary(
            session_factory,
            start_date=_required_date(start_date, "start_date"),
            end_date=_required_date(end_date, "end_date"),
        )

    @mcp.tool()
    async def list_weight_measurements(
        start_date: str,
        end_date: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """List Withings weight measurements in a date range (newest first)."""
        start = _required_date(start_date, "start_date")
        end = _required_date(end_date, "end_date")
        lim = _limit(limit)
        stmt = (
            select(WeightMeasurement)
            .order_by(WeightMeasurement.measured_at.desc())
            .limit(lim)
        )
        stmt = _apply_date_filters(stmt, start_date=start, end_date=end)
        async with readonly_session(session_factory) as session:
            rows = (await session.scalars(stmt)).all()
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "count": len(rows),
            "measurements": [_row_dict(r) for r in rows],
        }

    @mcp.tool()
    async def get_latest_weight() -> dict[str, Any]:
        """Return the most recent Withings weight measurement."""
        async with readonly_session(session_factory) as session:
            row = (
                await session.scalars(
                    select(WeightMeasurement)
                    .order_by(WeightMeasurement.measured_at.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return {"available": False}
        return {"available": True, "measurement": _row_dict(row)}

    @mcp.tool()
    async def compare_weight_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
    ) -> dict[str, Any]:
        """Compare average Withings weight between two date ranges."""
        a = await _weight_summary(
            session_factory,
            start_date=_required_date(period_a_start, "period_a_start"),
            end_date=_required_date(period_a_end, "period_a_end"),
        )
        b = await _weight_summary(
            session_factory,
            start_date=_required_date(period_b_start, "period_b_start"),
            end_date=_required_date(period_b_end, "period_b_end"),
        )
        avg_a = a.get("avg_kg")
        avg_b = b.get("avg_kg")
        delta = _round(avg_b - avg_a) if avg_a is not None and avg_b is not None else None
        return {"period_a": a, "period_b": b, "avg_kg_delta": delta}
