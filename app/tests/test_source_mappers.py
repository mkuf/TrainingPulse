"""Tests for the per-source mappers and stream normalizers.

These run against recorded payloads in ``fixtures/payloads.py``. The mappers
are pure and synchronous precisely so this is possible without HTTP or a
database.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fixtures import payloads
from sources import google_health as gh
from sources import hammerhead
from sources import intervals as icu
from sources import strava
from sources.base import ActivitySource, external_id, prefixed_id

ATHLETE_ID = 7


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ── Id prefixing ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prefix", "raw", "expected"),
    [
        ("strava_", 1122334455, "strava_1122334455"),
        ("strava_", "1122334455", "strava_1122334455"),
        # Already prefixed: preserved rows from the intervals.icu migration.
        ("strava_", "strava_1122334455", "strava_1122334455"),
        ("strava_", "strava:1122334455", "strava_1122334455"),
        ("icu_", "i999", "icu_i999"),
        ("icu_", "icu_i999", "icu_i999"),
        ("icu_", "icu:i999", "icu_i999"),
        # Google Health resource names collapse to the last path segment.
        ("ghealth_", "users/me/dataTypes/exercise/dataPoints/abc", "ghealth_abc"),
        ("ghealth_", "ghealth_abc", "ghealth_abc"),
        # Hammerhead's own ids already contain dots; they pass through as-is.
        ("hammerhead_", "1000.activity.abcd", "hammerhead_1000.activity.abcd"),
        ("hammerhead_", "hammerhead_1000.activity.abcd", "hammerhead_1000.activity.abcd"),
    ],
)
def test_prefixed_id(prefix, raw, expected):
    assert prefixed_id(prefix, raw) == expected


def test_prefixing_is_idempotent():
    once = prefixed_id("strava_", 12345)
    assert prefixed_id("strava_", once) == once
    assert prefixed_id("strava_", prefixed_id("strava_", once)) == once


@pytest.mark.parametrize(
    ("prefix", "activity_id", "expected"),
    [
        ("strava_", "strava_1122334455", "1122334455"),
        ("strava_", "1122334455", "1122334455"),
        ("icu_", "icu_i999", "i999"),
        ("ghealth_", "ghealth_abc", "abc"),
        (
            "hammerhead_",
            "hammerhead_1000.activity.abcd",
            "1000.activity.abcd",
        ),
    ],
)
def test_external_id_round_trip(prefix, activity_id, expected):
    assert external_id(prefix, activity_id) == expected


# ── Protocol conformance ────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        icu.IntervalsSource(api_key="k"),
        strava.StravaSource(
            session_factory=None, client_id="c", client_secret="s"
        ),
        gh.GoogleHealthSource(
            session_factory=None, client_id="c", client_secret="s"
        ),
        hammerhead.HammerheadSource(
            session_factory=None, client_id="c", client_secret="s"
        ),
    ],
    ids=["icu", "strava", "google_health", "hammerhead"],
)
def test_sources_satisfy_the_protocol(source):
    assert isinstance(source, ActivitySource)
    for attribute in (
        "name",
        "id_prefix",
        "supports_detail",
        "supports_streams",
        "supports_reconcile",
        "auth",
        "kind",
        "connect_path",
        "disconnect_path",
    ):
        assert hasattr(source, attribute), attribute


def test_source_names_and_prefixes_are_distinct():
    names = {icu.SOURCE_NAME, strava.SOURCE_NAME, gh.SOURCE_NAME, hammerhead.SOURCE_NAME}
    prefixes = {icu.ID_PREFIX, strava.ID_PREFIX, gh.ID_PREFIX, hammerhead.ID_PREFIX}
    assert len(names) == 4
    assert len(prefixes) == 4


def test_mappers_agree_on_the_column_set():
    """Every mapper must produce exactly the same keys.

    A source that forgets a column would otherwise insert NULL silently, and a
    source that invents one would break the bulk upsert.
    """
    icu_row = icu.map_row(payloads.ICU_LIST_ACTIVITY, ATHLETE_ID)
    strava_row = strava.map_row(payloads.STRAVA_LIST_ACTIVITY, ATHLETE_ID)
    gh_row = gh.map_row(payloads.GOOGLE_HEALTH_EXERCISE, ATHLETE_ID)
    hammerhead_row = hammerhead.map_row(
        payloads.HAMMERHEAD_LIST_ACTIVITY, ATHLETE_ID
    )

    assert set(icu_row) == set(strava_row) == set(gh_row) == set(hammerhead_row)


def test_mapped_columns_all_exist_on_the_model():
    from models import Activity

    columns = set(Activity.__table__.columns.keys())
    row = strava.map_row(payloads.STRAVA_DETAIL_ACTIVITY, ATHLETE_ID)
    assert set(row) <= columns


def test_no_mapper_leaks_private_keys():
    """Underscore-prefixed smuggling would break the bulk insert."""
    for row in (
        icu.map_row(payloads.ICU_LIST_ACTIVITY, ATHLETE_ID),
        strava.map_row(payloads.STRAVA_LIST_ACTIVITY, ATHLETE_ID),
        gh.map_row(payloads.GOOGLE_HEALTH_EXERCISE, ATHLETE_ID),
        hammerhead.map_row(payloads.HAMMERHEAD_DETAIL_ACTIVITY, ATHLETE_ID),
    ):
        assert [key for key in row if key.startswith("_")] == []


# ── intervals.icu ───────────────────────────────────────────────────


def test_icu_map_row():
    row = icu.map_row(payloads.ICU_LIST_ACTIVITY, ATHLETE_ID)

    assert row["id"] == "icu_i87654321"
    assert row["source"] == "icu"
    assert row["athlete_id"] == ATHLETE_ID
    assert row["name"] == "Zwift Race"
    assert row["sport_type"] == "Ride"
    assert row["start_date"] == _utc("2026-03-14T17:05:00Z")
    assert row["elapsed_time"] == 3720
    assert row["moving_time"] == 3600
    assert row["distance"] == pytest.approx(41234.5)
    assert row["has_heartrate"] is True
    assert row["average_heartrate"] == pytest.approx(148.2)
    assert row["average_watts"] == pytest.approx(212.4)
    assert row["weighted_average_watts"] == pytest.approx(228.9)
    assert row["device_watts"] is True
    assert row["training_load"] == pytest.approx(96.0)
    assert row["gear_id"] == "b1234567"
    assert row["device_name"] == "Wahoo KICKR"
    assert row["provider_updated_at"] == _utc("2026-03-14T19:00:00Z")
    # suffer_score is a Strava-only concept.
    assert row["suffer_score"] is None


def test_icu_map_row_reads_icu_prefixed_aliases():
    row = icu.map_row(payloads.ICU_LIST_ACTIVITY_ALIASES, ATHLETE_ID)

    assert row["id"] == "icu_555"
    assert row["sport_type"] == "VirtualRide"
    # start_date (true UTC) must win over start_date_local (local wall-clock
    # time with no offset), which would otherwise be misparsed as UTC.
    assert row["start_date"] == _utc("2026-03-15T06:00:00Z")
    assert row["elapsed_time"] == 1800
    assert row["moving_time"] == 1700
    assert row["average_heartrate"] == pytest.approx(130.0)
    assert row["max_watts"] == pytest.approx(400.0)
    assert row["weighted_average_watts"] == pytest.approx(190.0)
    assert row["gear_id"] == "b999"
    assert row["device_name"] == "Garmin Edge"
    assert row["training_load"] == pytest.approx(44.0)
    # icu_joules is work in joules: kilojoules, never calories.
    assert row["kilojoules"] == pytest.approx(306.0)
    assert row["calories"] is None


def test_icu_map_row_skips_activity_without_start():
    assert icu.map_row(payloads.ICU_ACTIVITY_NO_START, ATHLETE_ID) is None


def test_icu_map_row_skips_payload_without_id():
    payload = dict(payloads.ICU_LIST_ACTIVITY)
    payload.pop("id")
    assert icu.map_row(payload, ATHLETE_ID) is None


def test_icu_map_row_uses_nested_gear_object():
    payload = dict(payloads.ICU_LIST_ACTIVITY)
    payload.pop("gear_id")
    payload["gear"] = {"id": "b555", "name": "Canyon Ultimate"}

    row = icu.map_row(payload, ATHLETE_ID)

    assert row["gear_id"] == "b555"
    assert row["gear_name"] == "Canyon Ultimate"


def test_icu_normalize_streams_flat_and_aliased():
    streams = icu.normalize_streams(payloads.ICU_STREAMS_FLAT)

    assert streams["time"] == [0, 1, 2]
    assert streams["heartrate"] == [120, 122, 125]
    assert streams["watts"] == [200, 210, 205]
    assert streams["velocity_smooth"] == [8.1, 8.2, 8.3]
    assert streams["latlng"][0] == [1.0, 2.0]
    assert "heart_rate" not in streams
    assert "power" not in streams


def test_icu_normalize_streams_wrapped_shapes_and_skips_private_keys():
    streams = icu.normalize_streams(payloads.ICU_STREAMS_WRAPPED)

    assert streams == {"time": [0, 1], "heartrate": [100, 101]}


def test_icu_normalize_streams_of_empty_payload():
    assert icu.normalize_streams({}) == {}


# ── Strava ──────────────────────────────────────────────────────────


def test_strava_map_row():
    row = strava.map_row(payloads.STRAVA_LIST_ACTIVITY, ATHLETE_ID)

    assert row["id"] == "strava_1122334455"
    assert row["source"] == "strava"
    assert row["name"] == "Morning Run"
    assert row["sport_type"] == "Run"
    assert row["start_date"] == _utc("2026-03-14T06:30:00Z")
    assert row["elapsed_time"] == 2700
    assert row["moving_time"] == 2650
    assert row["has_heartrate"] is True
    assert row["average_heartrate"] == pytest.approx(152.0)
    assert row["device_watts"] is False
    assert row["gear_id"] == "g7654321"
    assert row["provider_updated_at"] == _utc("2026-03-14T08:00:00Z")


def test_strava_map_row_never_uses_the_provider_athlete_id():
    """The local athlete_id is the DB key and must not be overwritten."""
    row = strava.map_row(payloads.STRAVA_LIST_ACTIVITY, ATHLETE_ID)

    assert payloads.STRAVA_LIST_ACTIVITY["athlete"]["id"] != ATHLETE_ID
    assert row["athlete_id"] == ATHLETE_ID


def test_strava_map_row_merges_detail_fields_and_suffer_score():
    row = strava.map_row(payloads.STRAVA_DETAIL_ACTIVITY, ATHLETE_ID)

    assert row["description"] == "Easy shakeout"
    assert row["private_note"] == "felt flat"
    assert row["calories"] == pytest.approx(612.0)
    assert row["device_name"] == "Garmin Forerunner 965"
    assert row["suffer_score"] == pytest.approx(74.0)
    # Resolved by fetch_detail so map_row can stay pure.
    assert row["gear_name"] == "Nike Pegasus 41"


def test_strava_map_row_maps_preserved_legacy_id_onto_itself():
    """Rows kept from the Strava era already carry the prefix."""
    payload = dict(payloads.STRAVA_LIST_ACTIVITY, id="strava_1122334455")

    row = strava.map_row(payload, ATHLETE_ID)

    assert row["id"] == "strava_1122334455"


@pytest.mark.parametrize(
    "payload",
    [payloads.STRAVA_ACTIVITY_NO_ID, payloads.STRAVA_ACTIVITY_NO_START],
    ids=["no id", "no start"],
)
def test_strava_map_row_skips_unusable_payloads(payload):
    assert strava.map_row(payload, ATHLETE_ID) is None


def test_strava_normalize_streams_unwraps_data_arrays():
    streams = strava.normalize_streams(payloads.STRAVA_STREAMS)

    assert streams["time"] == [0, 1, 2]
    assert streams["heartrate"] == [140, 141, 142]
    assert streams["watts"] == [0, 0, 0]
    assert streams["latlng"][0] == [1.0, 2.0]
    # series_type metadata must not leak through as a stream.
    assert all(isinstance(value, list) for value in streams.values())


def test_strava_normalize_streams_of_empty_payload():
    assert strava.normalize_streams({}) == {}


# ── Google Health ───────────────────────────────────────────────────


def test_google_health_map_row():
    row = gh.map_row(payloads.GOOGLE_HEALTH_EXERCISE, ATHLETE_ID)

    assert row["id"] == "ghealth_abc123"
    assert row["source"] == "google_health"
    assert row["name"] == "Evening Walk"
    assert row["sport_type"] == "Walking"
    assert row["start_date"] == _utc("2026-03-14T18:00:00Z")
    # activeDuration is 3600s but the interval spans 3900s.
    assert row["moving_time"] == 3600
    assert row["elapsed_time"] == 3900
    assert row["distance"] == pytest.approx(4200.0)
    assert row["average_speed"] == pytest.approx(1.1667)
    assert row["total_elevation_gain"] == pytest.approx(35.0)
    assert row["average_heartrate"] == pytest.approx(96.0)
    # MetricsSummary has no max; the stream phase derives it.
    assert row["max_heartrate"] is None
    assert row["has_heartrate"] is True
    assert row["calories"] == pytest.approx(210.0)
    assert row["description"] == "with the dog"
    assert row["device_name"] == "Google Health"
    assert row["provider_updated_at"] == _utc("2026-03-14T20:00:00Z")
    # Google Health never reports power.
    assert row["average_watts"] is None
    assert row["device_watts"] is False


def test_google_health_map_row_with_empty_health_connect_summary():
    row = gh.map_row(payloads.GOOGLE_HEALTH_EXERCISE_HEALTH_CONNECT, ATHLETE_ID)

    assert row["id"] == "ghealth_hc1"
    assert row["sport_type"] == "Strengthtraining"
    assert row["elapsed_time"] == 1800
    assert row["distance"] == 0.0
    assert row["average_heartrate"] is None
    assert row["has_heartrate"] is False
    assert row["calories"] is None


def test_google_health_stores_bike_activities_for_precedence_to_judge():
    """Bike sessions used to be dropped at ingest.

    They are stored now: precedence decides whether they are visible, so the
    decision stays reversible.
    """
    row = gh.map_row(payloads.GOOGLE_HEALTH_BIKE_EXERCISE, ATHLETE_ID)

    assert row is not None
    assert row["sport_type"] == "Biking"


def test_google_health_map_row_skips_exercise_without_start():
    assert gh.map_row(payloads.GOOGLE_HEALTH_EXERCISE_NO_START, ATHLETE_ID) is None


def test_google_health_hr_points_to_streams_sorts_and_filters():
    start = _utc("2026-03-14T18:00:00Z")

    streams = gh.hr_points_to_streams(payloads.GOOGLE_HEALTH_HR_POINTS, start)

    assert streams["time"] == [0, 30, 60, 90]
    assert streams["heartrate"] == [90.0, 95.0, 101.0, 104.0]


def test_google_health_hr_points_to_streams_when_nothing_usable():
    start = _utc("2026-03-14T18:00:00Z")

    assert gh.hr_points_to_streams([], start) == {}
    assert gh.hr_points_to_streams([{"nothing": 1}], start) == {}


def test_google_health_tcx_to_streams_keeps_positioned_trackpoints():
    start = _utc("2026-03-14T18:00:00Z")

    streams = gh.tcx_to_streams(payloads.GOOGLE_HEALTH_TCX, start)

    assert streams["time"] == [0, 10]
    assert streams["latlng"] == [[48.0, 11.4], [48.1, 11.5]]
    assert streams["altitude"] == [520.0, 520.5]
    assert streams["distance"] == [0.0, 40.0]
    assert streams["velocity_smooth"] == [4.0, 4.0]
    # No trackpoint has a heart rate, so the stream is left out.
    assert "heartrate" not in streams


def test_google_health_tcx_to_streams_ignores_garbage():
    assert gh.tcx_to_streams("not xml", _utc("2026-03-14T18:00:00Z")) == {}


@pytest.mark.asyncio
async def test_google_health_streams_merge_route_and_heart_rate():
    from types import SimpleNamespace

    class Client:
        async def export_exercise_tcx(self, data_point_id):
            assert data_point_id == "abc123"
            return payloads.GOOGLE_HEALTH_TCX

        async def list_heart_rate(self, start, end):
            return payloads.GOOGLE_HEALTH_HR_POINTS

    source = gh.GoogleHealthSource.__new__(gh.GoogleHealthSource)
    source._client = Client()
    activity = SimpleNamespace(
        id="ghealth_abc123",
        start_date=_utc("2026-03-14T18:00:00Z"),
        elapsed_time=120,
    )

    streams = await source.fetch_streams(activity)

    assert streams["time"] == [0, 10]
    assert streams["latlng"] == [[48.0, 11.4], [48.1, 11.5]]
    # Latest heart-rate sample at or before each route point.
    assert streams["heartrate"] == [90.0, 90.0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WALKING", "Walking"),
        # Multi-word enums concatenate without a separator. This is the
        # pre-existing behaviour and is deliberately preserved: sport_type is
        # stored on every row and used for Grafana grouping, so "fixing" the
        # spelling here would silently rewrite existing Google Health history.
        ("STRENGTH_TRAINING", "Strengthtraining"),
        ("HIGH_INTENSITY_INTERVAL_TRAINING", "Highintensityintervaltraining"),
        ("", "Workout"),
        (None, "Workout"),
    ],
)
def test_google_health_sport_type(raw, expected):
    assert gh.sport_type(raw) == expected


# ── Hammerhead ──────────────────────────────────────────────────────


def test_hammerhead_map_row_list_phase_only():
    """No detail/FIT merge yet: only createdAt/duration/distance are usable."""
    row = hammerhead.map_row(payloads.HAMMERHEAD_LIST_ACTIVITY, ATHLETE_ID)

    assert row["id"] == "hammerhead_1000.activity.abcd"
    assert row["source"] == "hammerhead"
    assert row["athlete_id"] == ATHLETE_ID
    assert row["name"] == "My Epic Ride"
    # activityType is a detail-only field.
    assert row["sport_type"] == ""
    # createdAt is the end; duration is milliseconds.
    assert row["start_date"] == _utc("2026-03-14T17:06:00Z")
    assert row["elapsed_time"] == 3720
    assert row["moving_time"] == 3720
    assert row["distance"] == pytest.approx(41234.5)
    assert row["average_heartrate"] is None
    assert row["has_heartrate"] is False
    assert row["device_watts"] is False
    assert row["kilojoules"] is None
    assert row["gear_id"] is None
    assert row["provider_updated_at"] is None


def test_hammerhead_list_phase_matches_a_real_summary():
    """Values from a real Karoo ride (2026-10-24, started 12:12:23 UTC).

    ``duration`` is the moving time in ms, so the derived start lands within
    the pauses of the true start, well inside the dedup start tolerance.
    """
    from sources.engine import START_TOLERANCE_SECONDS

    row = hammerhead.map_row(
        {
            "id": "78106.activity.ff5cea5de16d",
            "createdAt": "2026-10-24T13:16:40.966Z",
            "duration": 3615000,
            "distance": 30394.09,
        },
        ATHLETE_ID,
    )

    assert row["elapsed_time"] == 3615
    assert row["moving_time"] == 3615
    true_start = _utc("2026-10-24T12:12:23Z")
    assert 0 <= (row["start_date"] - true_start).total_seconds() < START_TOLERANCE_SECONDS


def test_hammerhead_map_row_merges_fit_summary():
    """Detail merge: activityType and the analyzed FIT summary both apply."""
    row = hammerhead.map_row(payloads.HAMMERHEAD_DETAIL_ACTIVITY, ATHLETE_ID)

    assert row["sport_type"] == "MountainBikeRide"
    assert row["description"] == "Muddy but fun"
    # The FIT-derived start_date/elapsed/moving/distance win over the
    # provisional list-phase values.
    assert row["start_date"] == _utc("2026-03-14T17:06:10Z")
    assert row["elapsed_time"] == 3700
    assert row["moving_time"] == 3600
    assert row["distance"] == pytest.approx(41500.0)
    assert row["average_heartrate"] == pytest.approx(148.2)
    assert row["max_heartrate"] == pytest.approx(181.0)
    assert row["has_heartrate"] is True
    assert row["average_watts"] == pytest.approx(212.4)
    assert row["device_watts"] is True
    # Derived like Strava: average power x moving time.
    assert row["kilojoules"] == pytest.approx(212.4 * 3600 / 1000)
    assert row["weighted_average_watts"] == pytest.approx(228.9)
    assert row["total_elevation_gain"] == pytest.approx(312.0)
    assert row["calories"] == pytest.approx(820.0)
    assert row["device_name"] == "Hammerhead Karoo 3"
    assert row["provider_updated_at"] == _utc("2026-03-14T19:00:00Z")
    # Hammerhead/FIT don't track gear or a proprietary training-load score.
    assert row["gear_id"] is None
    assert row["gear_name"] is None
    assert row["suffer_score"] is None
    assert row["training_load"] is None


@pytest.mark.parametrize(
    "payload",
    [payloads.HAMMERHEAD_ACTIVITY_NO_ID, payloads.HAMMERHEAD_ACTIVITY_NO_START],
    ids=["no id", "no start"],
)
def test_hammerhead_map_row_skips_unusable_payloads(payload):
    assert hammerhead.map_row(payload, ATHLETE_ID) is None


def test_hammerhead_map_row_preserves_a_genuine_zero_elapsed_or_moving_time():
    """A real FIT-derived 0 must not be treated as "missing" and overwritten.

    Regression test: ``as_int(...) or fallback`` used to be indistinguishable
    from "no value provided" for a genuine zero.
    """
    payload = {
        **payloads.HAMMERHEAD_LIST_ACTIVITY,
        "_fit_summary": {
            **payloads.HAMMERHEAD_DETAIL_ACTIVITY["_fit_summary"],
            "elapsed_time": 0,
            "moving_time": 0,
        },
    }

    row = hammerhead.map_row(payload, ATHLETE_ID)

    assert row["elapsed_time"] == 0
    assert row["moving_time"] == 0


def test_hammerhead_analysis_cache_round_trips_and_is_bounded():
    """The fetch_detail -> fetch_streams handoff cache works and stays small.

    Regression test for the FIT bytes being fully re-parsed twice per
    activity: fetch_detail stashes the already-computed analysis so
    fetch_streams can reuse it instead of re-parsing.
    """
    source = hammerhead.HammerheadSource(
        session_factory=None, client_id="c", client_secret="s"
    )
    analysis = object()

    source._cache_analysis("hammerhead_1", analysis)
    assert source._take_cached_analysis("hammerhead_1") is analysis
    # Popped, not just peeked: a second take must miss.
    assert source._take_cached_analysis("hammerhead_1") is None

    for i in range(hammerhead._ANALYSIS_CACHE_MAX + 5):
        source._cache_analysis(f"hammerhead_{i}", object())
    assert len(source._analysis_cache) <= hammerhead._ANALYSIS_CACHE_MAX
