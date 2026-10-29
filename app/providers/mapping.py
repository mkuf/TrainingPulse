"""Pure helpers shared by the source mappers.

Everything here is synchronous and side-effect free so the mappers can be
tested against recorded provider fixtures without HTTP or a database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo


def parse_dt(raw: object) -> datetime | None:
    """Parse a provider timestamp into an aware UTC-ish datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # date-only
        try:
            day = date.fromisoformat(text[:10])
            return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_duration_seconds(raw: object) -> int:
    """Parse a duration that may be a number or a protobuf-style ``"123s"``."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    if text.endswith("s") and text[:-1].replace(".", "", 1).isdigit():
        return int(float(text[:-1]))
    try:
        return int(float(text))
    except ValueError:
        return 0


def normal_gear_id(raw: object) -> str | None:
    """Normalize a gear id, treating the literal string "none" as unset."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() == "none":
        return None
    return text[:64]


def as_float(raw: object) -> float | None:
    """Coerce to float, returning None for missing or non-numeric values."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def as_int(raw: object, default: int = 0) -> int:
    """Coerce to int, falling back to ``default``."""
    value = as_float(raw)
    return default if value is None else int(value)


def clip_text(raw: object, length: int) -> str | None:
    """Trim a provider string to a column width, or None when empty."""
    if raw is None:
        return None
    text = str(raw).strip()
    return text[:length] if text else None


def local_dates(start: datetime, end: datetime, tz: ZoneInfo) -> tuple[date, date]:
    """Inclusive local dates covered by the half-open UTC window ``[start, end)``.

    The record engine hands feeds windows that cover whole local days, so
    this recovers exactly those days for APIs that take calendar dates.
    """
    first = start.astimezone(tz).date()
    last = (end - timedelta(microseconds=1)).astimezone(tz).date()
    return first, last


def date_chunks(first: date, last: date, days: int) -> Iterator[tuple[date, date]]:
    """Inclusive ``(from, to)`` ranges of at most ``days`` days covering first..last."""
    current = first
    while current <= last:
        chunk_end = min(current + timedelta(days=days - 1), last)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)
