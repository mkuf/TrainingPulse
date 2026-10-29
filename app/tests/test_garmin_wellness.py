"""Tests for Garmin sleep, daily wellness, and intraday samples."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fixtures import payloads

from catalog import DataKind
from ingest.records import STORES
from providers import garmin_wellness as gw
from providers.feeds import (
    METRIC_BODY_BATTERY,
    METRIC_HEART_RATE,
    METRIC_HRV,
    SampleRecord,
    WellnessRecord,
)

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
    assert gw.local_dates(start, end, BERLIN) == (date(2026, 10, 1), date(2026, 10, 29))


def test_date_chunks_cover_the_range_without_overlap():
    chunks = list(gw.date_chunks(date(2026, 1, 1), date(2026, 3, 1), 28))

    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 28))
    assert chunks[-1][1] == date(2026, 3, 1)
    assert all((b - a).days < 28 for a, b in chunks)
    for (_, end), (start, _) in zip(chunks, chunks[1:]):
        assert start == end + timedelta(days=1)


# ── Sleep ───────────────────────────────────────────────────────────


def test_sleep_maps_last_night():
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
    assert record.score == 100
    assert record.avg_heart_rate == 50
    assert (record.min_respiration, record.avg_respiration, record.max_respiration) == (
        9,
        16,
        23,
    )
    assert record.avg_stress == 5
    assert record.awake_count == 0
    assert record.nap_seconds == 0


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


def test_wellness_merges_summary_and_hrv():
    record = gw.map_wellness(
        {
            "calendarDate": "2026-10-29",
            "summary": payloads.GARMIN_DAILY_SUMMARY,
            "hrv": payloads.GARMIN_HRV_SUMMARY,
        }
    )

    assert record == WellnessRecord(
        day=date(2026, 10, 29),
        resting_hr=44,
        min_hr=43,
        max_hr=78,
        hrv_avg_ms=81,
        hrv_5min_high_ms=127,
        # First night: no weekly average, baseline, or status yet.
        hrv_weekly_avg_ms=None,
        hrv_baseline_low_ms=None,
        hrv_baseline_high_ms=None,
        hrv_status=None,
        avg_stress=6,
        max_stress=60,
        body_battery_min=88,
        body_battery_max=99,
        body_battery_charged=11,
        body_battery_drained=0,
        avg_respiration=15,
        min_respiration=9,
        max_respiration=23,
        steps=311,
        active_kcal=13,
        total_kcal=905,
        moderate_intensity_minutes=0,
        vigorous_intensity_minutes=0,
    )


def test_wellness_reads_an_established_hrv_baseline_and_status():
    hrv = {
        **payloads.GARMIN_HRV_SUMMARY,
        "weeklyAvg": 74,
        "status": "BALANCED",
        "baseline": {"lowUpper": 60, "balancedLow": 65, "balancedUpper": 85},
    }
    record = gw.map_wellness({"calendarDate": "2026-11-20", "hrv": hrv})

    assert record.hrv_weekly_avg_ms == 74
    assert (record.hrv_baseline_low_ms, record.hrv_baseline_high_ms) == (65, 85)
    assert record.hrv_status == "BALANCED"
    # No daily summary that day: those fields stay empty.
    assert record.resting_hr is None


def test_negative_garmin_values_mean_no_data():
    summary = {
        **payloads.GARMIN_DAILY_SUMMARY,
        "stress": {"avgLevel": -1, "maxLevel": -2},
    }
    record = gw.map_wellness({"calendarDate": "2026-10-29", "summary": summary})

    assert record.avg_stress is None
    assert record.max_stress is None


def test_a_day_without_any_value_is_skipped():
    empty = {"heartRate": {}, "stress": {}, "bodyBattery": {}, "movement": {}}
    assert gw.map_wellness({"calendarDate": "2026-10-29", "summary": empty}) is None


def test_wellness_feed_joins_both_endpoints_by_day():
    class Client:
        async def get_daily_summaries(self, start_date, end_date):
            return [payloads.GARMIN_DAILY_SUMMARY]

        async def get_hrv_summaries(self, start_date, end_date):
            return [
                payloads.GARMIN_HRV_SUMMARY,
                {**payloads.GARMIN_HRV_SUMMARY, "calendarDate": "2026-10-28"},
            ]

    start, end = _window(date(2026, 10, 28), date(2026, 10, 29))
    merged = asyncio.run(
        gw.GarminWellnessFeed(_Provider(Client())).list_records(start, end)
    )

    assert [entry["calendarDate"] for entry in merged] == ["2026-10-28", "2026-10-29"]
    assert "summary" not in merged[0]
    assert merged[1]["summary"] is payloads.GARMIN_DAILY_SUMMARY
    assert merged[1]["hrv"]["lastNightAvg"] == 81


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


def test_body_battery_skips_nulls():
    raw = [
        sample
        for report in payloads.GARMIN_BODY_BATTERY
        for sample in gw.body_battery_samples(report)
    ]
    mapped = [gw.map_sample(payload) for payload in raw]

    assert mapped[0] is None
    assert {record.metric for record in mapped[1:]} == {METRIC_BODY_BATTERY}
    assert [record.value for record in mapped[1:]] == [88, 99, 99]


def test_intraday_feed_requests_each_day_once():
    days = []

    class Client:
        async def get_daily_heart_rate(self, day):
            days.append(("hr", day))
            return payloads.GARMIN_HEART_RATE_DAY

        async def get_hrv(self, day):
            days.append(("hrv", day))
            return payloads.GARMIN_HRV_DAY

        async def get_body_battery(self, start_date, end_date):
            days.append(("bb", start_date, end_date))
            return payloads.GARMIN_BODY_BATTERY

    start, end = _window(date(2026, 10, 28), date(2026, 10, 29))
    raw = asyncio.run(
        gw.GarminIntradayFeed(_Provider(Client())).list_records(start, end)
    )

    assert days == [
        ("hr", date(2026, 10, 28)),
        ("hrv", date(2026, 10, 28)),
        ("hr", date(2026, 10, 29)),
        ("hrv", date(2026, 10, 29)),
        ("bb", date(2026, 10, 28), date(2026, 10, 29)),
    ]
    assert {payload["metric"] for payload in raw} == {
        METRIC_HEART_RATE,
        METRIC_HRV,
        METRIC_BODY_BATTERY,
    }


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


@pytest.mark.parametrize("kind", [DataKind.SLEEP, DataKind.WELLNESS, DataKind.INTRADAY])
def test_new_kinds_are_stored_and_deduplicated_per_day(kind):
    from catalog import KINDS, Dedup

    assert KINDS[kind].dedup is Dedup.PER_DAY
    assert hasattr(STORES[kind].model, "day")
    assert hasattr(STORES[kind].model, "superseded_by")
