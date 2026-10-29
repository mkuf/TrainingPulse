"""Garmin sleep, daily wellness, and intraday samples via the Personal API.

Field names and units were recorded from a real Personal API account (a
Garmin CIRQA band). Only the baseline in :mod:`providers.feeds` is mapped;
Garmin's own scores (sleep score, stress, Body Battery, HRV status) are left
out because no other provider can match them. The Personal API has no sleep
stage timeline (it returns 404), so Garmin nights carry stage totals only.

Garmin assigns every daily value to a calendar day in the user's Garmin time
zone, which is assumed to be ``athlete.timezone``. A night belongs to the day
it ends, and so does its HRV.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from catalog import DataKind
from providers.feeds import (
    METRIC_HEART_RATE,
    METRIC_HRV,
    SampleRecord,
    SleepRecord,
    WellnessRecord,
)
from providers.mapping import (
    as_float,
    as_int,
    date_chunks,
    local_dates,
    parse_dt,
)

if TYPE_CHECKING:
    from providers.garmin import GarminSource

#: Sleep summaries reject ranges over 31 days.
_SLEEP_CHUNK_DAYS = 28
#: Daily and HRV summaries accept a year; the engine's windows are shorter.
_SUMMARY_CHUNK_DAYS = 90


def _epoch_ms(raw: object) -> datetime | None:
    value = as_float(raw)
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def _positive(raw: object) -> float | None:
    """Garmin uses 0 and negative numbers (-1, -2) for "no reading"."""
    value = as_float(raw)
    return value if value is not None and value > 0 else None


def _optional_int(raw: object) -> int | None:
    value = as_float(raw)
    return None if value is None else as_int(value)


# ── Sleep ───────────────────────────────────────────────────────────


def _is_sleep(payload: dict) -> bool:
    """Days without a sleep come back with every field null."""
    return (
        payload.get("id") is not None
        and payload.get("sleepStartTimestampGMT") is not None
        and payload.get("sleepEndTimestampGMT") is not None
        and bool(payload.get("calendarDate"))
    )


def map_sleep(payload: dict) -> SleepRecord | None:
    """Map one ``dailySleepSummariesByDate`` entry onto a sleep record. Pure."""
    if not _is_sleep(payload):
        return None
    return SleepRecord(
        external_id=str(payload["id"]),
        day=date.fromisoformat(str(payload["calendarDate"])),
        start=_epoch_ms(payload["sleepStartTimestampGMT"]),
        end=_epoch_ms(payload["sleepEndTimestampGMT"]),
        asleep_seconds=_optional_int(payload.get("sleepTimeSeconds")),
        deep_seconds=_optional_int(payload.get("deepSleepSeconds")),
        light_seconds=_optional_int(payload.get("lightSleepSeconds")),
        rem_seconds=_optional_int(payload.get("remSleepSeconds")),
        awake_seconds=_optional_int(payload.get("awakeSleepSeconds")),
        nap_seconds=_optional_int(payload.get("napTimeSeconds")),
    )


class GarminSleepFeed:
    """Garmin sleep as a ``SleepFeed``."""

    kind = DataKind.SLEEP

    def __init__(self, provider: "GarminSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        first, last = local_dates(start, end, self._provider.timezone)
        out: list[dict] = []
        for chunk_start, chunk_end in date_chunks(first, last, _SLEEP_CHUNK_DAYS):
            out.extend(
                await self._provider.client.get_sleep_summaries(chunk_start, chunk_end)
            )
        return out

    def map_record(self, payload: dict) -> SleepRecord | None:
        return map_sleep(payload)


# ── Daily wellness ──────────────────────────────────────────────────


def map_wellness(payload: dict) -> WellnessRecord | None:
    """Map a merged ``{calendarDate, summary, hrv, sleep}`` payload. Pure.

    ``summary`` is one ``v2/dailysummaries`` entry, ``hrv`` one
    ``hrvSummaries`` entry, and ``sleep`` that night's sleep summary; any may
    be missing. Respiration comes from the sleep, like every other
    provider's: the daily summary's value averages the whole day.
    """
    raw_day = payload.get("calendarDate")
    if not raw_day:
        return None
    summary = payload.get("summary") or {}
    hrv = payload.get("hrv") or {}
    sleep = payload.get("sleep") or {}

    heart = summary.get("heartRate") or {}
    # Empty while Pulse Ox is off; the key names follow the other summary
    # blocks and are not yet confirmed with readings.
    pulse_ox = summary.get("pulseOx") or {}
    steps = ((summary.get("movement") or {}).get("steps") or {}).get("value")
    calories = summary.get("calories") or {}

    record = WellnessRecord(
        day=date.fromisoformat(str(raw_day)),
        resting_hr=_positive(heart.get("restingValue")),
        hrv_ms=_positive(hrv.get("lastNightAvg")),
        respiration=_positive(sleep.get("averageRespirationValue")),
        spo2_avg=_positive(pulse_ox.get("avgValue")),
        spo2_min=_positive(pulse_ox.get("minValue")),
        steps=_optional_int(steps),
        active_kcal=as_float(calories.get("burnedActive")),
        total_kcal=as_float(calories.get("burnedTotal")),
    )
    measured = [
        value
        for name, value in vars(record).items()
        if name != "day" and value is not None
    ]
    return record if measured else None


class GarminWellnessFeed:
    """Daily summaries, overnight HRV, and sleep respiration, joined by day."""

    kind = DataKind.WELLNESS

    def __init__(self, provider: "GarminSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        first, last = local_dates(start, end, self._provider.timezone)
        by_day: dict[str, dict] = {}

        def put(day: object, key: str, value: dict) -> None:
            if day:
                by_day.setdefault(str(day), {"calendarDate": str(day)})[key] = value

        for chunk_start, chunk_end in date_chunks(first, last, _SUMMARY_CHUNK_DAYS):
            for summary in await client.get_daily_summaries(chunk_start, chunk_end):
                put(summary.get("calendarDate"), "summary", summary)
            for hrv in await client.get_hrv_summaries(chunk_start, chunk_end):
                put(hrv.get("calendarDate"), "hrv", hrv)
        for chunk_start, chunk_end in date_chunks(first, last, _SLEEP_CHUNK_DAYS):
            for sleep in await client.get_sleep_summaries(chunk_start, chunk_end):
                if _is_sleep(sleep):
                    put(sleep.get("calendarDate"), "sleep", sleep)
        return [by_day[day] for day in sorted(by_day)]

    def map_record(self, payload: dict) -> WellnessRecord | None:
        return map_wellness(payload)


# ── Intraday samples ────────────────────────────────────────────────


def heart_rate_samples(report: dict) -> list[dict]:
    """``dailyHeartRate`` values as sample payloads: ``[epoch ms, bpm]`` pairs,
    already two-minute averages."""
    return [
        {"metric": METRIC_HEART_RATE, "at": pair[0], "value": pair[1]}
        for pair in report.get("heartRateValues") or []
        if isinstance(pair, list) and len(pair) >= 2
    ]


def hrv_samples(report: dict) -> list[dict]:
    """``hrv/{date}`` readings as sample payloads; ``readingTimeGMT`` is UTC."""
    return [
        {
            "metric": METRIC_HRV,
            "at": reading.get("readingTimeGMT"),
            "value": reading.get("hrvValue"),
        }
        for reading in report.get("hrvReadings") or []
    ]


def map_sample(payload: dict) -> SampleRecord | None:
    """Map a sample payload; ``at`` is epoch milliseconds or a UTC timestamp.

    Pure. Missing, zero, and negative values are Garmin's "no reading".
    """
    value = _positive(payload.get("value"))
    raw_at = payload.get("at")
    if value is None or raw_at is None:
        return None
    measured_at = (
        _epoch_ms(raw_at) if isinstance(raw_at, (int, float)) else parse_dt(raw_at)
    )
    if measured_at is None:
        return None
    return SampleRecord(
        metric=str(payload["metric"]), measured_at=measured_at, value=value
    )


class GarminIntradayFeed:
    """Heart rate and HRV samples as an ``IntradayFeed``.

    Costs two requests per day, which is why the kind's default history is
    short. Garmin has no SpO2 sample endpoint in the Personal API.
    """

    kind = DataKind.INTRADAY

    def __init__(self, provider: "GarminSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        first, last = local_dates(start, end, self._provider.timezone)
        out: list[dict] = []
        day = first
        while day <= last:
            out.extend(heart_rate_samples(await client.get_daily_heart_rate(day)))
            out.extend(hrv_samples(await client.get_hrv(day)))
            day += timedelta(days=1)
        return out

    def map_record(self, payload: dict) -> SampleRecord | None:
        return map_sample(payload)
