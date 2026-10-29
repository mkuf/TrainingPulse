"""Google Health sleep, daily wellness, and intraday samples.

Shapes and units were recorded from a real account with Fitbit Air data.
Only the baseline in :mod:`providers.feeds` is mapped. Google's data types
bring several Fitbit-specific extras (HRV entropy, per-stage respiration,
skin temperature standard deviation) that are deliberately left out.

Google returns civil (local) dates with every daily value and a UTC offset
with every timestamp, so a night's wake day comes from the data itself.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from catalog import DataKind
from providers.feeds import (
    HEART_RATE_WINDOW_SECONDS,
    METRIC_HEART_RATE,
    METRIC_HRV,
    METRIC_SPO2,
    SampleRecord,
    SleepRecord,
    SleepStage,
    WellnessRecord,
)
from providers.mapping import as_float, as_int, date_chunks, local_dates, parse_dt

if TYPE_CHECKING:
    from providers.google_health import GoogleHealthSource

#: Google stage names → the baseline's. RESTLESS only appears in "classic"
#: logs from older devices and counts as awake.
_STAGE_NAMES = {
    "AWAKE": "awake",
    "RESTLESS": "awake",
    "LIGHT": "light",
    "DEEP": "deep",
    "REM": "rem",
    "ASLEEP": "asleep",
}

#: Daily data types listed per window, and the key their payload sits under.
DAILY_TYPES = {
    "daily-resting-heart-rate": "dailyRestingHeartRate",
    "daily-heart-rate-variability": "dailyHeartRateVariability",
    "daily-respiratory-rate": "dailyRespiratoryRate",
    "daily-oxygen-saturation": "dailyOxygenSaturation",
    "daily-sleep-temperature-derivations": "dailySleepTemperatureDerivations",
}
#: Daily totals taken from Google's rollup, which de-duplicates the band and
#: the phone; listing raw points would count steps twice.
ROLLUP_TYPES = {
    "steps": "steps",
    "active-energy-burned": "activeEnergyBurned",
    "total-calories": "totalCalories",
}

_WELLNESS_CHUNK_DAYS = 90


def _civil_day(raw: object) -> date | None:
    if not isinstance(raw, dict):
        return None
    try:
        return date(int(raw["year"]), int(raw["month"]), int(raw["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _offset(raw: object) -> timedelta:
    """A protobuf duration such as ``"3600s"``."""
    text = str(raw or "0s").rstrip("s")
    try:
        return timedelta(seconds=float(text))
    except ValueError:
        return timedelta(0)


def _minutes(raw: object) -> int | None:
    value = as_float(raw)
    return None if value is None else as_int(value) * 60


# ── Sleep ───────────────────────────────────────────────────────────


def wake_day(session: dict) -> date | None:
    """The local day a sleep session ends on."""
    interval = (session.get("sleep") or {}).get("interval") or {}
    end = parse_dt(interval.get("endTime"))
    if end is None:
        return None
    return (end + _offset(interval.get("endUtcOffset"))).date()


def _is_nap(session: dict) -> bool:
    return bool(((session.get("sleep") or {}).get("metadata") or {}).get("nap"))


def _asleep_minutes(session: dict) -> float:
    summary = (session.get("sleep") or {}).get("summary") or {}
    return as_float(summary.get("minutesAsleep")) or 0.0


def group_nights(sessions: list[dict]) -> list[dict]:
    """One ``{day, main, naps}`` payload per wake day that has a main sleep.

    Google flags the night as ``mainSleep`` and naps as ``nap``; a day whose
    sessions carry neither flag (a manual log) uses its longest session.
    """
    by_day: dict[date, list[dict]] = {}
    for session in sessions:
        day = wake_day(session)
        if day is not None:
            by_day.setdefault(day, []).append(session)
    nights = []
    for day in sorted(by_day):
        naps = [s for s in by_day[day] if _is_nap(s)]
        candidates = [s for s in by_day[day] if not _is_nap(s)]
        if not candidates:
            continue
        flagged = [
            s
            for s in candidates
            if ((s["sleep"].get("metadata") or {}).get("mainSleep"))
        ]
        main = max(flagged or candidates, key=_asleep_minutes)
        nights.append({"day": day.isoformat(), "main": main, "naps": naps})
    return nights


def map_sleep(payload: dict) -> SleepRecord | None:
    """Map a ``{day, main, naps}`` payload from :func:`group_nights`. Pure."""
    main = payload.get("main") or {}
    sleep = main.get("sleep") or {}
    interval = sleep.get("interval") or {}
    start = parse_dt(interval.get("startTime"))
    end = parse_dt(interval.get("endTime"))
    if start is None or end is None or not payload.get("day"):
        return None
    summary = sleep.get("summary") or {}
    by_stage = {
        str(entry.get("type")): _minutes(entry.get("minutes"))
        for entry in summary.get("stagesSummary") or []
    }
    stages = tuple(
        SleepStage(
            start=parse_dt(stage.get("startTime")),
            end=parse_dt(stage.get("endTime")),
            stage=_STAGE_NAMES[stage["type"]],
        )
        for stage in sleep.get("stages") or []
        if stage.get("type") in _STAGE_NAMES
        and parse_dt(stage.get("startTime"))
        and parse_dt(stage.get("endTime"))
    )
    nap_seconds = sum(
        int(_asleep_minutes(nap)) * 60 for nap in payload.get("naps") or []
    )
    name = str(main.get("name") or "").rstrip("/")
    return SleepRecord(
        external_id=(name.rsplit("/", 1)[-1] or start.isoformat())[:128],
        day=date.fromisoformat(payload["day"]),
        start=start,
        end=end,
        asleep_seconds=_minutes(summary.get("minutesAsleep")),
        deep_seconds=by_stage.get("DEEP"),
        light_seconds=by_stage.get("LIGHT"),
        rem_seconds=by_stage.get("REM"),
        awake_seconds=_minutes(summary.get("minutesAwake")),
        nap_seconds=nap_seconds,
        stages=stages or None,
    )


class GoogleSleepFeed:
    """Google Health sleep as a ``SleepFeed``: one main sleep per wake day,
    naps summed into it."""

    kind = DataKind.SLEEP

    def __init__(self, provider: "GoogleHealthSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        first, last = local_dates(start, end, self._provider.timezone)
        return group_nights(await self._provider.client.list_sleep(first, last))

    def map_record(self, payload: dict) -> SleepRecord | None:
        return map_sleep(payload)


# ── Daily wellness ──────────────────────────────────────────────────


def map_wellness(payload: dict) -> WellnessRecord | None:
    """Map a ``{date, values}`` payload; ``values`` holds each data type's
    body under its JSON key (``dailyRestingHeartRate``, ``steps``, ...). Pure.
    """
    raw_day = payload.get("date")
    if not raw_day:
        return None
    values = payload.get("values") or {}
    resting = values.get("dailyRestingHeartRate") or {}
    hrv = values.get("dailyHeartRateVariability") or {}
    respiration = values.get("dailyRespiratoryRate") or {}
    spo2 = values.get("dailyOxygenSaturation") or {}
    temperature = values.get("dailySleepTemperatureDerivations") or {}
    nightly = as_float(temperature.get("nightlyTemperatureCelsius"))
    baseline = as_float(temperature.get("baselineTemperatureCelsius"))
    steps = as_float((values.get("steps") or {}).get("countSum"))

    record = WellnessRecord(
        day=date.fromisoformat(str(raw_day)),
        resting_hr=as_float(resting.get("beatsPerMinute")),
        hrv_ms=as_float(hrv.get("averageHeartRateVariabilityMilliseconds")),
        respiration=as_float(respiration.get("breathsPerMinute")),
        spo2_avg=as_float(spo2.get("averagePercentage")),
        spo2_min=as_float(spo2.get("lowerBoundPercentage")),
        skin_temp_deviation_c=(
            round(nightly - baseline, 3)
            if nightly is not None and baseline is not None
            else None
        ),
        steps=None if steps is None else as_int(steps),
        active_kcal=as_float((values.get("activeEnergyBurned") or {}).get("kcalSum")),
        total_kcal=as_float((values.get("totalCalories") or {}).get("kcalSum")),
    )
    measured = [
        value
        for name, value in vars(record).items()
        if name != "day" and value is not None
    ]
    return record if measured else None


class GoogleWellnessFeed:
    """Google Health daily types and daily totals, joined by day."""

    kind = DataKind.WELLNESS

    def __init__(self, provider: "GoogleHealthSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        first, last = local_dates(start, end, self._provider.timezone)
        by_day: dict[date, dict] = {}

        def put(day: date | None, key: str, body: object) -> None:
            if day is not None and isinstance(body, dict):
                by_day.setdefault(day, {})[key] = body

        for chunk_start, chunk_end in date_chunks(first, last, _WELLNESS_CHUNK_DAYS):
            for data_type, key in DAILY_TYPES.items():
                for point in await client.list_daily(data_type, chunk_start, chunk_end):
                    body = point.get(key) or {}
                    put(_civil_day(body.get("date")), key, body)
            for data_type, key in ROLLUP_TYPES.items():
                for point in await client.daily_rollup(
                    data_type, chunk_start, chunk_end
                ):
                    day = _civil_day((point.get("civilStartTime") or {}).get("date"))
                    put(day, key, point.get(key))
        return [
            {"date": day.isoformat(), "values": by_day[day]}
            for day in sorted(by_day)
            if first <= day <= last
        ]

    def map_record(self, payload: dict) -> WellnessRecord | None:
        return map_wellness(payload)


# ── Intraday samples ────────────────────────────────────────────────


def heart_rate_samples(windows: list[dict]) -> list[dict]:
    """``rollUp`` windows as sample payloads, stamped at the window start."""
    return [
        {
            "metric": METRIC_HEART_RATE,
            "at": window.get("startTime"),
            "value": (window.get("heartRate") or {}).get("beatsPerMinuteAvg"),
        }
        for window in windows
    ]


def hrv_samples(points: list[dict]) -> list[dict]:
    return [
        {
            "metric": METRIC_HRV,
            "at": (
                (point.get("heartRateVariability") or {}).get("sampleTime") or {}
            ).get("physicalTime"),
            "value": (point.get("heartRateVariability") or {}).get(
                "rootMeanSquareOfSuccessiveDifferencesMilliseconds"
            ),
        }
        for point in points
    ]


def spo2_samples(points: list[dict]) -> list[dict]:
    return [
        {
            "metric": METRIC_SPO2,
            "at": ((point.get("oxygenSaturation") or {}).get("sampleTime") or {}).get(
                "physicalTime"
            ),
            "value": (point.get("oxygenSaturation") or {}).get("percentage"),
        }
        for point in points
    ]


def map_sample(payload: dict) -> SampleRecord | None:
    """Pure. Missing or non-positive values and timestamps are skipped."""
    value = as_float(payload.get("value"))
    measured_at = parse_dt(payload.get("at"))
    if value is None or value <= 0 or measured_at is None:
        return None
    return SampleRecord(
        metric=str(payload["metric"]),
        measured_at=measured_at.astimezone(timezone.utc),
        value=round(value, 2),
    )


class GoogleIntradayFeed:
    """Two-minute heart-rate averages plus HRV and SpO2 samples."""

    kind = DataKind.INTRADAY

    def __init__(self, provider: "GoogleHealthSource"):
        self._provider = provider

    async def list_records(self, start: datetime, end: datetime) -> list[dict]:
        client = self._provider.client
        out = heart_rate_samples(
            await client.heart_rate_rollup(
                start, end, window_seconds=HEART_RATE_WINDOW_SECONDS
            )
        )
        out.extend(
            hrv_samples(await client.list_samples("heart-rate-variability", start, end))
        )
        out.extend(
            spo2_samples(await client.list_samples("oxygen-saturation", start, end))
        )
        return out

    def map_record(self, payload: dict) -> SampleRecord | None:
        return map_sample(payload)
