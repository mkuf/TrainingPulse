"""Tests for Garmin sleep, daily wellness, and intraday samples."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fixtures import payloads

from catalog import KINDS, DataKind, Dedup
from ingest.records import STORES
from providers import garmin_wellness as gw
from providers.feeds import METRIC_HEART_RATE, METRIC_HRV, SampleRecord, WellnessRecord
from providers.mapping import date_chunks, local_dates

BERLIN = ZoneInfo("Europe/Berlin")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class _Provider:
    """Stands in for ``GarminSource``: a client and a time zone."""

    def __init__(self, client, tz=BERLIN):
        self.client = client
        self.timezone = tz


def _window(first: date, last: date, tz=BERLIN) -> tuple[datetime, datetime]:
    """The engine's UTC window for whole local days first..last."""
    start = datetime.combine(first, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ── Dates ───────────────────────────────────────────────────────────


def test_local_dates_of_an_engine_window_are_exact():
    start, end = _window(date(2026, 10, 1), date(2026, 10, 29))
    assert local_dates(start, end, BERLIN) == (date(2026, 10, 1), date(2026, 10, 29))


def test_date_chunks_cover_the_range_without_overlap():
    chunks = list(date_chunks(date(2026, 1, 1), date(2026, 3, 1), 28))

    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 28))
    assert chunks[-1][1] == date(2026, 3, 1)
    assert all((b - a).days < 28 for a, b in chunks)
    for (_, end), (start, _) in zip(chunks, chunks[1:]):
        assert start == end + timedelta(days=1)


# ── Sleep ───────────────────────────────────────────────────────────


def test_sleep_maps_last_night_to_the_baseline():
    record = gw.map_sleep(payloads.GARMIN_SLEEP)

    assert record.external_id == "1793223963000"
    # Dated by the morning it ends.
    assert record.day == date(2026, 10, 29)
    assert record.start == _utc("2026-10-28T21:46:03Z")
    assert record.end == _utc("2026-10-29T06:01:03Z")
    assert record.asleep_seconds == 29520
    assert (record.deep_seconds, record.light_seconds, record.rem_seconds) == (
        7140,
        15240,
        7140,
    )
    assert record.awake_seconds == 180
    assert record.nap_seconds == 0
    # The Personal API has no stage timeline.
    assert record.stages is None


def test_a_day_without_sleep_is_skipped():
    assert gw.map_sleep(payloads.GARMIN_SLEEP_EMPTY) is None


def test_sleep_feed_requests_at_most_28_days_at_a_time():
    calls = []

    class Client:
        async def get_sleep_summaries(self, start_date, end_date):
            calls.append((start_date, end_date))
            return [payloads.GARMIN_SLEEP_EMPTY]

    start, end = _window(date(2026, 8, 1), date(2026, 10, 29))
    entries = asyncio.run(
        gw.GarminSleepFeed(_Provider(Client())).list_records(start, end)
    )

    assert calls[0][0] == date(2026, 8, 1)
    assert calls[-1][1] == date(2026, 10, 29)
    assert all((b - a).days < 28 for a, b in calls)
    assert len(entries) == len(calls)


# ── Daily wellness ──────────────────────────────────────────────────


def test_wellness_maps_the_baseline_and_ignores_garmin_scores():
    record = gw.map_wellness(
        {
            "calendarDate": "2026-10-29",
            "summary": payloads.GARMIN_DAILY_SUMMARY,
            "hrv": payloads.GARMIN_HRV_SUMMARY,
            "sleep": payloads.GARMIN_SLEEP,
        }
    )

    assert record == WellnessRecord(
        day=date(2026, 10, 29),
        resting_hr=44,
        hrv_ms=81,
        # From the night, not the whole-day average (15).
        respiration=16,
        # Pulse Ox off: no SpO2, and Garmin has no skin temperature.
        spo2_avg=None,
        spo2_min=None,
        skin_temp_deviation_c=None,
        steps=311,
        active_kcal=13,
        total_kcal=905,
    )


def test_pulse_ox_values_are_read_when_present():
    summary = {
        **payloads.GARMIN_DAILY_SUMMARY,
        "pulseOx": {"avgValue": 95, "minValue": 89},
    }
    record = gw.map_wellness({"calendarDate": "2026-10-29", "summary": summary})

    assert (record.spo2_avg, record.spo2_min) == (95, 89)


def test_negative_garmin_values_mean_no_data():
    summary = {**payloads.GARMIN_DAILY_SUMMARY, "heartRate": {"restingValue": -1}}
    record = gw.map_wellness({"calendarDate": "2026-10-29", "summary": summary})

    assert record.resting_hr is None


def test_a_day_without_any_value_is_skipped():
    empty = {"heartRate": {}, "movement": {}, "calories": {}}
    assert gw.map_wellness({"calendarDate": "2026-10-29", "summary": empty}) is None


def test_wellness_feed_joins_summaries_hrv_and_sleep_by_day():
    class Client:
        async def get_daily_summaries(self, start_date, end_date):
            return [payloads.GARMIN_DAILY_SUMMARY]

        async def get_hrv_summaries(self, start_date, end_date):
            return [
                payloads.GARMIN_HRV_SUMMARY,
                {**payloads.GARMIN_HRV_SUMMARY, "calendarDate": "2026-10-28"},
            ]

        async def get_sleep_summaries(self, start_date, end_date):
            return [payloads.GARMIN_SLEEP_EMPTY, payloads.GARMIN_SLEEP]

    start, end = _window(date(2026, 10, 28), date(2026, 10, 29))
    merged = asyncio.run(
        gw.GarminWellnessFeed(_Provider(Client())).list_records(start, end)
    )

    assert [entry["calendarDate"] for entry in merged] == ["2026-10-28", "2026-10-29"]
    assert "summary" not in merged[0]
    assert merged[1]["summary"] is payloads.GARMIN_DAILY_SUMMARY
    assert merged[1]["sleep"] is payloads.GARMIN_SLEEP


# ── Intraday samples ────────────────────────────────────────────────


def test_heart_rate_pairs_become_samples_and_gaps_are_dropped():
    samples = [
        gw.map_sample(payload)
        for payload in gw.heart_rate_samples(payloads.GARMIN_HEART_RATE_DAY)
    ]

    assert samples[0] == SampleRecord(
        metric=METRIC_HEART_RATE,
        measured_at=datetime.fromtimestamp(1_793_228_400, tz=timezone.utc),
        value=56,
    )
    assert samples[2] is None


def test_hrv_readings_use_the_utc_reading_time():
    (first, _) = [
        gw.map_sample(payload) for payload in gw.hrv_samples(payloads.GARMIN_HRV_DAY)
    ]

    assert first.metric == METRIC_HRV
    assert first.measured_at == _utc("2026-10-28T21:49:08Z")
    assert first.value == 68


def test_intraday_feed_requests_heart_rate_and_hrv_once_per_day():
    calls = []

    class Client:
        async def get_daily_heart_rate(self, day):
            calls.append(("hr", day))
            return payloads.GARMIN_HEART_RATE_DAY

        async def get_hrv(self, day):
            calls.append(("hrv", day))
            return payloads.GARMIN_HRV_DAY

    start, end = _window(date(2026, 10, 28), date(2026, 10, 29))
    raw = asyncio.run(
        gw.GarminIntradayFeed(_Provider(Client())).list_records(start, end)
    )

    assert calls == [
        ("hr", date(2026, 10, 28)),
        ("hrv", date(2026, 10, 28)),
        ("hr", date(2026, 10, 29)),
        ("hrv", date(2026, 10, 29)),
    ]
    assert {payload["metric"] for payload in raw} == {METRIC_HEART_RATE, METRIC_HRV}


def test_samples_get_their_local_day():
    """An HRV reading at 22:49 Berlin time belongs to the evening's day."""
    record = gw.map_sample(gw.hrv_samples(payloads.GARMIN_HRV_DAY)[0])
    values = STORES[DataKind.INTRADAY].to_values(record, "garmin", BERLIN)

    assert values["day"] == date(2026, 10, 28)
    assert values["provider"] == "garmin"


def test_sleep_rows_keep_the_providers_wake_day():
    record = gw.map_sleep(payloads.GARMIN_SLEEP)
    values = STORES[DataKind.SLEEP].to_values(record, "garmin", ZoneInfo("UTC"))

    assert values["day"] == date(2026, 10, 29)
    assert values["start_at"] == record.start
    assert values["stages"] is None


@pytest.mark.parametrize("kind", [DataKind.SLEEP, DataKind.WELLNESS, DataKind.INTRADAY])
def test_wellness_kinds_are_stored_and_deduplicated_per_day(kind):
    assert KINDS[kind].dedup is Dedup.PER_DAY
    assert hasattr(STORES[kind].model, "day")
    assert hasattr(STORES[kind].model, "superseded_by")
