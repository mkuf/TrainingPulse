"""Garmin sleep, daily wellness, and intraday samples via the Personal API.

Field names and units were recorded from a real Personal API account (a
Garmin CIRQA band). The Personal API serves only a fixed set of endpoints:
sleep stage, stress, and respiration timelines, sleep movement, and daily
wellness FIT exports all return 404, so a night is rebuilt from heart rate,
HRV, and Body Battery samples plus the per-night totals.

Garmin assigns every daily value to a calendar day in the user's Garmin time
zone, which is assumed to be ``athlete.timezone``. A night belongs to the day
it ends, and so does its HRV.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterator
from zoneinfo import ZoneInfo

from catalog import DataKind
from providers.feeds import (
    METRIC_BODY_BATTERY,
    METRIC_HEART_RATE,
    METRIC_HRV,
    SampleRecord,
    SleepRecord,
    WellnessRecord,
)
from providers.mapping import as_float, as_int, parse_dt

if TYPE_CHECKING:
    from providers.garmin import GarminSource

#: Sleep summaries reject ranges over 31 days.
_SLEEP_CHUNK_DAYS = 28
#: Daily and HRV summaries accept a year; the engine's windows are shorter.
_SUMMARY_CHUNK_DAYS = 90
#: Body Battery reports: only short ranges were tested, so stay modest.
_BODY_BATTERY_CHUNK_DAYS = 28


def local_dates(start: datetime, end: datetime, tz: ZoneInfo) -> tuple[date, date]:
    """Inclusive local dates covered by the half-open UTC window ``[start, end)``."""
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


def _epoch_ms(raw: object) -> datetime | None:
    value = as_float(raw)
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def _non_negative(raw: object) -> float | None:
    """Garmin uses negative numbers (-1, -2) for "not enough data"."""
    value = as_float(raw)
    return value if value is not None and value >= 0 else None


def _optional_int(raw: object) -> int | None:
    value = as_float(raw)
    return None if value is None else as_int(value)


# ── Sleep ───────────────────────────────────────────────────────────


def map_sleep(payload: dict) -> SleepRecord | None:
    """Map one ``dailySleepSummariesByDate`` entry onto a sleep record.

    Pure. Days without a sleep come back with every field null and are
    skipped.
    """
    sleep_id = payload.get("id")
    start = _epoch_ms(payload.get("sleepStartTimestampGMT"))
    end = _epoch_ms(payload.get("sleepEndTimestampGMT"))
    raw_day = payload.get("calendarDate")
    if sleep_id is None or start is None or end is None or not raw_day:
        return None
    overall = ((payload.get("sleepScores") or {}).get("overall") or {}).get("value")
    return SleepRecord(
        external_id=str(sleep_id),
        day=date.fromisoformat(str(raw_day)),
        start=start,
        end=end,
        asleep_seconds=_optional_int(payload.get("sleepTimeSeconds")),
        deep_seconds=_optional_int(payload.get("deepSleepSeconds")),
        light_seconds=_optional_int(payload.get("lightSleepSeconds")),
        rem_seconds=_optional_int(payload.get("remSleepSeconds")),
        awake_seconds=_optional_int(payload.get("awakeSleepSeconds")),
        score=as_float(overall),
        avg_heart_rate=as_float(payload.get("avgHeartRate")),
        avg_respiration=as_float(payload.get("averageRespirationValue")),
        min_respiration=as_float(payload.get("lowestRespirationValue")),
        max_respiration=as_float(payload.get("highestRespirationValue")),
        avg_stress=_non_negative(payload.get("avgSleepStress")),
        awake_count=_optional_int(payload.get("awakeCount")),
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
    """Map a merged ``{calendarDate, summary, hrv}`` payload onto a record.

    ``summary`` is one ``v2/dailysummaries`` entry and ``hrv`` one
    ``hrvSummaries`` entry for the same day; either may be empty. Pure.
    """
    raw_day = payload.get("calendarDate")
    if not raw_day:
        return None
    summary = payload.get("summary") or {}
    hrv = payload.get("hrv") or {}

    heart = summary.get("heartRate") or {}
    stress = summary.get("stress") or {}
    battery = summary.get("bodyBattery") or {}
    respiration = summary.get("respiration") or {}
    steps = ((summary.get("movement") or {}).get("steps") or {}).get("value")
    calories = summary.get("calories") or {}
    intensity = summary.get("intensityMinutes") or {}
    baseline = hrv.get("baseline") or {}
    status = hrv.get("status")

    record = WellnessRecord(
        day=date.fromisoformat(str(raw_day)),
        resting_hr=_non_negative(heart.get("restingValue")),
        min_hr=_non_negative(heart.get("minValue")),
        max_hr=_non_negative(heart.get("maxValue")),
        hrv_avg_ms=as_float(hrv.get("lastNightAvg")),
        hrv_5min_high_ms=as_float(hrv.get("lastNight5MinHigh")),
        hrv_weekly_avg_ms=as_float(hrv.get("weeklyAvg")),
        hrv_baseline_low_ms=as_float(baseline.get("balancedLow")),
        hrv_baseline_high_ms=as_float(baseline.get("balancedUpper")),
        hrv_status=str(status)[:32] if status and status != "NONE" else None,
        avg_stress=_non_negative(stress.get("avgLevel")),
        max_stress=_non_negative(stress.get("maxLevel")),
        body_battery_min=_non_negative(battery.get("minValue")),
        body_battery_max=_non_negative(battery.get("maxValue")),
        body_battery_charged=_non_negative(battery.get("chargedValue")),
        body_battery_drained=_non_negative(battery.get("drainedValue")),
        avg_respiration=_non_negative(respiration.get("avgValue")),
        min_respiration=_non_negative(respiration.get("minValue")),
        max_respiration=_non_negative(respiration.get("maxValue")),
        steps=_optional_int(steps),
        active_kcal=as_float(calories.get("burnedActive")),
        total_kcal=as_float(calories.get("burnedTotal")),
        moderate_intensity_minutes=_optional_int(intensity.get("moderate")),
        vigorous_intensity_minutes=_optional_int(intensity.get("vigorous")),
    )
    measured = {
        name: value
        for name, value in vars(record).items()
        if name != "day" and value is not None
    }
    return record if measured else None


class GarminWellnessFeed:
    """Garmin daily summaries joined with overnight HRV, as a ``WellnessFeed``."""

    kind = DataKind.WELLNESS

    def __init__(self, provider: "GarminSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        first, last = local_dates(start, end, self._provider.timezone)
        by_day: dict[str, dict] = {}
        for chunk_start, chunk_end in date_chunks(first, last, _SUMMARY_CHUNK_DAYS):
            for summary in await client.get_daily_summaries(chunk_start, chunk_end):
                day = summary.get("calendarDate")
                if day:
                    by_day.setdefault(day, {"calendarDate": day})["summary"] = summary
            for hrv in await client.get_hrv_summaries(chunk_start, chunk_end):
                day = hrv.get("calendarDate")
                if day:
                    by_day.setdefault(day, {"calendarDate": day})["hrv"] = hrv
        return [by_day[day] for day in sorted(by_day)]

    def map_record(self, payload: dict) -> WellnessRecord | None:
        return map_wellness(payload)


# ── Intraday samples ────────────────────────────────────────────────


def heart_rate_samples(report: dict) -> list[dict]:
    """``dailyHeartRate`` values as sample payloads: ``[epoch ms, bpm]`` pairs."""
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


def body_battery_samples(report: dict) -> list[dict]:
    """One Body Battery report's ``[epoch ms, level]`` pairs as sample payloads."""
    return [
        {"metric": METRIC_BODY_BATTERY, "at": pair[0], "value": pair[1]}
        for pair in report.get("bodyBatteryValuesArray") or []
        if isinstance(pair, list) and len(pair) >= 2
    ]


def map_sample(payload: dict) -> SampleRecord | None:
    """Map a sample payload; ``at`` is epoch milliseconds or a UTC timestamp.

    Pure. Missing, zero, and negative values are Garmin's "no reading".
    """
    value = as_float(payload.get("value"))
    raw_at = payload.get("at")
    if value is None or value <= 0 or raw_at is None:
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
    """Heart rate, HRV, and Body Battery samples as an ``IntradayFeed``.

    Costs two requests per day (heart rate, HRV) plus one Body Battery
    request per four weeks, which is why the kind's default history is short.
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
        for chunk_start, chunk_end in date_chunks(
            first, last, _BODY_BATTERY_CHUNK_DAYS
        ):
            for report in await client.get_body_battery(chunk_start, chunk_end):
                out.extend(body_battery_samples(report))
        return out

    def map_record(self, payload: dict) -> SampleRecord | None:
        return map_sample(payload)
