"""Helpers shared by the MCP tool modules.

Every date a tool accepts or returns is a local calendar day in
``athlete.timezone``, the same day the ``body_measurements.day``,
``daily_nutrition.day``, and ``daily_metrics.date`` columns use. Timestamps
are returned in that zone too, with their offset.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, AsyncIterator, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import config
from database import async_session

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

LOCAL_TZ = ZoneInfo(config.athlete.timezone)


@asynccontextmanager
async def readonly_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


# ── Arguments ───────────────────────────────────────────────────────


def parse_date(value: str | None, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date like YYYY-MM-DD") from exc


def required_date(value: str, field_name: str) -> date:
    parsed = parse_date(value, field_name)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def check_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError("start_date must not be after end_date")


def clamp_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return min(max(1, value), MAX_LIMIT)


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def local_day_bounds(
    start: date | None, end: date | None
) -> tuple[datetime | None, datetime | None]:
    """Instants bounding the local days ``start``..``end``, end exclusive.

    Filter timestamps with ``>= lower`` and ``< upper``.
    """
    lower = datetime.combine(start, time.min, tzinfo=LOCAL_TZ) if start else None
    upper = (
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
        if end
        else None
    )
    return lower, upper


# ── Output ──────────────────────────────────────────────────────────


def round_or_none(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def seconds_to_hours(value: Any) -> float:
    return round(float(value or 0) / 3600.0, 2)


def seconds_to_minutes(value: Any) -> float:
    return round(float(value or 0) / 60.0, 1)


def meters_to_km(value: Any) -> float:
    return round(float(value or 0) / 1000.0, 2)


def iso_day(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def local_time(value: datetime | None) -> str | None:
    """A stored instant as an ISO timestamp in ``athlete.timezone``."""
    if value is None:
        return None
    return value.astimezone(LOCAL_TZ).isoformat()


def local_day(value: datetime) -> date:
    return value.astimezone(LOCAL_TZ).date()


def period_deltas(
    period_a: Mapping[str, Any], period_b: Mapping[str, Any], fields: list[str]
) -> dict[str, float | None]:
    """Per-field change from period A to period B (``b - a``).

    Every ``compare_*`` tool uses this, so A is always the baseline.
    """
    deltas: dict[str, float | None] = {}
    for field in fields:
        a, b = period_a.get(field), period_b.get(field)
        deltas[field] = None if a is None or b is None else round(float(b) - float(a), 2)
    return deltas
