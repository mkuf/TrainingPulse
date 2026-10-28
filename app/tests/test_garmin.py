"""Tests for the Garmin Connect provider: mappers, feeds, and the client helpers."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fixtures import payloads
from providers import garmin
from providers.feeds import BodyRecord
from providers.garmin_client import (
    GarminAuthError,
    extract_fit,
    jwt_expiration,
)

ATHLETE_ID = 7


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ── Activities ──────────────────────────────────────────────────────


def test_search_payload_maps_provisional_values():
    row = garmin.map_row(payloads.GARMIN_SEARCH_ACTIVITY, ATHLETE_ID)

    assert row["id"] == "garmin_17123456789"
    assert row["source"] == "garmin"
    assert row["athlete_id"] == ATHLETE_ID
    assert row["name"] == "Morning Run"
    assert row["sport_type"] == "Run"
    # startTimeGMT has no offset but is UTC.
    assert row["start_date"] == _utc("2026-10-20T06:31:12Z")
    assert row["elapsed_time"] == 3180
    assert row["moving_time"] == 3080
    assert row["distance"] == pytest.approx(10234.5)
    assert row["average_heartrate"] == pytest.approx(151.0)
    assert row["has_heartrate"] is True
    assert row["total_elevation_gain"] == pytest.approx(84.0)
    # No power in the summary: no power, no work.
    assert row["average_watts"] is None
    assert row["device_watts"] is False
    assert row["kilojoules"] is None


def test_detail_with_fit_summary_prefers_fit_values():
    payload = {
        **garmin.flatten_detail(payloads.GARMIN_DETAIL_ACTIVITY),
        "_fit_summary": payloads.GARMIN_FIT_SUMMARY,
    }
    row = garmin.map_row(payload, ATHLETE_ID)

    assert row["description"] == "Easy loop"
    assert row["start_date"] == _utc("2026-10-20T06:31:10Z")
    assert row["elapsed_time"] == 3181
    assert row["moving_time"] == 3079
    assert row["distance"] == pytest.approx(10240.0)
    assert row["average_watts"] == pytest.approx(269.0)
    assert row["device_watts"] is True
    assert row["kilojoules"] == pytest.approx(269.0 * 3079 / 1000)
    assert row["weighted_average_watts"] == pytest.approx(276.0)
    assert row["device_name"] == "Garmin CIRQA"
    assert row["provider_updated_at"] == _utc("2026-10-20T08:10:00Z")


def test_detail_without_fit_falls_back_to_summary_dto():
    payload = {
        **garmin.flatten_detail(payloads.GARMIN_DETAIL_ACTIVITY),
        "_fit_summary": None,
    }
    row = garmin.map_row(payload, ATHLETE_ID)

    assert row["start_date"] == _utc("2026-10-20T06:31:12Z")
    assert row["sport_type"] == "Run"
    assert row["average_watts"] == pytest.approx(268.0)
    assert row["weighted_average_watts"] == pytest.approx(275.0)
    assert row["device_watts"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"activityName": "No id", "startTimeGMT": "2026-10-20 06:31:12"},
        {"activityId": 1, "activityName": "No start"},
    ],
    ids=["no id", "no start"],
)
def test_unusable_activity_payloads_are_skipped(payload):
    assert garmin.map_row(payload, ATHLETE_ID) is None


@pytest.mark.parametrize(
    ("type_key", "expected"),
    [("trail_running", "Trail Run"), ("gravel_cycling", "GravelRide"), ("golf", "")],
)
def test_sport_type(type_key, expected):
    assert garmin.sport_type(type_key) == expected


def test_list_activities_pages_and_trims_to_the_window(monkeypatch):
    monkeypatch.setattr(garmin, "_PER_PAGE", 2)
    start = _utc("2026-10-20T00:00:00Z")
    end = start + timedelta(days=1)

    def activity(n: int, when: str) -> dict:
        return {"activityId": n, "startTimeGMT": when}

    pages = {
        0: [activity(1, "2026-10-19 23:30:00"), activity(2, "2026-10-20 06:00:00")],
        2: [activity(3, "2026-10-20 18:00:00"), activity(4, "2026-10-21 00:00:00")],
        4: [],
    }
    calls = []

    class Client:
        async def search_activities(self, *, start, limit, start_date, end_date):
            calls.append((start, start_date, end_date))
            return pages[start]

    source = garmin.GarminSource(api_key="k")
    source._client = Client()
    items = asyncio.run(source.list_activities(start, end))

    assert [item["activityId"] for item in items] == [2, 3]
    assert [call[0] for call in calls] == [0, 2, 4]
    # Local-date filter is padded a day either side.
    assert calls[0][1:] == (
        start.date() - timedelta(days=1),
        end.date() + timedelta(days=1),
    )


# ── Body ────────────────────────────────────────────────────────────


def test_weight_metric_maps_grams_to_kilograms():
    record = garmin.map_weight_metric(payloads.GARMIN_WEIGHT_METRIC)

    assert record == BodyRecord(
        external_id="1792562400123",
        measured_at=datetime.fromtimestamp(1_792_562_400, tz=timezone.utc),
        weight_kg=81.25,
        fat_pct=18.4,
        muscle_mass_kg=36.1,
        device="INDEX_SCALE",
    )


def test_weight_metric_without_weight_is_skipped():
    metric = {**payloads.GARMIN_WEIGHT_METRIC, "weight": None}
    assert garmin.map_weight_metric(metric) is None


def test_body_feed_flattens_daily_summaries_and_trims_to_the_window():
    inside = payloads.GARMIN_WEIGHT_METRIC
    outside = {
        **inside,
        "samplePk": 2,
        "timestampGMT": inside["timestampGMT"] - 86_400_000,
    }

    class Client:
        async def get_weight_measurements(self, start_date, end_date):
            assert start_date < end_date
            return {
                "dailyWeightSummaries": [
                    {"summaryDate": "2026-10-20", "allWeightMetrics": [outside]},
                    {"summaryDate": "2026-10-21", "allWeightMetrics": [inside]},
                ]
            }

    class Provider:
        client = Client()

    start = _utc("2026-10-21T00:00:00Z")
    metrics = asyncio.run(
        garmin.GarminBodyFeed(Provider()).list_records(start, start + timedelta(days=1))
    )

    assert [metric["samplePk"] for metric in metrics] == [inside["samplePk"]]


# ── Client helpers ──────────────────────────────────────────────────


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


def test_jwt_expiration_reads_the_exp_claim():
    assert jwt_expiration(_jwt({"exp": 1_761_100_000})) == 1_761_100_000.0


def test_jwt_expiration_rejects_a_non_jwt():
    with pytest.raises(GarminAuthError):
        jwt_expiration("not-a-jwt")


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


def test_extract_fit_returns_the_fit_member():
    archive = _zip({"readme.txt": b"x", "17123456789_ACTIVITY.fit": b"FITDATA"})
    assert extract_fit(archive) == b"FITDATA"


@pytest.mark.parametrize(
    "archive",
    [_zip({"activity.gpx": b"<gpx/>"}), b"not a zip"],
    ids=["no fit member", "not a zip"],
)
def test_extract_fit_rejects_archives_without_a_fit_file(archive):
    with pytest.raises(ValueError):
        extract_fit(archive)
