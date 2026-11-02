"""Withings sleep, daily wellness, and intraday samples.

Shapes and units follow the Withings API reference; they still need checking
against a real account. Only the baseline in :mod:`providers.feeds` is mapped.
Withings' own scores and indexes (sleep score, Withings index, breathing
disturbances, temperature status) are left out, and so are the values the core
derives itself (sleep heart rate, efficiency, heart-rate zones).

Withings has no resting heart rate, nightly HRV average, daily SpO2, or skin
temperature deviation, so those wellness fields stay empty.

Every sleep summary carries its own time zone, so a night's wake day comes
from the data itself. Withings marks neither main sleep nor naps: the longest
sleep ending on a day is that day's night, and the others count as naps.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from catalog import DataKind
from providers.feeds import (
    METRIC_HEART_RATE,
    METRIC_HRV,
    METRIC_SPO2,
    SampleRecord,
    SleepRecord,
    SleepStage,
    WellnessRecord,
    heart_rate_windows,
)
from providers.mapping import as_float, as_int, date_chunks, local_dates

if TYPE_CHECKING:
    from providers.withings import WithingsSource

#: Sleep ``get`` segment states → the baseline's stage names. 4 (manually
#: entered) and 5 (unspecified) are asleep without a stage.
_STAGE_NAMES = {0: "awake", 1: "light", 2: "deep", 3: "rem", 4: "asleep", 5: "asleep"}

_CHUNK_DAYS = 30
#: Sleep ``get`` and ``getintradayactivity`` return at most 24 hours.
_MAX_SPAN_SECONDS = 86_400


def _epoch(raw: object) -> datetime | None:
    value = as_float(raw)
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _seconds(raw: object) -> int | None:
    value = as_float(raw)
    return None if value is None else as_int(value)


def _positive(raw: object) -> float | None:
    """Withings reports a missing reading as 0."""
    value = as_float(raw)
    return value if value is not None and value > 0 else None


async def _paged(fetch, **params) -> list[dict]:
    """Every page of a list call, following ``more``/``offset``; ``fetch``
    returns one page body and the list sits under ``series`` or
    ``activities``."""
    items: list[dict] = []
    offset: int | None = None
    while True:
        body = await fetch(**params, offset=offset)
        items.extend(body.get("series") or body.get("activities") or [])
        if not body.get("more") or body.get("offset") in (None, offset):
            return items
        offset = int(body["offset"])


def _spans(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """``[start, end)`` as epoch ``(startdate, enddate)`` pairs short enough
    for the 24-hour endpoints; ``enddate`` is inclusive there."""
    first, last = int(start.timestamp()), int(end.timestamp())
    return [
        (at, min(at + _MAX_SPAN_SECONDS, last) - 1)
        for at in range(first, last, _MAX_SPAN_SECONDS)
    ]


# ── Sleep ───────────────────────────────────────────────────────────


def wake_day(summary: dict, fallback: ZoneInfo) -> date | None:
    """The local day a sleep summary ends on, in its own time zone."""
    end = _epoch(summary.get("enddate"))
    if end is None:
        return None
    try:
        tz = ZoneInfo(str(summary.get("timezone")))
    except (ZoneInfoNotFoundError, ValueError):
        tz = fallback
    return end.astimezone(tz).date()


def _asleep(summary: dict) -> int | None:
    """Time asleep; nights from an external source only have ``asleepduration``."""
    data = summary.get("data") or {}
    return _seconds(data.get("total_sleep_time")) or _seconds(
        data.get("asleepduration")
    )


def group_nights(summaries: list[dict], fallback: ZoneInfo) -> list[dict]:
    """One ``{day, main, naps}`` payload per wake day; the longest sleep is
    the night."""
    by_day: dict[date, list[dict]] = {}
    for summary in summaries:
        day = wake_day(summary, fallback)
        if day is not None and _epoch(summary.get("startdate")) is not None:
            by_day.setdefault(day, []).append(summary)
    nights = []
    for day in sorted(by_day):
        main = max(by_day[day], key=lambda s: _asleep(s) or 0)
        naps = [s for s in by_day[day] if s is not main]
        nights.append({"day": day.isoformat(), "main": main, "naps": naps})
    return nights


def map_sleep(payload: dict) -> SleepRecord | None:
    """Map a ``{day, main, naps, segments}`` payload; ``segments`` is the
    night's sleep ``get`` series. Pure."""
    main = payload.get("main") or {}
    start = _epoch(main.get("startdate"))
    end = _epoch(main.get("enddate"))
    if start is None or end is None or not payload.get("day"):
        return None
    data = main.get("data") or {}
    stages = tuple(
        SleepStage(
            start=_epoch(segment["startdate"]),
            end=_epoch(segment["enddate"]),
            stage=_STAGE_NAMES[segment["state"]],
        )
        for segment in payload.get("segments") or []
        if segment.get("state") in _STAGE_NAMES
        and _epoch(segment.get("startdate"))
        and _epoch(segment.get("enddate"))
    )
    return SleepRecord(
        external_id=str(main.get("id") or main["startdate"])[:128],
        day=date.fromisoformat(payload["day"]),
        start=start,
        end=end,
        asleep_seconds=_asleep(main),
        deep_seconds=_seconds(data.get("deepsleepduration")),
        light_seconds=_seconds(data.get("lightsleepduration")),
        rem_seconds=_seconds(data.get("remsleepduration")),
        awake_seconds=_seconds(data.get("wakeupduration")),
        nap_seconds=sum(_asleep(nap) or 0 for nap in payload.get("naps") or []),
        stages=stages or None,
    )


async def list_nights(provider: "WithingsSource", first: date, last: date) -> list[dict]:
    """Nights ending on ``first``..``last``, grouped by :func:`group_nights`."""
    client = provider.client
    summaries: list[dict] = []
    # A night is listed under the date Withings gives it, which may be the
    # day before it ends.
    for chunk_start, chunk_end in date_chunks(first - timedelta(days=1), last, _CHUNK_DAYS):
        summaries.extend(
            await _paged(
                client.get_sleep_summary,
                startdateymd=chunk_start.isoformat(),
                enddateymd=chunk_end.isoformat(),
            )
        )
    nights = group_nights(summaries, provider.timezone)
    return [n for n in nights if first <= date.fromisoformat(n["day"]) <= last]


class WithingsSleepFeed:
    """Withings sleep as a ``SleepFeed``: one night per wake day, with its
    stage timeline (one extra request per night)."""

    kind = DataKind.SLEEP

    def __init__(self, provider: "WithingsSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        first, last = local_dates(start, end, self._provider.timezone)
        nights = await list_nights(self._provider, first, last)
        for night in nights:
            main = night["main"]
            body = await self._provider.client.get_sleep(
                startdate=int(main["startdate"]), enddate=int(main["enddate"])
            )
            night["segments"] = body.get("series") or []
        return nights

    def map_record(self, payload: dict) -> SleepRecord | None:
        return map_sleep(payload)


# ── Daily wellness ──────────────────────────────────────────────────


def _worn(activity: dict) -> bool:
    """A day without steps or active calories only carries Withings' passive
    calorie estimate, so it says nothing about the day."""
    return bool(_positive(activity.get("steps")) or _positive(activity.get("calories")))


def map_wellness(payload: dict) -> WellnessRecord | None:
    """Map a ``{date, activity, sleep}`` payload: one ``getactivity`` day and
    the sleep summary of the night ending on it; either may be missing. Pure.
    """
    raw_day = payload.get("date")
    if not raw_day:
        return None
    activity = payload.get("activity") or {}
    if not _worn(activity):
        activity = {}
    sleep = (payload.get("sleep") or {}).get("data") or {}
    steps = as_float(activity.get("steps"))
    record = WellnessRecord(
        day=date.fromisoformat(str(raw_day)),
        respiration=_positive(sleep.get("rr_average")),
        steps=None if steps is None else as_int(steps),
        active_kcal=as_float(activity.get("calories")),
        total_kcal=as_float(activity.get("totalcalories")),
    )
    measured = [
        value
        for name, value in vars(record).items()
        if name != "day" and value is not None
    ]
    return record if measured else None


class WithingsWellnessFeed:
    """Daily activity totals and sleep respiration, joined by day."""

    kind = DataKind.WELLNESS

    def __init__(self, provider: "WithingsSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        first, last = local_dates(start, end, self._provider.timezone)
        by_day: dict[str, dict] = {}
        for chunk_start, chunk_end in date_chunks(first, last, _CHUNK_DAYS):
            for activity in await _paged(
                client.get_activity,
                startdateymd=chunk_start.isoformat(),
                enddateymd=chunk_end.isoformat(),
            ):
                if activity.get("date"):
                    day = str(activity["date"])
                    by_day.setdefault(day, {"date": day})["activity"] = activity
        for night in await list_nights(self._provider, first, last):
            by_day.setdefault(night["day"], {"date": night["day"]})["sleep"] = night["main"]
        return [
            by_day[day]
            for day in sorted(by_day)
            if first <= date.fromisoformat(day) <= last
        ]

    def map_record(self, payload: dict) -> WellnessRecord | None:
        return map_wellness(payload)


# ── Intraday samples ────────────────────────────────────────────────


def _points(series: object, field: str) -> dict[int, float]:
    """``{"<epoch>": {field: value}}`` intraday series → ``{epoch: value}``."""
    if not isinstance(series, dict):
        return {}
    out: dict[int, float] = {}
    for at, values in series.items():
        value = _positive((values or {}).get(field)) if isinstance(values, dict) else None
        if value is not None and str(at).isdigit():
            out[int(at)] = value
    return out


def _segment_points(segment: dict, field: str) -> dict[int, float]:
    """A sleep ``get`` segment's ``{"<epoch>": value}`` series."""
    raw = segment.get(field)
    if not isinstance(raw, dict):
        return {}
    return {
        int(at): value
        for at, value in ((at, _positive(v)) for at, v in raw.items())
        if value is not None and str(at).isdigit()
    }


def merge_night(
    day_points: dict[int, float], segments: list[dict], field: str
) -> dict[int, float]:
    """Add a sleep tracker's readings where the watch has none.

    A segment's readings are used only when the intraday series has no
    reading inside that segment, so one stretch never mixes two devices (a
    watch worn on a Sleep Analyzer mat).
    """
    merged = dict(day_points)
    for segment in segments:
        start, end = as_float(segment.get("startdate")), as_float(segment.get("enddate"))
        if start is None or end is None:
            continue
        if any(start <= at < end for at in day_points):
            continue
        merged.update(_segment_points(segment, field))
    return merged


def samples_for(
    intraday: object, segments: list[dict], start: int, end: int
) -> list[dict]:
    """Sample payloads for ``[start, end)``: two-minute heart-rate averages,
    and HRV and SpO2 readings as reported."""
    def within(points: dict[int, float]) -> dict[int, float]:
        return {at: v for at, v in points.items() if start <= at < end}

    heart_rate = within(merge_night(_points(intraday, "heart_rate"), segments, "hr"))
    hrv = within(merge_night(_points(intraday, "rmssd"), segments, "rmssd"))
    spo2 = within(_points(intraday, "spo2_auto"))
    out = [
        {"metric": METRIC_HEART_RATE, "at": at, "value": bpm}
        for at, bpm in heart_rate_windows(heart_rate)
    ]
    out.extend({"metric": METRIC_HRV, "at": at, "value": v} for at, v in sorted(hrv.items()))
    out.extend({"metric": METRIC_SPO2, "at": at, "value": v} for at, v in sorted(spo2.items()))
    return out


def map_sample(payload: dict) -> SampleRecord | None:
    """Pure. ``at`` is epoch seconds; missing and zero values are skipped."""
    value = _positive(payload.get("value"))
    measured_at = _epoch(payload.get("at"))
    if value is None or measured_at is None:
        return None
    return SampleRecord(
        metric=str(payload["metric"]), measured_at=measured_at, value=round(value, 2)
    )


class WithingsIntradayFeed:
    """Heart rate, HRV, and SpO2 as an ``IntradayFeed``.

    Two requests per day: intraday activity (a watch) and sleep (a watch or a
    Sleep Analyzer mat, whose readings only come from there).
    """

    kind = DataKind.INTRADAY

    def __init__(self, provider: "WithingsSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        tz = self._provider.timezone
        first, last = local_dates(start, end, tz)
        out: list[dict] = []
        day = first
        while day <= last:
            day_start = datetime.combine(day, time.min, tzinfo=tz)
            day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
            intraday: dict = {}
            segments: list[dict] = []
            for startdate, enddate in _spans(day_start, day_end):
                body = await client.get_intraday_activity(
                    startdate=startdate, enddate=enddate
                )
                if isinstance(body.get("series"), dict):
                    intraday.update(body["series"])
                body = await client.get_sleep(startdate=startdate, enddate=enddate)
                segments.extend(body.get("series") or [])
            out.extend(
                samples_for(
                    intraday,
                    segments,
                    int(day_start.timestamp()),
                    int(day_end.timestamp()),
                )
            )
            day += timedelta(days=1)
        return out

    def map_record(self, payload: dict) -> SampleRecord | None:
        return map_sample(payload)
