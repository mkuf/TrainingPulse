"""TrainingPulse MCP server.

Exposes read-only, training-aware tools over MCP for local/network clients.
Activity, training-load, athlete, and coverage tools live here; body and
nutrition tools in :mod:`mcp_records`. Shared conventions (local days,
``b - a`` deltas) are in :mod:`mcp_common`.
"""

from datetime import timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import Select, desc, func, select

from config import config
from mcp_common import (
    check_range,
    clamp_limit,
    iso_day,
    local_day,
    local_day_bounds,
    local_time,
    meters_to_km,
    parse_date,
    period_deltas,
    readonly_session,
    required_date,
    round_or_none,
    seconds_to_hours,
    seconds_to_minutes,
    today_local,
)
from mcp_records import register_record_tools
from metrics import get_hr_zone_boundaries, get_power_zone_boundaries
from models import (
    ACTIVITY_VISIBLE,
    Activity,
    ActivityStream,
    AthleteSettings,
    BodyMeasurement,
    DailyMetrics,
    DailyNutrition,
)

APP_NAME = "TrainingPulse MCP"


def _allowed_origins(allowed_hosts: list[str]) -> list[str]:
    origins: list[str] = []
    for host in allowed_hosts:
        if host.startswith(("http://", "https://")):
            origins.append(host)
            continue
        origins.append(f"http://{host}")
        origins.append(f"https://{host}")
    return origins


mcp = FastMCP(
    APP_NAME,
    instructions=(
        "Read-only access to TrainingPulse: activities, training load "
        "(CTL/ATL/TSB), the athlete profile, gear, body measurements, "
        "nutrition, and data coverage. Data from several providers is "
        "deduplicated, so each workout or day is counted once. All dates are "
        f"local calendar days in {config.athlete.timezone}, and date ranges "
        "include both ends. compare_* tools treat period A as the baseline and "
        "report deltas as B minus A. Credentials are never exposed."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.mcp.allowed_hosts,
        allowed_origins=_allowed_origins(config.mcp.allowed_hosts),
    ),
)


# ── Activities ──────────────────────────────────────────────────────


def _apply_activity_filters(
    stmt: Select,
    *,
    start_date=None,
    end_date=None,
    sport_type: str | None = None,
    gear_name: str | None = None,
) -> Select:
    # The same workout can arrive from several sources. Only the row that
    # wins on precedence is visible, otherwise every aggregate here would
    # double-count duplicated activities. Activities gone upstream are hidden.
    stmt = stmt.where(ACTIVITY_VISIBLE)
    lower, upper = local_day_bounds(start_date, end_date)
    if lower is not None:
        stmt = stmt.where(Activity.start_date >= lower)
    if upper is not None:
        stmt = stmt.where(Activity.start_date < upper)
    if sport_type:
        stmt = stmt.where(func.lower(Activity.sport_type) == sport_type.lower())
    if gear_name:
        stmt = stmt.where(func.lower(Activity.gear_name) == gear_name.lower())
    return stmt


def _activity_row(activity: Activity) -> dict[str, Any]:
    return {
        "id": activity.id,
        "name": activity.name,
        "sport_type": activity.sport_type,
        "start_time": local_time(activity.start_date),
        "day": local_day(activity.start_date).isoformat(),
        "distance_km": meters_to_km(activity.distance),
        "moving_time_minutes": seconds_to_minutes(activity.moving_time),
        "trimp": round_or_none(activity.trimp, 1),
        "elevation_m": round_or_none(activity.total_elevation_gain, 0),
        "calories": round_or_none(activity.calories, 0),
        "average_heart_rate": round_or_none(activity.average_heartrate, 0),
        "average_power": round_or_none(activity.average_watts, 0),
        "gear_name": activity.gear_name,
        "provider": activity.source,
        "detail_synced": activity.detail_synced,
        "streams_synced": activity.synced_streams,
    }


def _zone_seconds(value: dict | None) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(zone): int(seconds or 0) for zone, seconds in value.items()}


@mcp.tool()
async def list_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    sport_type: str | None = None,
    gear_name: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List activities, newest first, optionally filtered by local date range,
    sport type, or gear. Returns at most ``limit`` rows (default 25, max 100).
    """
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date")
    check_range(start, end)
    result_limit = clamp_limit(limit)
    stmt = _apply_activity_filters(
        select(Activity),
        start_date=start,
        end_date=end,
        sport_type=sport_type,
        gear_name=gear_name,
    )
    stmt = stmt.order_by(desc(Activity.start_date)).limit(result_limit)

    async with readonly_session() as session:
        activities = (await session.scalars(stmt)).all()

    return {
        "limit": result_limit,
        "count": len(activities),
        "activities": [_activity_row(activity) for activity in activities],
    }


@mcp.tool()
async def get_activity(activity_id: str) -> dict[str, Any]:
    """Get one activity: summary, HR/power zone seconds, power curve, stream
    availability, and the training load of the day it falls on.

    Activity ids are provider-prefixed strings, e.g. ``icu_i55610271``,
    ``strava_123``, ``ghealth_<id>``, or ``hammerhead_<id>``. A duplicate
    hidden behind another provider's copy is still returned when asked for by
    id; ``superseded_by`` then names the visible copy. So is an activity the
    provider no longer serves; ``deleted_upstream_at`` then says when that was
    noticed.
    """
    stmt = (
        select(Activity, ActivityStream)
        .outerjoin(ActivityStream, ActivityStream.activity_id == Activity.id)
        .where(Activity.id == activity_id)
    )

    async with readonly_session() as session:
        row = (await session.execute(stmt)).first()
        if row is None:
            return {"activity_id": activity_id, "found": False}
        activity, stream = row
        day = local_day(activity.start_date)
        metrics_rows = (
            await session.scalars(
                select(DailyMetrics).where(
                    DailyMetrics.athlete_id == activity.athlete_id,
                    DailyMetrics.date.in_([day - timedelta(days=1), day]),
                )
            )
        ).all()

    stream_types = []
    if stream is not None and stream.stream_types:
        stream_types = [item for item in stream.stream_types.split(",") if item]

    by_date = {m.date: m for m in metrics_rows}
    prior, current = by_date.get(day - timedelta(days=1)), by_date.get(day)
    day_training_load = None
    if current is not None:
        # CTL/ATL are daily values: with several workouts on one day, the
        # "after" values include all of them, not just this activity.
        day_training_load = {
            "day": day.isoformat(),
            "day_trimp": round_or_none(current.daily_trimp, 1),
            "day_activity_count": current.activity_count,
            "ctl_before": round_or_none(prior.ctl, 1) if prior else None,
            "ctl_after": round_or_none(current.ctl, 1),
            "atl_before": round_or_none(prior.atl, 1) if prior else None,
            "atl_after": round_or_none(current.atl, 1),
            "tsb_before": round_or_none(current.tsb, 1),
            "tsb_after": round_or_none(current.ctl - current.atl, 1),
        }

    average_speed_kmh = (
        round(activity.average_speed * 3.6, 1)
        if activity.average_speed is not None
        else None
    )
    return {
        "found": True,
        **_activity_row(activity),
        "description": activity.description,
        "elapsed_time_minutes": seconds_to_minutes(activity.elapsed_time),
        "average_speed_kmh": average_speed_kmh,
        "kilojoules": round_or_none(activity.kilojoules, 0),
        "max_heart_rate": round_or_none(activity.max_heartrate, 0),
        "weighted_average_power": round_or_none(activity.weighted_average_watts, 0),
        "max_power": round_or_none(activity.max_watts, 0),
        "best_20min_power": round_or_none(activity.best_20min_power, 0),
        "provider_training_load": round_or_none(activity.training_load, 0),
        "device_name": activity.device_name,
        "hr_zone_seconds": _zone_seconds(activity.hr_zone_seconds),
        "power_zone_seconds": _zone_seconds(activity.power_zone_seconds),
        # Best average watts keyed by duration in seconds.
        "power_curve": activity.power_curve or {},
        "superseded_by": activity.superseded_by,
        "deleted_upstream_at": (
            activity.deleted_upstream_at.isoformat()
            if activity.deleted_upstream_at
            else None
        ),
        "stream_types": stream_types,
        "stream_sample_count": stream.sample_count if stream is not None else 0,
        "day_training_load": day_training_load,
    }


DEFAULT_MAX_POINTS = 500
ABSOLUTE_MAX_POINTS = 2000
DEFAULT_STREAM_TYPES = frozenset({"time", "heartrate", "watts"})
ALL_STREAM_TYPES = frozenset(
    {
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
)


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
    activity_id: str,
    stream_types: str | None = None,
    max_points: int | None = None,
) -> dict[str, Any]:
    """Return raw time-series streams (HR, power, etc.) for an activity.

    Use this to analyze the full workout profile beyond zone buckets.
    ``stream_types`` is a comma-separated list of: time, heartrate, watts,
    cadence, altitude, velocity_smooth, grade_smooth, distance, latlng, temp.
    Defaults to time, heartrate, and watts; time is always included.
    Large streams are downsampled to max_points (default 500, max 2000).
    """
    if max_points is None:
        max_points = DEFAULT_MAX_POINTS
    max_points = min(max(2, max_points), ABSOLUTE_MAX_POINTS)

    requested: set[str] = set(DEFAULT_STREAM_TYPES)
    if stream_types:
        parsed = {s.strip().lower() for s in stream_types.split(",") if s.strip()}
        valid = parsed & ALL_STREAM_TYPES
        if not valid:
            return {
                "activity_id": activity_id,
                "error": f"No valid stream types in '{stream_types}'. "
                f"Available: {', '.join(sorted(ALL_STREAM_TYPES))}",
            }
        requested = valid | {"time"}

    stmt = select(ActivityStream).where(ActivityStream.activity_id == activity_id)

    async with readonly_session() as session:
        stream = (await session.scalars(stmt)).first()

    if stream is None:
        return {"activity_id": activity_id, "found": False, "streams": {}}

    result_streams: dict[str, list] = {}
    returned_count = 0
    for stype in sorted(requested):
        raw = stream.data.get(stype)
        if raw is None:
            continue
        sampled = _downsample(raw, max_points)
        result_streams[stype] = sampled
        returned_count = max(returned_count, len(sampled))

    return {
        "activity_id": activity_id,
        "found": True,
        "sample_count": stream.sample_count,
        "returned_count": returned_count,
        "downsampled": stream.sample_count > returned_count,
        "streams": result_streams,
    }


# ── Training volume ─────────────────────────────────────────────────


TRAINING_DELTA_FIELDS = [
    "activity_count",
    "distance_km",
    "moving_time_hours",
    "trimp",
    "elevation_m",
    "calories",
    "average_heart_rate",
    "average_power",
    "trimp_per_hour",
]


async def _training_summary(
    *, start_date, end_date, sport_type: str | None = None
) -> dict[str, Any]:
    check_range(start_date, end_date)
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
        stmt, start_date=start_date, end_date=end_date, sport_type=sport_type
    )

    async with readonly_session() as session:
        row = (await session.execute(stmt)).one()

    moving_hours = seconds_to_hours(row.moving_time_s)
    trimp = float(row.trimp or 0)
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "sport_type": sport_type,
        "activity_count": int(row.activity_count or 0),
        "distance_km": meters_to_km(row.distance_m),
        "moving_time_hours": moving_hours,
        "trimp": round(trimp, 1),
        "elevation_m": round_or_none(row.elevation_m, 0),
        "calories": round_or_none(row.calories, 0),
        "average_heart_rate": round_or_none(row.avg_hr, 0),
        "average_power": round_or_none(row.avg_power, 0),
        "trimp_per_hour": round(trimp / moving_hours, 1) if moving_hours else 0.0,
    }


@mcp.tool()
async def get_training_summary(
    start_date: str,
    end_date: str,
    sport_type: str | None = None,
) -> dict[str, Any]:
    """Summarize training volume and load (count, distance, time, TRIMP,
    elevation, calories, average HR and power) for a local date range."""
    return await _training_summary(
        start_date=required_date(start_date, "start_date"),
        end_date=required_date(end_date, "end_date"),
        sport_type=sport_type,
    )


@mcp.tool()
async def compare_training_periods(
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    sport_type: str | None = None,
) -> dict[str, Any]:
    """Compare training volume and load between two local date ranges.

    Period A is the baseline; ``delta_b_minus_a`` is B minus A per field.
    """
    period_a = await _training_summary(
        start_date=required_date(period_a_start, "period_a_start"),
        end_date=required_date(period_a_end, "period_a_end"),
        sport_type=sport_type,
    )
    period_b = await _training_summary(
        start_date=required_date(period_b_start, "period_b_start"),
        end_date=required_date(period_b_end, "period_b_end"),
        sport_type=sport_type,
    )
    return {
        "period_a": period_a,
        "period_b": period_b,
        "delta_b_minus_a": period_deltas(period_a, period_b, TRAINING_DELTA_FIELDS),
    }


@mcp.tool()
async def get_gear_usage(
    start_date: str | None = None,
    end_date: str | None = None,
    sport_type: str | None = None,
) -> dict[str, Any]:
    """Group activity volume and load by gear name, optionally within a local
    date range or for one sport type."""
    start = parse_date(start_date, "start_date")
    end = parse_date(end_date, "end_date")
    check_range(start, end)
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
        stmt, start_date=start, end_date=end, sport_type=sport_type
    )
    stmt = stmt.group_by(gear_label).order_by(desc(distance_m))

    async with readonly_session() as session:
        rows = (await session.execute(stmt)).all()

    return {
        "gear": [
            {
                "gear_name": row.gear_name,
                "activity_count": int(row.activity_count or 0),
                "distance_km": meters_to_km(row.distance_m),
                "moving_time_hours": seconds_to_hours(row.moving_time_s),
                "trimp": round_or_none(row.trimp, 1),
                "elevation_m": round_or_none(row.elevation_m, 0),
            }
            for row in rows
        ]
    }


# ── Training load ───────────────────────────────────────────────────


DEFAULT_LOAD_DAYS = 42
MAX_LOAD_DAYS = 366


def _load_day(row: DailyMetrics) -> dict[str, Any]:
    return {
        "day": row.date.isoformat(),
        "trimp": round_or_none(row.daily_trimp, 1),
        "activity_count": row.activity_count,
        "ctl": round_or_none(row.ctl, 1),
        "atl": round_or_none(row.atl, 1),
        # Start-of-day form (yesterday's CTL - ATL), as on the dashboards.
        "tsb": round_or_none(row.tsb, 1),
        "tsb_end_of_day": round_or_none(row.ctl - row.atl, 1),
    }


@mcp.tool()
async def get_training_load(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Daily fitness (CTL), fatigue (ATL), and form (TSB) for a local date
    range, plus the change over it. Defaults to the last 42 days; at most 366.

    CTL and ATL are end-of-day values. ``tsb`` is start-of-day form;
    ``tsb_end_of_day`` includes that day's training.
    """
    end = parse_date(end_date, "end_date") or today_local()
    start = parse_date(start_date, "start_date") or end - timedelta(
        days=DEFAULT_LOAD_DAYS - 1
    )
    check_range(start, end)
    if (end - start).days + 1 > MAX_LOAD_DAYS:
        raise ValueError(
            f"date range is limited to {MAX_LOAD_DAYS} days; split the request"
        )

    stmt = (
        select(DailyMetrics)
        .where(DailyMetrics.date >= start, DailyMetrics.date <= end)
        .order_by(DailyMetrics.date)
    )
    async with readonly_session() as session:
        rows = (await session.scalars(stmt)).all()

    days = [_load_day(row) for row in rows]
    summary = None
    if rows:
        first, last = rows[0], rows[-1]
        peak = max(rows, key=lambda r: r.ctl)
        lowest_form = min(rows, key=lambda r: r.tsb)
        summary = {
            "ctl_change": round(last.ctl - first.ctl, 1),
            "atl_change": round(last.atl - first.atl, 1),
            "total_trimp": round(sum(r.daily_trimp for r in rows), 1),
            "peak_ctl": round_or_none(peak.ctl, 1),
            "peak_ctl_day": peak.date.isoformat(),
            "lowest_tsb": round_or_none(lowest_form.tsb, 1),
            "lowest_tsb_day": lowest_form.date.isoformat(),
            "metrics_as_of": local_time(max(r.updated_at for r in rows)),
        }
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "summary": summary,
        "days": days,
    }


# ── Athlete ─────────────────────────────────────────────────────────


def _zones(bounds: list[tuple[float, float]], unit: str) -> list[dict[str, Any]]:
    zones = []
    for index, (low, high) in enumerate(bounds, start=1):
        zones.append(
            {
                # Same keys as hr_zone_seconds / power_zone_seconds.
                "zone": f"zone_{index}",
                f"min_{unit}": round(low),
                # The top zone is open-ended.
                f"max_{unit}": round(high) if index < len(bounds) else None,
            }
        )
    return zones


@mcp.tool()
async def get_athlete_profile() -> dict[str, Any]:
    """Return the athlete's max/resting HR, FTP (configured and estimated),
    time zone, and the HR and power zone boundaries used for zone seconds."""
    async with readonly_session() as session:
        settings = (
            await session.scalars(
                select(AthleteSettings).order_by(desc(AthleteSettings.updated_at)).limit(1)
            )
        ).first()

    athlete = config.athlete
    max_hr = settings.max_hr if settings else athlete.max_hr
    rest_hr = settings.rest_hr if settings else athlete.rest_hr
    ftp = settings.ftp if settings else athlete.ftp
    hr_bounds = get_hr_zone_boundaries(
        max_hr, rest_hr, settings.hr_zones if settings else None
    )
    return {
        "max_hr": max_hr,
        "rest_hr": rest_hr,
        "ftp": ftp,
        "estimated_ftp": settings.estimated_ftp if settings else None,
        "timezone": athlete.timezone,
        "hr_zones": _zones(hr_bounds, "bpm"),
        "power_zones": _zones(get_power_zone_boundaries(ftp), "watts"),
        "updated_at": local_time(settings.updated_at) if settings else None,
    }


# ── Coverage ────────────────────────────────────────────────────────


async def _record_coverage(session, model) -> dict[str, Any]:
    visible, hidden, earliest, latest = (
        await session.execute(
            select(
                func.count(model.id).filter(model.superseded_by == None),  # noqa: E711
                func.count(model.id).filter(model.superseded_by != None),  # noqa: E711
                func.min(model.day).filter(model.superseded_by == None),  # noqa: E711
                func.max(model.day).filter(model.superseded_by == None),  # noqa: E711
            )
        )
    ).one()
    by_provider = (
        await session.execute(
            select(model.provider, func.count(model.id))
            .where(model.superseded_by == None)  # noqa: E711
            .group_by(model.provider)
        )
    ).all()
    return {
        "count": int(visible or 0),
        "by_provider": {name: int(count) for name, count in by_provider},
        "superseded_duplicates": int(hidden or 0),
        "earliest_day": iso_day(earliest),
        "latest_day": iso_day(latest),
    }


@mcp.tool()
async def get_data_coverage() -> dict[str, Any]:
    """Report what data is stored, per kind: visible row counts per provider,
    hidden duplicates, the first and last day, and for activities how many
    still lack detail, streams, or heart rate, and how many the provider no
    longer serves. Use it to tell missing data from a quiet period."""
    visible = ACTIVITY_VISIBLE
    gone = Activity.deleted_upstream_at != None  # noqa: E711
    activity_count = func.count(Activity.id)
    # Superseded duplicates are never enriched, so counting them would
    # permanently report missing detail and streams.
    activity_stmt = select(
        activity_count.filter(visible).label("count"),
        activity_count.filter(
            Activity.superseded_by != None, ~gone  # noqa: E711
        ).label("superseded"),
        activity_count.filter(gone).label("deleted_upstream"),
        func.min(Activity.start_date).filter(visible).label("earliest"),
        func.max(Activity.start_date).filter(visible).label("latest"),
        activity_count.filter(visible, Activity.detail_synced == False).label(  # noqa: E712
            "missing_detail"
        ),
        activity_count.filter(visible, Activity.synced_streams == False).label(  # noqa: E712
            "missing_streams"
        ),
        activity_count.filter(visible, Activity.has_heartrate == False).label(  # noqa: E712
            "without_heart_rate"
        ),
        activity_count.filter(visible, Activity.average_watts != None).label(  # noqa: E711
            "with_power"
        ),
    )
    by_source_stmt = (
        select(Activity.source, func.count(Activity.id))
        .where(visible)
        .group_by(Activity.source)
    )
    load_stmt = select(
        func.count(DailyMetrics.date),
        func.min(DailyMetrics.date),
        func.max(DailyMetrics.date),
        func.max(DailyMetrics.updated_at),
    )

    async with readonly_session() as session:
        act = (await session.execute(activity_stmt)).one()
        by_source = (await session.execute(by_source_stmt)).all()
        load = (await session.execute(load_stmt)).one()
        body = await _record_coverage(session, BodyMeasurement)
        nutrition = await _record_coverage(session, DailyNutrition)

    return {
        "activities": {
            "count": int(act.count or 0),
            "by_provider": {
                str(source or "unknown"): int(count or 0) for source, count in by_source
            },
            "superseded_duplicates": int(act.superseded or 0),
            "deleted_upstream": int(act.deleted_upstream or 0),
            "earliest_day": iso_day(local_day(act.earliest) if act.earliest else None),
            "latest_day": iso_day(local_day(act.latest) if act.latest else None),
            "missing_detail": int(act.missing_detail or 0),
            "missing_streams": int(act.missing_streams or 0),
            "without_heart_rate": int(act.without_heart_rate or 0),
            "with_power": int(act.with_power or 0),
        },
        "training_load": {
            "days": int(load[0] or 0),
            "earliest_day": iso_day(load[1]),
            "latest_day": iso_day(load[2]),
            "metrics_as_of": local_time(load[3]),
        },
        "body": body,
        "nutrition": nutrition,
    }


register_record_tools(mcp)


if __name__ == "__main__":
    mcp.settings.host = config.mcp.host
    mcp.settings.port = config.mcp.port
    mcp.run(transport="streamable-http")
