"""TrainingPulse MCP server.

Exposes read-only, training-aware tools over MCP for local/network clients.
"""

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import Select, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models import Activity, ActivityStream, AthleteSettings, DailyMetrics
from plugins.registry import load_plugins, register_plugin_mcp_tools

APP_NAME = "TrainingPulse MCP"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
DEFAULT_ALLOWED_HOSTS = "localhost:*,127.0.0.1:*"


def _csv_env(name: str, default: str) -> list[str]:
    return [
        value.strip()
        for value in os.environ.get(name, default).split(",")
        if value.strip()
    ]


def _allowed_origins(allowed_hosts: list[str]) -> list[str]:
    origins: list[str] = []
    for host in allowed_hosts:
        if host.startswith(("http://", "https://")):
            origins.append(host)
            continue
        origins.append(f"http://{host}")
        origins.append(f"https://{host}")
    return origins


MCP_ALLOWED_HOSTS = _csv_env("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)


mcp = FastMCP(
    APP_NAME,
    instructions=(
        "Read-only access to TrainingPulse activity, training-load, gear, "
        "and sync-health summaries. When Withings/FDDB plugins are enabled, "
        "weight and nutrition tools are also available. Do not request OAuth "
        "tokens or FDDB cookies."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=_allowed_origins(MCP_ALLOWED_HOSTS),
    ),
)


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


def _apply_activity_filters(
    stmt: Select,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    sport_type: str | None = None,
    gear_name: str | None = None,
) -> Select:
    start_at = _start_datetime(start_date)
    end_at = _end_datetime(end_date)

    if start_at is not None:
        stmt = stmt.where(Activity.start_date >= start_at)
    if end_at is not None:
        stmt = stmt.where(Activity.start_date <= end_at)
    if sport_type:
        stmt = stmt.where(func.lower(Activity.sport_type) == sport_type.lower())
    if gear_name:
        stmt = stmt.where(func.lower(Activity.gear_name) == gear_name.lower())

    return stmt


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    return min(max(1, value), MAX_LIMIT)


def _round(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _seconds_to_hours(value: Any) -> float:
    return round(float(value or 0) / 3600.0, 2)


def _meters_to_km(value: Any) -> float:
    return round(float(value or 0) / 1000.0, 2)


def _date(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _zone_seconds(value: dict | None) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(zone): int(seconds or 0) for zone, seconds in value.items()}


@asynccontextmanager
async def _readonly_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


async def _training_summary(
    *,
    start_date: date,
    end_date: date,
    sport_type: str | None = None,
) -> dict[str, Any]:
    stmt = select(
        func.count(Activity.id).label("activity_count"),
        func.coalesce(func.sum(Activity.distance), 0).label("distance_m"),
        func.coalesce(func.sum(Activity.moving_time), 0).label("moving_time_s"),
        func.coalesce(func.sum(Activity.trimp), 0).label("trimp"),
        func.coalesce(func.sum(Activity.total_elevation_gain), 0).label("elevation_m"),
        func.coalesce(func.sum(Activity.calories), 0).label("calories"),
        func.avg(Activity.average_heartrate).label("avg_hr"),
        func.avg(Activity.average_watts).label("avg_power"),
    )
    stmt = _apply_activity_filters(
        stmt,
        start_date=start_date,
        end_date=end_date,
        sport_type=sport_type,
    )

    async with _readonly_session() as session:
        row = (await session.execute(stmt)).one()

    moving_hours = _seconds_to_hours(row.moving_time_s)
    trimp = float(row.trimp or 0)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "sport_type": sport_type,
        "activity_count": int(row.activity_count or 0),
        "distance_km": _meters_to_km(row.distance_m),
        "moving_time_hours": moving_hours,
        "trimp": round(trimp, 1),
        "elevation_m": _round(row.elevation_m, 0),
        "calories": _round(row.calories, 0),
        "average_heart_rate": _round(row.avg_hr, 0),
        "average_power": _round(row.avg_power, 0),
        "trimp_per_hour": round(trimp / moving_hours, 1) if moving_hours else 0.0,
    }


@mcp.tool()
async def get_training_summary(
    start_date: str,
    end_date: str,
    sport_type: str | None = None,
) -> dict[str, Any]:
    """Summarize training volume and load for an ISO date range."""
    return await _training_summary(
        start_date=_required_date(start_date, "start_date"),
        end_date=_required_date(end_date, "end_date"),
        sport_type=sport_type,
    )


@mcp.tool()
async def compare_periods(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    sport_type: str | None = None,
) -> dict[str, Any]:
    """Compare two ISO date ranges side by side."""
    period_a = await _training_summary(
        start_date=_required_date(period_a_start, "period_a_start"),
        end_date=_required_date(period_a_end, "period_a_end"),
        sport_type=sport_type,
    )
    period_b = await _training_summary(
        start_date=_required_date(period_b_start, "period_b_start"),
        end_date=_required_date(period_b_end, "period_b_end"),
        sport_type=sport_type,
    )

    delta_fields = [
        "activity_count",
        "distance_km",
        "moving_time_hours",
        "trimp",
        "elevation_m",
        "calories",
        "trimp_per_hour",
    ]
    deltas = {
        field: round(float(period_a[field] or 0) - float(period_b[field] or 0), 2)
        for field in delta_fields
    }
    return {"period_a": period_a, "period_b": period_b, "delta_a_minus_b": deltas}


@mcp.tool()
async def list_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    sport_type: str | None = None,
    gear_name: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List recent or filtered activities with a capped result size."""
    result_limit = _limit(limit)
    stmt = select(Activity)
    stmt = _apply_activity_filters(
        stmt,
        start_date=_parse_date(start_date, "start_date"),
        end_date=_parse_date(end_date, "end_date"),
        sport_type=sport_type,
        gear_name=gear_name,
    )
    stmt = stmt.order_by(desc(Activity.start_date)).limit(result_limit)

    async with _readonly_session() as session:
        activities = (await session.scalars(stmt)).all()

    return {
        "limit": result_limit,
        "activities": [
            {
                "id": activity.id,
                "name": activity.name,
                "sport_type": activity.sport_type,
                "start_date": _date(activity.start_date),
                "distance_km": _meters_to_km(activity.distance),
                "moving_time_minutes": round(activity.moving_time / 60.0, 1),
                "trimp": _round(activity.trimp, 1),
                "elevation_m": _round(activity.total_elevation_gain, 0),
                "calories": _round(activity.calories, 0),
                "average_heart_rate": _round(activity.average_heartrate, 0),
                "average_power": _round(activity.average_watts, 0),
                "gear_name": activity.gear_name,
                "has_heart_rate": activity.has_heartrate,
                "has_power": activity.average_watts is not None,
                "detail_synced": activity.strava_detail_synced,
                "streams_synced": activity.synced_streams,
            }
            for activity in activities
        ],
    }


@mcp.tool()
async def get_activity_detail(activity_id: int) -> dict[str, Any]:
    """Get a single activity summary, zones, and stream availability."""
    stmt = (
        select(Activity, ActivityStream)
        .outerjoin(ActivityStream, ActivityStream.activity_id == Activity.id)
        .where(Activity.id == activity_id)
    )

    async with _readonly_session() as session:
        row = (await session.execute(stmt)).first()

    if row is None:
        return {"activity_id": activity_id, "found": False}

    activity, stream = row
    stream_types = []
    if stream is not None and stream.stream_types:
        stream_types = [item for item in stream.stream_types.split(",") if item]

    return {
        "found": True,
        "id": activity.id,
        "name": activity.name,
        "sport_type": activity.sport_type,
        "start_date": _date(activity.start_date),
        "distance_km": _meters_to_km(activity.distance),
        "elapsed_time_minutes": round(activity.elapsed_time / 60.0, 1),
        "moving_time_minutes": round(activity.moving_time / 60.0, 1),
        "trimp": _round(activity.trimp, 1),
        "elevation_m": _round(activity.total_elevation_gain, 0),
        "calories": _round(activity.calories, 0),
        "kilojoules": _round(activity.kilojoules, 0),
        "average_heart_rate": _round(activity.average_heartrate, 0),
        "max_heart_rate": _round(activity.max_heartrate, 0),
        "average_power": _round(activity.average_watts, 0),
        "weighted_average_power": _round(activity.weighted_average_watts, 0),
        "max_power": _round(activity.max_watts, 0),
        "best_20min_power": _round(activity.best_20min_power, 0),
        "gear_name": activity.gear_name,
        "device_name": activity.device_name,
        "hr_zone_seconds": _zone_seconds(activity.hr_zone_seconds),
        "power_zone_seconds": _zone_seconds(activity.power_zone_seconds),
        "detail_synced": activity.strava_detail_synced,
        "streams_synced": activity.synced_streams,
        "stream_types": stream_types,
        "stream_sample_count": stream.sample_count if stream is not None else 0,
    }


DEFAULT_MAX_POINTS = 500
ABSOLUTE_MAX_POINTS = 2000
DEFAULT_STREAM_TYPES = {"time", "heartrate", "watts"}
ALL_STREAM_TYPES = {
    "time",
    "heartrate",
    "watts",
    "cadence",
    "altitude",
    "velocity_smooth",
    "grade_smooth",
    "distance",
    "latlng",
    "temp",
}


def _downsample(data: list, max_points: int) -> list:
    """Downsample a list using nth-point decimation, always keeping first and last."""
    length = len(data)
    if length <= max_points:
        return data
    step = (length - 1) / (max_points - 1)
    indices = {0, length - 1}
    indices.update(int(round(i * step)) for i in range(max_points))
    sorted_indices = sorted(indices)[:max_points]
    return [data[i] for i in sorted_indices]


@mcp.tool()
async def get_activity_streams(
    activity_id: int,
    stream_types: str | None = None,
    max_points: int | None = None,
) -> dict[str, Any]:
    """Return raw time-series streams (HR, power, etc.) for an activity.

    Use this to analyze the full workout profile beyond zone buckets.
    Available stream types: time, heartrate, watts, cadence, altitude,
    velocity_smooth, grade_smooth, distance, latlng, temp.
    Defaults to time, heartrate, and watts if not specified.
    Large streams are downsampled to max_points (default 500, max 2000).
    """
    if max_points is None:
        max_points = DEFAULT_MAX_POINTS
    max_points = min(max(1, max_points), ABSOLUTE_MAX_POINTS)

    requested: set[str] = DEFAULT_STREAM_TYPES
    if stream_types:
        parsed = {s.strip().lower() for s in stream_types.split(",") if s.strip()}
        valid = parsed & ALL_STREAM_TYPES
        if not valid:
            return {
                "activity_id": activity_id,
                "error": f"No valid stream types in '{stream_types}'. "
                f"Available: {', '.join(sorted(ALL_STREAM_TYPES))}",
            }
        requested = valid
        requested.add("time")  # always include time axis

    stmt = select(ActivityStream).where(ActivityStream.activity_id == activity_id)

    async with _readonly_session() as session:
        stream = (await session.scalars(stmt)).first()

    if stream is None:
        return {"activity_id": activity_id, "found": False, "streams": {}}

    result_streams: dict[str, list] = {}
    sample_count = stream.sample_count
    returned_count = 0

    for stype in requested:
        raw = stream.data.get(stype)
        if raw is None:
            continue
        sampled = _downsample(raw, max_points)
        result_streams[stype] = sampled
        returned_count = max(returned_count, len(sampled))

    return {
        "activity_id": activity_id,
        "found": True,
        "sample_count": sample_count,
        "returned_count": returned_count,
        "downsampled": sample_count > returned_count,
        "streams": result_streams,
    }


@mcp.tool()
async def get_gear_usage(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Group activity volume and load by gear name."""
    gear_label = func.coalesce(Activity.gear_name, "Unspecified gear").label("gear_name")
    distance_m = func.coalesce(func.sum(Activity.distance), 0).label("distance_m")
    stmt = select(
        gear_label,
        func.count(Activity.id).label("activity_count"),
        distance_m,
        func.coalesce(func.sum(Activity.moving_time), 0).label("moving_time_s"),
        func.coalesce(func.sum(Activity.trimp), 0).label("trimp"),
        func.coalesce(func.sum(Activity.total_elevation_gain), 0).label("elevation_m"),
    )
    stmt = _apply_activity_filters(
        stmt,
        start_date=_parse_date(start_date, "start_date"),
        end_date=_parse_date(end_date, "end_date"),
    )
    stmt = stmt.group_by(gear_label).order_by(desc(distance_m))

    async with _readonly_session() as session:
        rows = (await session.execute(stmt)).all()

    return {
        "gear": [
            {
                "gear_name": row.gear_name,
                "activity_count": int(row.activity_count or 0),
                "distance_km": _meters_to_km(row.distance_m),
                "moving_time_hours": _seconds_to_hours(row.moving_time_s),
                "trimp": _round(row.trimp, 1),
                "elevation_m": _round(row.elevation_m, 0),
            }
            for row in rows
        ]
    }


@mcp.tool()
async def get_sync_health() -> dict[str, Any]:
    """Report sync completeness and common activity-data gaps."""
    activity_count = func.count(Activity.id)
    stmt = select(
        activity_count.label("activity_count"),
        activity_count.filter(Activity.strava_detail_synced == False).label(  # noqa: E712
            "missing_details"
        ),
        activity_count.filter(Activity.synced_streams == False).label(  # noqa: E712
            "missing_streams"
        ),
        activity_count.filter(Activity.has_heartrate == False).label(  # noqa: E712
            "without_heart_rate"
        ),
        activity_count.filter(Activity.average_watts.is_(None)).label("without_power"),
        func.max(Activity.start_date).label("latest_activity_date"),
    )
    settings_stmt = (
        select(AthleteSettings).order_by(desc(AthleteSettings.updated_at)).limit(1)
    )
    metrics_stmt = select(DailyMetrics).order_by(desc(DailyMetrics.date)).limit(1)

    async with _readonly_session() as session:
        row = (await session.execute(stmt)).one()
        athlete_settings = (await session.scalars(settings_stmt)).first()
        latest_metrics = (await session.scalars(metrics_stmt)).first()

    return {
        "activity_count": int(row.activity_count or 0),
        "missing_detail_sync": int(row.missing_details or 0),
        "missing_stream_sync": int(row.missing_streams or 0),
        "activities_without_heart_rate": int(row.without_heart_rate or 0),
        "activities_without_power": int(row.without_power or 0),
        "latest_activity_date": _date(row.latest_activity_date),
        "athlete_settings": {
            "available": athlete_settings is not None,
            "athlete_id": athlete_settings.athlete_id if athlete_settings else None,
            "max_hr": athlete_settings.max_hr if athlete_settings else None,
            "rest_hr": athlete_settings.rest_hr if athlete_settings else None,
            "ftp": athlete_settings.ftp if athlete_settings else None,
            "estimated_ftp": (
                athlete_settings.estimated_ftp if athlete_settings else None
            ),
            "updated_at": _date(
                athlete_settings.updated_at if athlete_settings else None
            ),
        },
        "latest_daily_metrics": {
            "available": latest_metrics is not None,
            "date": _date(latest_metrics.date if latest_metrics else None),
            "daily_trimp": _round(latest_metrics.daily_trimp, 1)
            if latest_metrics
            else None,
            "ctl": _round(latest_metrics.ctl, 1) if latest_metrics else None,
            "atl": _round(latest_metrics.atl, 1) if latest_metrics else None,
            "tsb": _round(latest_metrics.tsb, 1) if latest_metrics else None,
        },
    }


_plugin_instances = load_plugins(for_mcp=True)
register_plugin_mcp_tools(mcp, _plugin_instances)


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8001"))
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
