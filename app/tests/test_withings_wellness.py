"""Tests for Withings sleep, daily wellness, intraday samples, and scopes."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fixtures import payloads

from catalog import DataKind
from ingest.records import STORES
from providers import withings_wellness as ww
from providers.feeds import (
    METRIC_HEART_RATE,
    METRIC_HRV,
    METRIC_SPO2,
    WellnessRecord,
    heart_rate_windows,
)
from providers.withings_client import (
    KIND_SCOPES,
    LEGACY_SCOPES,
    SCOPE_ACTIVITY,
    _scope_list,
    scopes_for,
)

BERLIN = ZoneInfo("Europe/Berlin")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class _Provider:
    def __init__(self, client, tz=BERLIN):
        self.client = client
        self.timezone = tz


def _window(first: date, last: date, tz=BERLIN) -> tuple[datetime, datetime]:
    start = datetime.combine(first, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _night(naps=(), segments=payloads.WITHINGS_SLEEP_SEGMENTS) -> dict:
    return {
        "day": "2026-10-28",
        "main": payloads.WITHINGS_SLEEP_NIGHT,
        "naps": list(naps),
        "segments": segments,
    }


# ── Sleep ───────────────────────────────────────────────────────────


def test_a_night_is_dated_by_its_wake_day_in_its_own_time_zone():
    summary = {**payloads.WITHINGS_SLEEP_NIGHT, "date": "2026-10-27"}
    assert ww.wake_day(summary, ZoneInfo("UTC")) == date(2026, 10, 28)

    # 05:10 UTC is still the 27th in Los Angeles.
    elsewhere = {**summary, "timezone": "America/Los_Angeles"}
    assert ww.wake_day(elsewhere, BERLIN) == date(2026, 10, 27)


def test_the_longest_sleep_of_a_day_is_the_night_and_the_rest_are_naps():
    (night,) = ww.group_nights(
        [payloads.WITHINGS_SLEEP_NAP, payloads.WITHINGS_SLEEP_NIGHT], BERLIN
    )

    assert night["day"] == "2026-10-28"
    assert night["main"] is payloads.WITHINGS_SLEEP_NIGHT
    assert night["naps"] == [payloads.WITHINGS_SLEEP_NAP]


def test_night_maps_totals_naps_and_the_stage_timeline():
    record = ww.map_sleep(_night(naps=[payloads.WITHINGS_SLEEP_NAP]))

    assert record.external_id == "3812345678"
    assert record.day == date(2026, 10, 28)
    assert record.start == _utc("2026-10-27T21:40:00Z")
    assert record.end == _utc("2026-10-28T05:10:00Z")
    assert record.asleep_seconds == 27000
    assert (record.deep_seconds, record.light_seconds, record.rem_seconds) == (
        5400,
        15600,
        6000,
    )
    assert record.awake_seconds == 1200
    assert record.nap_seconds == 1800
    assert [stage.stage for stage in record.stages] == [
        "awake",
        "light",
        "deep",
        "rem",
        "awake",
    ]
    assert record.stages[1].start == _utc("2026-10-27T21:52:00Z")


def test_a_night_from_an_external_source_has_only_its_sleep_time():
    external = {
        **payloads.WITHINGS_SLEEP_NIGHT,
        "data": {"total_sleep_time": None, "asleepduration": 25200},
    }
    record = ww.map_sleep({"day": "2026-10-28", "main": external, "naps": []})

    assert record.asleep_seconds == 25200
    assert record.deep_seconds is None
    assert record.nap_seconds == 0
    assert record.stages is None


def test_stage_timeline_is_stored_as_json():
    record = ww.map_sleep(_night())
    values = STORES[DataKind.SLEEP].to_values(record, "withings", BERLIN)

    assert values["stages"][0] == {
        "start": "2026-10-27T21:40:00+00:00",
        "end": "2026-10-27T21:52:00+00:00",
        "stage": "awake",
    }


def test_sleep_feed_lists_the_day_before_and_fetches_each_nights_stages():
    summary_calls, sleep_calls = [], []

    class Client:
        async def get_sleep_summary(self, *, startdateymd, enddateymd, offset=None):
            summary_calls.append((startdateymd, enddateymd))
            return {
                "series": [payloads.WITHINGS_SLEEP_NIGHT, payloads.WITHINGS_SLEEP_NAP],
                "more": False,
            }

        async def get_sleep(self, *, startdate, enddate):
            sleep_calls.append((startdate, enddate))
            return {"series": payloads.WITHINGS_SLEEP_SEGMENTS}

    start, end = _window(date(2026, 10, 28), date(2026, 10, 28))
    (night,) = asyncio.run(
        ww.WithingsSleepFeed(_Provider(Client())).list_records(start, end)
    )

    assert summary_calls == [("2026-10-27", "2026-10-28")]
    assert sleep_calls == [(1793137200, 1793164200)]
    assert night["segments"] == payloads.WITHINGS_SLEEP_SEGMENTS
    assert ww.map_sleep(night).day == date(2026, 10, 28)


# ── Daily wellness ──────────────────────────────────────────────────


def test_wellness_maps_activity_totals_and_sleep_respiration():
    record = ww.map_wellness(
        {
            "date": "2026-10-28",
            "activity": payloads.WITHINGS_ACTIVITY_DAY,
            "sleep": payloads.WITHINGS_SLEEP_NIGHT,
        }
    )

    assert record == WellnessRecord(
        day=date(2026, 10, 28),
        respiration=14,
        steps=8421,
        active_kcal=512.3,
        total_kcal=2380.6,
    )


def test_a_day_nothing_was_worn_is_skipped():
    payload = {"date": "2026-10-29", "activity": payloads.WITHINGS_ACTIVITY_NOT_WORN}
    assert ww.map_wellness(payload) is None


def test_a_night_still_counts_on_a_day_without_activity():
    record = ww.map_wellness(
        {
            "date": "2026-10-29",
            "activity": payloads.WITHINGS_ACTIVITY_NOT_WORN,
            "sleep": payloads.WITHINGS_SLEEP_NIGHT,
        }
    )

    assert record == WellnessRecord(day=date(2026, 10, 29), respiration=14)


def test_wellness_feed_joins_activity_and_sleep_by_day():
    class Client:
        async def get_activity(self, *, startdateymd, enddateymd, offset=None):
            pages = {
                None: {"activities": [payloads.WITHINGS_ACTIVITY_DAY], "more": True, "offset": 1},
                1: {"activities": [payloads.WITHINGS_ACTIVITY_NOT_WORN], "more": False},
            }
            return pages[offset]

        async def get_sleep_summary(self, *, startdateymd, enddateymd, offset=None):
            return {"series": [payloads.WITHINGS_SLEEP_NIGHT], "more": False}

    start, end = _window(date(2026, 10, 28), date(2026, 10, 29))
    days = asyncio.run(
        ww.WithingsWellnessFeed(_Provider(Client())).list_records(start, end)
    )

    assert [day["date"] for day in days] == ["2026-10-28", "2026-10-29"]
    assert days[0]["activity"] is payloads.WITHINGS_ACTIVITY_DAY
    assert days[0]["sleep"] is payloads.WITHINGS_SLEEP_NIGHT
    assert "sleep" not in days[1]


# ── Intraday ────────────────────────────────────────────────────────


def test_heart_rate_is_averaged_into_two_minute_windows():
    points = {1793174400: 64, 1793174460: 68, 1793174610: 70}
    assert heart_rate_windows(points) == [(1793174400, 66.0), (1793174520, 70.0)]


def test_intraday_series_become_heart_rate_hrv_and_spo2_samples():
    raw = ww.samples_for(
        payloads.WITHINGS_INTRADAY_SERIES, [], 1793142000, 1793228400
    )
    records = [ww.map_sample(payload) for payload in raw]

    assert [(r.metric, r.measured_at, r.value) for r in records] == [
        (METRIC_HEART_RATE, _utc("2026-10-28T08:00:00Z"), 66.0),
        (METRIC_HEART_RATE, _utc("2026-10-28T08:02:00Z"), 70.0),
        (METRIC_HRV, _utc("2026-10-28T08:01:00Z"), 41),
        (METRIC_SPO2, _utc("2026-10-28T08:10:00Z"), 97),
    ]


def test_a_sleep_mat_fills_a_night_the_watch_did_not_record():
    raw = ww.samples_for({}, payloads.WITHINGS_SLEEP_SEGMENTS, 1793055600, 1793142000)

    assert [(p["metric"], p["at"], p["value"]) for p in raw] == [
        (METRIC_HEART_RATE, 1793137200, 62.0),
        (METRIC_HEART_RATE, 1793137920, 57.0),
        (METRIC_HRV, 1793137920, 48),
    ]


def test_a_watch_reading_in_a_segment_keeps_the_mat_out_of_it():
    watch = {"1793137950": {"heart_rate": 60}}
    raw = ww.samples_for(
        watch, payloads.WITHINGS_SLEEP_SEGMENTS, 1793055600, 1793142000
    )

    heart = [(p["at"], p["value"]) for p in raw if p["metric"] == METRIC_HEART_RATE]
    # The awake segment has no watch reading, so the mat's 62 stays.
    assert heart == [(1793137200, 62.0), (1793137920, 60.0)]


def test_zero_readings_are_skipped():
    assert ww.map_sample({"metric": METRIC_SPO2, "at": 1793175000, "value": 0}) is None


def test_intraday_feed_splits_a_long_day_into_24_hour_requests():
    intraday_calls, sleep_calls = [], []

    class Client:
        async def get_intraday_activity(self, *, startdate, enddate):
            intraday_calls.append((startdate, enddate))
            return {"series": payloads.WITHINGS_INTRADAY_SERIES}

        async def get_sleep(self, *, startdate, enddate):
            sleep_calls.append((startdate, enddate))
            return {"series": []}

    # 2026-10-25 in Berlin has 25 hours.
    start, end = _window(date(2026, 10, 25), date(2026, 10, 25))
    asyncio.run(ww.WithingsIntradayFeed(_Provider(Client())).list_records(start, end))

    first = int(start.timestamp())
    assert intraday_calls == [
        (first, first + 86_399),
        (first + 86_400, first + 90_000 - 1),
    ]
    assert sleep_calls == intraday_calls


# ── Scopes ──────────────────────────────────────────────────────────


def test_only_the_imported_kinds_scopes_are_requested():
    assert scopes_for(["body"]) == "user.info,user.metrics"
    assert scopes_for(["body", "sleep"]) == "user.activity,user.info,user.metrics"
    assert SCOPE_ACTIVITY not in scopes_for(["body"])


def test_tokens_from_before_scopes_were_recorded_read_measurements_only():
    readable = {kind for kind, scopes in KIND_SCOPES.items() if scopes <= LEGACY_SCOPES}
    assert readable == {"body", "blood_pressure"}


def test_withings_scope_list_is_stored_space_separated():
    assert _scope_list("user.info, user.metrics,user.activity") == (
        "user.info user.metrics user.activity"
    )
    assert _scope_list(None) is None
