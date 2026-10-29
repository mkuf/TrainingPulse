"""Tests for Google Health sleep, daily wellness, intraday samples, and scopes."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fixtures import payloads

from catalog import DataKind
from ingest.records import STORES
from providers import google_wellness as gw
from providers.feeds import METRIC_HEART_RATE, METRIC_HRV, METRIC_SPO2, WellnessRecord
from providers.google_health_client import (
    KIND_SCOPES,
    LEGACY_SCOPES,
    SCOPE_SLEEP,
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


# ── Sleep ───────────────────────────────────────────────────────────


def test_a_night_is_dated_by_its_local_wake_day():
    assert gw.wake_day(payloads.GOOGLE_SLEEP_MAIN) == date(2026, 10, 28)


def test_main_sleep_and_naps_of_a_day_become_one_night():
    (night,) = gw.group_nights([payloads.GOOGLE_SLEEP_NAP, payloads.GOOGLE_SLEEP_MAIN])

    assert night["day"] == "2026-10-28"
    assert night["main"] is payloads.GOOGLE_SLEEP_MAIN
    assert night["naps"] == [payloads.GOOGLE_SLEEP_NAP]


def test_a_day_with_only_naps_has_no_night():
    assert gw.group_nights([payloads.GOOGLE_SLEEP_NAP]) == []


def test_main_sleep_maps_totals_naps_and_the_stage_timeline():
    record = gw.map_sleep(
        {
            "day": "2026-10-28",
            "main": payloads.GOOGLE_SLEEP_MAIN,
            "naps": [payloads.GOOGLE_SLEEP_NAP],
        }
    )

    assert record.external_id == "8217586939298100001"
    assert record.day == date(2026, 10, 28)
    assert record.start == _utc("2026-10-27T21:50:00Z")
    assert record.end == _utc("2026-10-28T06:19:00Z")
    assert record.asleep_seconds == 494 * 60
    assert (record.deep_seconds, record.light_seconds, record.rem_seconds) == (
        100 * 60,
        274 * 60,
        120 * 60,
    )
    assert record.awake_seconds == 15 * 60
    assert record.nap_seconds == 98 * 60
    assert [stage.stage for stage in record.stages] == ["awake", "light", "deep"]
    assert record.stages[1].start == _utc("2026-10-27T21:56:00Z")


def test_a_classic_log_without_flags_still_maps():
    (night,) = gw.group_nights([payloads.GOOGLE_SLEEP_CLASSIC])
    record = gw.map_sleep(night)

    assert record.day == date(2026, 8, 16)
    assert record.asleep_seconds == 460 * 60
    assert record.deep_seconds is None
    assert [stage.stage for stage in record.stages] == ["asleep"]


def test_stage_timeline_is_stored_as_json():
    record = gw.map_sleep(
        {"day": "2026-10-28", "main": payloads.GOOGLE_SLEEP_MAIN, "naps": []}
    )
    values = STORES[DataKind.SLEEP].to_values(record, "google_health", BERLIN)

    assert values["stages"][0] == {
        "start": "2026-10-27T21:50:00+00:00",
        "end": "2026-10-27T21:56:00+00:00",
        "stage": "awake",
    }
    assert values["nap_seconds"] == 0


# ── Daily wellness ──────────────────────────────────────────────────


def _day_values() -> dict:
    values = {}
    for data_type, key in gw.DAILY_TYPES.items():
        values[key] = payloads.GOOGLE_DAILY[data_type][key]
    for data_type, key in gw.ROLLUP_TYPES.items():
        values[key] = payloads.GOOGLE_ROLLUPS[data_type][key]
    return values


def test_wellness_maps_every_daily_type_to_the_baseline():
    record = gw.map_wellness({"date": "2026-10-28", "values": _day_values()})

    assert record == WellnessRecord(
        day=date(2026, 10, 28),
        resting_hr=55,
        hrv_ms=90.55,
        respiration=17.6,
        spo2_avg=95.5,
        spo2_min=93,
        skin_temp_deviation_c=0.12,
        steps=4012,
        active_kcal=pytest.approx(434.538273),
        total_kcal=pytest.approx(3313.539294),
    )


def test_wellness_without_a_temperature_baseline_has_no_deviation():
    values = _day_values()
    values["dailySleepTemperatureDerivations"] = {"nightlyTemperatureCelsius": 33.6}
    record = gw.map_wellness({"date": "2026-10-28", "values": values})

    assert record.skin_temp_deviation_c is None


def test_wellness_feed_joins_daily_types_and_rollups_by_day():
    listed, rolled = [], []

    class Client:
        async def list_daily(self, data_type, first, last):
            listed.append(data_type)
            return [payloads.GOOGLE_DAILY[data_type]]

        async def daily_rollup(self, data_type, first, last):
            rolled.append(data_type)
            return [payloads.GOOGLE_ROLLUPS[data_type]]

    start, end = _window(date(2026, 10, 28), date(2026, 10, 28))
    (merged,) = asyncio.run(
        gw.GoogleWellnessFeed(_Provider(Client())).list_records(start, end)
    )

    assert set(listed) == set(gw.DAILY_TYPES)
    assert set(rolled) == set(gw.ROLLUP_TYPES)
    assert merged["date"] == "2026-10-28"
    assert set(merged["values"]) == set(_day_values())


# ── Intraday ────────────────────────────────────────────────────────


def test_heart_rate_windows_hrv_and_spo2_become_samples():
    heart = gw.map_sample(gw.heart_rate_samples([payloads.GOOGLE_HEART_RATE_WINDOW])[0])
    hrv = gw.map_sample(gw.hrv_samples([payloads.GOOGLE_HRV_SAMPLE])[0])
    spo2 = gw.map_sample(gw.spo2_samples([payloads.GOOGLE_SPO2_SAMPLE])[0])

    assert (heart.metric, heart.measured_at, heart.value) == (
        METRIC_HEART_RATE,
        _utc("2026-10-20T02:06:00Z"),
        46.25,
    )
    assert (hrv.metric, hrv.value) == (METRIC_HRV, 64.4)
    assert (spo2.metric, spo2.value) == (METRIC_SPO2, 89.9)


def test_intraday_feed_asks_for_two_minute_heart_rate_averages():
    windows = []

    class Client:
        async def heart_rate_rollup(self, start, end, *, window_seconds):
            windows.append(window_seconds)
            return [payloads.GOOGLE_HEART_RATE_WINDOW]

        async def list_samples(self, data_type, start, end):
            return {
                "heart-rate-variability": [payloads.GOOGLE_HRV_SAMPLE],
                "oxygen-saturation": [payloads.GOOGLE_SPO2_SAMPLE],
            }[data_type]

    start, end = _window(date(2026, 10, 20), date(2026, 10, 26))
    raw = asyncio.run(
        gw.GoogleIntradayFeed(_Provider(Client())).list_records(start, end)
    )

    assert windows == [120]
    assert [payload["metric"] for payload in raw] == [
        METRIC_HEART_RATE,
        METRIC_HRV,
        METRIC_SPO2,
    ]


# ── Scopes ──────────────────────────────────────────────────────────


def test_only_the_imported_kinds_scopes_are_requested():
    assert scopes_for(["activities"]).split() == sorted(KIND_SCOPES["activities"])
    assert SCOPE_SLEEP in scopes_for(["activities", "sleep"]).split()
    assert SCOPE_SLEEP not in scopes_for(["activities", "wellness"]).split()


def test_tokens_from_before_scopes_were_recorded_cannot_read_sleep():
    readable = {kind for kind, scopes in KIND_SCOPES.items() if scopes <= LEGACY_SCOPES}
    assert readable == {"activities", "wellness", "intraday"}


def test_a_transient_server_error_is_retried(monkeypatch):
    import httpx

    from providers import google_health_client as ghc

    monkeypatch.setattr(ghc, "_SERVER_ERROR_RETRY_DELAYS", (0.0, 0.0, None))
    monkeypatch.setattr(ghc, "_PACER", ghc.RequestPacer(0.0))
    statuses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), json={"dataPoints": []})

    client = ghc.GoogleHealthClient(None)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def token():
        return "token"

    client._ensure_valid_token = token

    async def run():
        try:
            return await client._request("GET", "/users/me/dataTypes/steps/dataPoints")
        finally:
            await client.close()

    assert asyncio.run(run()) == {"dataPoints": []}
