"""Generic FIT file analysis.

Turns raw ``.fit`` bytes into:

- a :class:`FitSummary` of aggregate metrics matching the ``activities``
  table's aggregate columns (elapsed/moving time, distance, HR, power, ...)
- a dict of canonical time-series streams, using the same stream names as
  ``providers.base`` (``time``, ``heartrate``, ``watts``, ...)

This module has no knowledge of any particular activity source. Any source
that can hand it raw FIT bytes (downloaded from wherever) gets the same
analysis. It performs no I/O and no database access — see :mod:`fitfiles.store`
for persistence.

Uses ``fitdecode`` with its default data processor, which decodes ``date_time``
fields to timezone-aware UTC ``datetime`` objects but leaves distance in
meters, speed in m/s, and GPS coordinates in FIT "semicircles" — matching the
rest of this app's meters/seconds convention, except semicircles, which are
converted to degrees here.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import fitdecode

logger = logging.getLogger(__name__)


class FitParseError(Exception):
    """Raised when a .fit file is fundamentally unreadable.

    Malformed individual messages/fields are logged and skipped instead of
    raising — this is only for a stream that cannot be read at all (bad
    header, truncated body, mismatched CRC when checking is enabled, etc.).
    """


# Canonical stream keys. Duplicated here (as plain strings) rather than
# imported from ``providers.base``, so this package stays usable outside the
# app's source-adapter layer (e.g. from a future plugin) without a hard
# dependency on it.
STREAM_TIME = "time"
STREAM_HEARTRATE = "heartrate"
STREAM_WATTS = "watts"
STREAM_CADENCE = "cadence"
STREAM_DISTANCE = "distance"
STREAM_ALTITUDE = "altitude"
STREAM_LATLNG = "latlng"
STREAM_VELOCITY = "velocity_smooth"
STREAM_TEMP = "temp"

_SEMICIRCLES_TO_DEGREES = 180.0 / (2**31)


@dataclass
class FitSummary:
    """Aggregate metrics derived from one FIT file."""

    start_date: datetime | None = None
    elapsed_time: int = 0
    moving_time: int = 0
    distance: float | None = None
    average_heartrate: float | None = None
    max_heartrate: float | None = None
    has_heartrate: bool = False
    average_watts: float | None = None
    max_watts: float | None = None
    device_watts: bool = False
    average_speed: float | None = None
    max_speed: float | None = None
    #: One leg on foot, as FIT stores it; see ``mapping.steps_per_minute``.
    average_cadence: float | None = None
    max_cadence: float | None = None
    total_elevation_gain: float | None = None
    total_descent: float | None = None
    calories: float | None = None
    device_name: str | None = None


@dataclass
class FitAnalysis:
    """The full result of analyzing one FIT file."""

    summary: FitSummary
    streams: dict[str, list] = field(default_factory=dict)


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _semicircles_to_degrees(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value) * _SEMICIRCLES_TO_DEGREES
    except (TypeError, ValueError):
        return None


def _as_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: float | None) -> int | None:
    return None if value is None else round(value)


def _mean(values: list[float | None]) -> float | None:
    numeric = [v for v in values if v is not None]
    if not numeric:
        return None
    return sum(numeric) / len(numeric)


def _max(values: list[float | None]) -> float | None:
    numeric = [v for v in values if v is not None]
    return max(numeric) if numeric else None


def analyze_fit_file(raw: bytes) -> FitAnalysis:
    """Parse raw ``.fit`` bytes into a summary and canonical streams.

    Never raises on a single malformed message or field — those are logged and
    skipped so a partially-corrupt file still yields whatever it can. Only
    raises :class:`FitParseError` when the file itself cannot be read at all.
    """
    records: list[dict] = []
    sessions: list[dict] = []
    device_name: str | None = None

    try:
        with fitdecode.FitReader(io.BytesIO(raw)) as fit:
            for frame in fit:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                try:
                    name = frame.name
                except Exception as exc:  # noqa: BLE001 - keep parsing
                    logger.warning("Skipping unnamed FIT message: %s", exc)
                    continue

                try:
                    if name == "record":
                        records.append(_extract_record(frame))
                    elif name == "session":
                        sessions.append(_extract_session(frame))
                    elif name == "device_info" and device_name is None:
                        device_name = _extract_device_name(frame)
                except Exception as exc:  # noqa: BLE001 - one bad message
                    logger.warning("Skipping malformed FIT %s message: %s", name, exc)
    except Exception as exc:  # noqa: BLE001 - any decode failure means unreadable
        raise FitParseError(str(exc)) from exc

    streams = _build_streams(records)
    summary = _build_summary(records, sessions, streams, device_name)
    return FitAnalysis(summary=summary, streams=streams)


def _extract_record(frame: "fitdecode.FitDataMessage") -> dict:
    timestamp = _as_utc(frame.get_value("timestamp", fallback=None))
    altitude = frame.get_value("enhanced_altitude", fallback=None)
    if altitude is None:
        altitude = frame.get_value("altitude", fallback=None)
    speed = frame.get_value("enhanced_speed", fallback=None)
    if speed is None:
        speed = frame.get_value("speed", fallback=None)
    return {
        "timestamp": timestamp,
        "lat": _semicircles_to_degrees(frame.get_value("position_lat", fallback=None)),
        "lon": _semicircles_to_degrees(
            frame.get_value("position_long", fallback=None)
        ),
        "altitude": _as_number(altitude),
        "heart_rate": _as_number(frame.get_value("heart_rate", fallback=None)),
        "power": _as_number(frame.get_value("power", fallback=None)),
        "cadence": _as_number(frame.get_value("cadence", fallback=None)),
        "distance": _as_number(frame.get_value("distance", fallback=None)),
        "speed": _as_number(speed),
        "temperature": _as_number(frame.get_value("temperature", fallback=None)),
    }


def _extract_session(frame: "fitdecode.FitDataMessage") -> dict:
    return {
        "start_time": _as_utc(frame.get_value("start_time", fallback=None)),
        "total_elapsed_time": frame.get_value("total_elapsed_time", fallback=None),
        "total_timer_time": frame.get_value("total_timer_time", fallback=None),
        "total_distance": frame.get_value("total_distance", fallback=None),
        "total_calories": frame.get_value("total_calories", fallback=None),
        "total_ascent": frame.get_value("total_ascent", fallback=None),
        "total_descent": frame.get_value("total_descent", fallback=None),
        "avg_heart_rate": frame.get_value("avg_heart_rate", fallback=None),
        "max_heart_rate": frame.get_value("max_heart_rate", fallback=None),
        "avg_power": frame.get_value("avg_power", fallback=None),
        "max_power": frame.get_value("max_power", fallback=None),
        "avg_speed": frame.get_value("avg_speed", fallback=None),
        "max_speed": frame.get_value("enhanced_max_speed", fallback=None)
        or frame.get_value("max_speed", fallback=None),
        "avg_cadence": frame.get_value("avg_cadence", fallback=None),
        "max_cadence": frame.get_value("max_cadence", fallback=None),
    }


def _extract_device_name(frame: "fitdecode.FitDataMessage") -> str | None:
    manufacturer = frame.get_value("manufacturer", fallback=None)
    product_name = frame.get_value("product_name", fallback=None)
    product = frame.get_value("product", fallback=None)
    parts = []
    for part in (manufacturer, product_name or product):
        if not part:
            continue
        text = str(part).replace("_", " ").strip()
        if text:
            parts.append(text.title())
    return " ".join(parts) or None


def _build_streams(records: list[dict]) -> dict[str, list]:
    timed = [r for r in records if r["timestamp"] is not None]
    if not timed:
        return {}
    timed.sort(key=lambda r: r["timestamp"])
    start = timed[0]["timestamp"]

    streams: dict[str, list] = {
        STREAM_TIME: [
            max(0, int((r["timestamp"] - start).total_seconds())) for r in timed
        ]
    }

    def maybe_add(key: str, extractor) -> None:
        values = [extractor(r) for r in timed]
        if any(v is not None for v in values):
            streams[key] = values

    # HR, power and cadence are whole numbers in FIT and in Strava streams;
    # keep them as ints so consumers (e.g. Grafana's ``::int`` casts) agree.
    maybe_add(STREAM_HEARTRATE, lambda r: _as_int(r["heart_rate"]))
    maybe_add(STREAM_WATTS, lambda r: _as_int(r["power"]))
    maybe_add(STREAM_CADENCE, lambda r: _as_int(r["cadence"]))
    maybe_add(STREAM_DISTANCE, lambda r: r["distance"])
    maybe_add(STREAM_ALTITUDE, lambda r: r["altitude"])
    maybe_add(STREAM_VELOCITY, lambda r: r["speed"])
    maybe_add(STREAM_TEMP, lambda r: r["temperature"])

    if any(r["lat"] is not None and r["lon"] is not None for r in timed):
        streams[STREAM_LATLNG] = [
            [r["lat"], r["lon"]]
            if r["lat"] is not None and r["lon"] is not None
            else None
            for r in timed
        ]

    return streams


def _build_summary(
    records: list[dict],
    sessions: list[dict],
    streams: dict[str, list],
    device_name: str | None,
) -> FitSummary:
    # v1 limitation: multi-session (multi-sport) files use only the first
    # session message for its totals; the stream-derived aggregates below
    # already span every record regardless of session boundaries.
    session = sessions[0] if sessions else {}

    time_stream = streams.get(STREAM_TIME) or []
    hr_stream = streams.get(STREAM_HEARTRATE) or []
    watts_stream = streams.get(STREAM_WATTS) or []
    distance_stream = streams.get(STREAM_DISTANCE) or []
    velocity_stream = streams.get(STREAM_VELOCITY) or []

    timed = [r for r in records if r["timestamp"] is not None]
    start_date = timed[0]["timestamp"] if timed else session.get("start_time")

    if time_stream:
        elapsed_time = time_stream[-1]
    else:
        elapsed_time = int(session.get("total_elapsed_time") or 0)
    moving_time = session.get("total_timer_time")
    moving_time = int(moving_time) if moving_time is not None else elapsed_time

    distance = next((v for v in reversed(distance_stream) if v is not None), None)
    if distance is None:
        distance = _as_number(session.get("total_distance"))

    avg_hr = _mean(hr_stream) if hr_stream else _as_number(session.get("avg_heart_rate"))
    max_hr = _max(hr_stream) if hr_stream else _as_number(session.get("max_heart_rate"))
    avg_watts = (
        _mean(watts_stream) if watts_stream else _as_number(session.get("avg_power"))
    )
    max_watts = (
        _max(watts_stream) if watts_stream else _as_number(session.get("max_power"))
    )

    average_speed = _as_number(session.get("avg_speed"))
    if average_speed is None and velocity_stream:
        average_speed = _mean(velocity_stream)

    return FitSummary(
        start_date=start_date,
        elapsed_time=int(elapsed_time or 0),
        moving_time=int(moving_time or 0),
        distance=distance,
        average_heartrate=avg_hr,
        max_heartrate=max_hr,
        has_heartrate=bool(hr_stream) or avg_hr is not None,
        average_watts=avg_watts,
        max_watts=max_watts,
        device_watts=bool(watts_stream) or avg_watts is not None,
        average_speed=average_speed,
        max_speed=_as_number(session.get("max_speed")),
        average_cadence=_as_number(session.get("avg_cadence")),
        max_cadence=_as_number(session.get("max_cadence")),
        total_elevation_gain=_as_number(session.get("total_ascent")),
        total_descent=_as_number(session.get("total_descent")),
        calories=_as_number(session.get("total_calories")),
        device_name=device_name,
    )
