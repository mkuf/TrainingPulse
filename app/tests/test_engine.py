"""Tests for the generic sync engine's pure logic.

``choose_winners`` is the heart of the precedence/dedup design, so it is tested
directly rather than through the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sources.engine import (
    LIST_UPSERT_FIELDS,
    START_TOLERANCE_SECONDS,
    ActivityKey,
    choose_winners,
    date_windows,
    needs_full_refresh,
)

DEFAULT_PRECEDENCE = ["icu", "strava", "google_health"]
BASE = datetime(2026, 3, 14, 17, 0, tzinfo=timezone.utc)


def key(
    activity_id: str,
    source: str,
    *,
    offset_seconds: int = 0,
    elapsed: int = 3600,
) -> ActivityKey:
    return ActivityKey(
        id=activity_id,
        source=source,
        start=BASE + timedelta(seconds=offset_seconds),
        elapsed=elapsed,
    )


# ── Date windows ────────────────────────────────────────────────────


def test_date_windows_splits_inclusive_range():
    from datetime import date

    windows = date_windows(date(2026, 1, 1), date(2026, 1, 10), 4)

    assert windows == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 8)),
        (date(2026, 1, 9), date(2026, 1, 10)),
    ]


def test_date_windows_single_day():
    from datetime import date

    assert date_windows(date(2026, 1, 1), date(2026, 1, 1), 90) == [
        (date(2026, 1, 1), date(2026, 1, 1))
    ]


def test_date_windows_covers_every_day_without_gaps_or_overlap():
    from datetime import date

    oldest, newest = date(2025, 1, 1), date(2026, 6, 30)
    windows = date_windows(oldest, newest, 90)

    assert windows[0][0] == oldest
    assert windows[-1][1] == newest
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == previous_end + timedelta(days=1)


def test_date_windows_rejects_nothing_but_clamps_zero_width():
    from datetime import date

    windows = date_windows(date(2026, 1, 1), date(2026, 1, 3), 0)

    assert len(windows) == 3


# ── choose_winners ──────────────────────────────────────────────────


def test_single_activity_is_visible():
    rows = [key("icu_1", "icu")]

    assert choose_winners(rows, DEFAULT_PRECEDENCE) == {"icu_1": None}


def test_every_input_id_is_present_in_the_result():
    rows = [key("icu_1", "icu"), key("strava_1", "strava"), key("gh_1", "google_health")]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert set(winners) == {"icu_1", "strava_1", "gh_1"}


def test_identical_start_highest_precedence_wins():
    rows = [key("strava_1", "strava"), key("icu_1", "icu")]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": "icu_1"}


def test_three_way_overlap_collapses_to_one_winner():
    rows = [
        key("gh_1", "google_health"),
        key("strava_1", "strava", offset_seconds=60),
        key("icu_1", "icu", offset_seconds=120),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": "icu_1", "gh_1": "icu_1"}


def test_precedence_order_is_configurable():
    rows = [key("strava_1", "strava"), key("icu_1", "icu")]

    winners = choose_winners(rows, ["strava", "icu", "google_health"])

    assert winners == {"strava_1": None, "icu_1": "strava_1"}


def test_flipping_precedence_is_reversible():
    rows = [key("strava_1", "strava"), key("icu_1", "icu")]

    forward = choose_winners(rows, ["icu", "strava"])
    reversed_ = choose_winners(rows, ["strava", "icu"])
    back = choose_winners(rows, ["icu", "strava"])

    assert forward != reversed_
    assert forward == back


def test_start_offset_just_inside_tolerance_overlaps():
    rows = [
        key("icu_1", "icu"),
        key("strava_1", "strava", offset_seconds=START_TOLERANCE_SECONDS - 60),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners["strava_1"] == "icu_1"


def test_start_offset_just_outside_tolerance_does_not_overlap():
    rows = [
        key("icu_1", "icu", elapsed=7200),
        key("strava_1", "strava", offset_seconds=START_TOLERANCE_SECONDS + 60),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": None}


def test_close_starts_without_real_overlap_are_both_kept():
    """Within the start tolerance, but the time ranges do not intersect."""
    rows = [
        key("icu_1", "icu", elapsed=60),
        key("strava_1", "strava", offset_seconds=600, elapsed=60),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": None}


def test_nested_activity_is_superseded():
    rows = [
        key("icu_1", "icu", elapsed=3600),
        key("strava_1", "strava", offset_seconds=300, elapsed=600),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": "icu_1"}


def test_zero_duration_rows_at_the_same_instant_overlap():
    rows = [
        key("icu_1", "icu", elapsed=0),
        key("strava_1", "strava", elapsed=0),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": "icu_1"}


def test_zero_duration_rows_apart_do_not_overlap():
    rows = [
        key("icu_1", "icu", elapsed=0),
        key("strava_1", "strava", offset_seconds=300, elapsed=0),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": None}


def test_same_source_duplicates_prefer_the_longer_activity():
    rows = [
        key("icu_short", "icu", elapsed=600),
        key("icu_long", "icu", elapsed=3600),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_long": None, "icu_short": "icu_long"}


def test_unknown_source_ranks_last():
    rows = [key("garmin_1", "garmin"), key("gh_1", "google_health")]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"gh_1": None, "garmin_1": "gh_1"}


def test_disabled_source_still_participates():
    """Precedence is positional, independent of the enabled flag.

    Preserved Strava rows must keep outranking Google Health exactly as they did
    before Strava was re-enabled, otherwise upgrading silently rewrites history.
    """
    rows = [key("strava_legacy", "strava"), key("gh_1", "google_health")]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"strava_legacy": None, "gh_1": "strava_legacy"}


def test_superseded_by_always_points_at_a_visible_activity():
    """Chains must resolve to their root, never to another hidden row."""
    rows = [
        key("gh_1", "google_health"),
        key("strava_1", "strava", offset_seconds=30),
        key("icu_1", "icu", offset_seconds=60),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    visible = {activity_id for activity_id, w in winners.items() if w is None}
    for activity_id, winner in winners.items():
        if winner is not None:
            assert winner in visible, f"{activity_id} points at a hidden row"


def test_non_overlapping_days_are_independent():
    rows = [
        key("icu_1", "icu"),
        key("strava_1", "strava", offset_seconds=86400),
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert winners == {"icu_1": None, "strava_1": None}


def test_result_is_deterministic_regardless_of_input_order():
    rows = [
        key("gh_1", "google_health"),
        key("strava_1", "strava", offset_seconds=60),
        key("icu_1", "icu", offset_seconds=120),
    ]

    forward = choose_winners(rows, DEFAULT_PRECEDENCE)
    backward = choose_winners(list(reversed(rows)), DEFAULT_PRECEDENCE)

    assert forward == backward


def test_empty_input():
    assert choose_winners([], DEFAULT_PRECEDENCE) == {}


def test_large_history_stays_pairwise_not_transitive():
    """A long chain of 10-minute-apart short activities must not collapse."""
    rows = [
        key(f"icu_{index}", "icu", offset_seconds=index * 600, elapsed=60)
        for index in range(50)
    ]

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    assert all(winner is None for winner in winners.values())


def test_overlap_chain_does_not_collapse_non_overlapping_ends():
    """A bridges B, B bridges C, but A and C never overlap.

    ``gh_1`` (0s-1000s) only overlaps ``strava_1`` (100s-1100s), which only
    overlaps ``icu_1`` (950s-1950s). ``gh_1`` and ``icu_1`` are 950s apart,
    just outside ``START_TOLERANCE_SECONDS`` (900s), so they must never be
    linked even though the direct-link chain would otherwise bridge them via
    ``strava_1``.
    """
    rows = [
        key("gh_1", "google_health", offset_seconds=0, elapsed=1000),
        key("strava_1", "strava", offset_seconds=100, elapsed=1000),
        key("icu_1", "icu", offset_seconds=950, elapsed=1000),
    ]
    assert (950 - 0) > START_TOLERANCE_SECONDS
    assert (950 - 100) <= START_TOLERANCE_SECONDS

    winners = choose_winners(rows, DEFAULT_PRECEDENCE)

    # icu_1 outranks everything and is visible; strava_1 genuinely overlaps
    # icu_1, so it is correctly superseded by it.
    assert winners["icu_1"] is None
    assert winners["strava_1"] == "icu_1"
    # gh_1 never overlaps icu_1, and its only real overlap partner
    # (strava_1) is not itself visible, so gh_1 must stay visible rather
    # than being collapsed into an activity it never overlapped.
    assert winners["gh_1"] is None

    visible = {activity_id for activity_id, w in winners.items() if w is None}
    for activity_id, winner in winners.items():
        if winner is not None:
            assert winner in visible, f"{activity_id} points at a hidden row"


# ── Reconcile diffing ───────────────────────────────────────────────


class _Existing:
    """Stand-in for a stored Activity row."""

    def __init__(self, **values):
        defaults = {
            "name": "Ride",
            "sport_type": "Ride",
            "start_date": BASE,
            "elapsed_time": 3600,
            "moving_time": 3500,
            "distance": 40000.0,
            "average_heartrate": 140.0,
            "max_heartrate": 170.0,
            "has_heartrate": True,
            "average_watts": 200.0,
            "max_watts": 500.0,
            "gear_id": "b1",
            "kilojoules": 700.0,
            "device_watts": True,
            "training_load": 90.0,
            "provider_updated_at": BASE,
            "detail_synced": True,
        }
        defaults.update(values)
        for name, value in defaults.items():
            setattr(self, name, value)

    def as_row(self, **overrides) -> dict:
        row = {
            name: getattr(self, name)
            for name in LIST_UPSERT_FIELDS
            if hasattr(self, name)
        }
        row.update(overrides)
        return row


def test_unchanged_activity_needs_no_refresh():
    existing = _Existing()

    assert needs_full_refresh(existing, existing.as_row()) is False


def test_newer_provider_timestamp_triggers_refresh():
    existing = _Existing()
    row = existing.as_row(provider_updated_at=BASE + timedelta(minutes=1))

    assert needs_full_refresh(existing, row) is True


def test_older_provider_timestamp_does_not_trigger_refresh():
    existing = _Existing()
    row = existing.as_row(provider_updated_at=BASE - timedelta(minutes=1))

    assert needs_full_refresh(existing, row) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Renamed"),
        ("sport_type", "VirtualRide"),
        ("elapsed_time", 4000),
        ("distance", 41000.0),
        ("average_heartrate", 145.0),
        ("gear_id", "b2"),
        ("device_watts", False),
        ("training_load", 91.0),
    ],
)
def test_changed_list_field_triggers_refresh(field, value):
    existing = _Existing()
    row = existing.as_row(**{field: value})

    assert needs_full_refresh(existing, row) is True


def test_float_fields_compare_numerically_not_by_type():
    existing = _Existing(distance=40000.0)
    row = existing.as_row(distance=40000)

    assert needs_full_refresh(existing, row) is False


def test_newly_present_float_triggers_refresh():
    existing = _Existing(average_watts=None)
    row = existing.as_row(average_watts=200.0)

    assert needs_full_refresh(existing, row) is True


def test_list_upsert_fields_do_not_include_derived_or_detail_columns():
    """Re-listing must never wipe enriched data."""
    forbidden = {
        "trimp",
        "hr_zone_seconds",
        "power_zone_seconds",
        "best_20min_power",
        "power_curve",
        "synced_streams",
        "superseded_by",
        "description",
        "private_note",
        "calories",
        "device_name",
        "gear_name",
        "id",
    }

    assert forbidden.isdisjoint(LIST_UPSERT_FIELDS)


def test_list_upsert_fields_include_suffer_score():
    assert "suffer_score" in LIST_UPSERT_FIELDS


# ── Stream application ──────────────────────────────────────────────


class _FakeSession:
    """Records ``update(Activity)`` values; mimics no real database."""

    def __init__(self, weight_kg: float | None = 80.0):
        self.updates: list[dict] = []
        self.rolled_back = False
        self.weight_kg = weight_kg

    async def scalar(self, stmt):
        return self.weight_kg

    async def execute(self, stmt):
        if getattr(stmt, "is_update", False):
            self.updates.append(
                {col.key: val.value for col, val in stmt._values.items()}
            )

    async def commit(self):
        pass

    async def rollback(self):
        self.rolled_back = True


class _FakeSource:
    name = "hammerhead"

    def __init__(self, streams):
        self._streams = streams

    async def fetch_streams(self, activity):
        if isinstance(self._streams, Exception):
            raise self._streams
        return self._streams


def _stream_activity(**overrides):
    from types import SimpleNamespace

    fields = dict(
        id="hammerhead_1",
        has_heartrate=True,
        device_watts=True,
        sport_type="Ride",
        moving_time=1500,
        start_date=BASE,
        distance=12000.0,
        total_elevation_gain=50.0,
        average_watts=200.0,
        average_heartrate=150.0,
        max_heartrate=175.0,
        average_speed=8.0,
        calories=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _stream_ctx():
    from types import SimpleNamespace

    from calories import AthleteEnergyInputs
    from metrics import get_power_zone_boundaries

    return SimpleNamespace(
        hr_zone_boundaries=[(0, 120), (120, 140), (140, 160), (160, 175), (175, 250)],
        power_zone_boundaries=get_power_zone_boundaries(200),
        energy_athlete=AthleteEnergyInputs(
            ftp=200, max_hr=190, rest_hr=60, weight_kg=None
        ),
    )


@pytest.mark.asyncio
async def test_apply_streams_tolerates_none_gaps_in_fit_streams():
    from sources.engine import _apply_streams

    n = 1500
    power = [200] * n
    hr = [150] * n
    power[10] = None
    hr[20] = None
    streams = {"time": list(range(n)), "watts": power, "heartrate": hr}
    session = _FakeSession()

    await _apply_streams(session, _FakeSource(streams), _stream_activity(), _stream_ctx())

    assert not session.rolled_back
    (values,) = session.updates
    assert values["synced_streams"] is True
    assert values["best_20min_power"] == pytest.approx(200.0)
    assert values["power_curve"]["1200"] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_apply_streams_fallback_does_not_reload_activity_after_rollback():
    from sources.engine import _apply_streams

    session = _FakeSession()

    await _apply_streams(
        session, _FakeSource(RuntimeError("boom")), _stream_activity(), _stream_ctx()
    )

    assert session.rolled_back
    (values,) = session.updates
    assert values["synced_streams"] is True
    assert values["trimp"] > 0



# ── Computed energy in the stream phase ─────────────────────────────


@pytest.mark.asyncio
async def test_apply_streams_computes_kilojoules_from_the_power_stream():
    from calories import KCAL_PER_KJ
    from sources.engine import _apply_streams

    n = 1501
    streams = {"time": list(range(n)), "watts": [200] * n, "heartrate": [150] * n}
    session = _FakeSession()
    # The summary average disagrees with the stream; the stream is the work.
    activity = _stream_activity(average_watts=150.0, calories=999.0)

    await _apply_streams(session, _FakeSource(streams), activity, _stream_ctx())

    (values,) = session.updates
    assert values["computed_kilojoules"] == pytest.approx(300.0)
    assert values["computed_calories"] == pytest.approx(300.0 * KCAL_PER_KJ)
    assert values["computed_calories_method"] == "power"


@pytest.mark.asyncio
async def test_apply_streams_computes_hr_power_for_gym_sessions():
    from sources.engine import _apply_streams

    streams = {"time": list(range(600)), "heartrate": [140] * 600}
    session = _FakeSession()
    activity = _stream_activity(
        sport_type="Crossfit", device_watts=False, average_watts=None, calories=900.0
    )

    await _apply_streams(session, _FakeSource(streams), activity, _stream_ctx())

    (values,) = session.updates
    assert values["computed_calories_method"] == "hr_power"
    assert values["computed_kilojoules"] is None


@pytest.mark.asyncio
async def test_apply_streams_without_hr_or_power_uses_acsm_and_weigh_in():
    from calories import ACSM_WALK, KCAL_PER_LITRE_O2, RESTING_VO2
    from sources.engine import _apply_streams

    session = _FakeSession(weight_kg=90.0)
    activity = _stream_activity(
        sport_type="Walk",
        has_heartrate=False,
        device_watts=False,
        average_watts=None,
        average_heartrate=None,
        distance=4000.0,
        total_elevation_gain=0.0,
        moving_time=3000,
    )

    await _apply_streams(session, _FakeSource({}), activity, _stream_ctx())

    (values,) = session.updates
    oxygen = RESTING_VO2 * 50 + ACSM_WALK[0] * 4000
    assert values["computed_calories_method"] == "acsm"
    assert values["computed_calories"] == pytest.approx(
        oxygen * 90 / 1000 * KCAL_PER_LITRE_O2
    )


@pytest.mark.asyncio
async def test_apply_streams_failure_still_computes_from_summary():
    from calories import KCAL_PER_KJ
    from sources.engine import _apply_streams

    session = _FakeSession()

    await _apply_streams(
        session, _FakeSource(RuntimeError("boom")), _stream_activity(), _stream_ctx()
    )

    (values,) = session.updates
    assert values["computed_calories_method"] == "power"
    assert values["computed_kilojoules"] == pytest.approx(200 * 1500 / 1000)
    assert values["computed_calories"] == pytest.approx(300.0 * KCAL_PER_KJ)


@pytest.mark.asyncio
async def test_apply_streams_derives_missing_heart_rate_summary():
    from sources.engine import _apply_streams

    n = 1200
    streams = {"time": list(range(n)), "heartrate": [0] * 10 + [140] * (n - 20) + [180] * 10}
    session = _FakeSession()
    activity = _stream_activity(
        device_watts=False,
        has_heartrate=False,
        average_heartrate=None,
        max_heartrate=None,
        average_watts=None,
        sport_type="Crossfit",
    )

    await _apply_streams(session, _FakeSource(streams), activity, _stream_ctx())

    assert not session.rolled_back
    (values,) = session.updates
    assert values["has_heartrate"] is True
    assert values["max_heartrate"] == pytest.approx(180.0)
    assert values["average_heartrate"] == pytest.approx((140 * 1180 + 180 * 10) / 1190)
    # With an average heart rate the HR-to-power rule applies.
    assert values["computed_calories_method"] == "hr_power"
    assert values["hr_zone_seconds"] is not None


@pytest.mark.asyncio
async def test_apply_streams_keeps_source_heart_rate_summary():
    from sources.engine import _apply_streams

    n = 1500
    streams = {"time": list(range(n)), "heartrate": [100] * n}
    session = _FakeSession()

    await _apply_streams(session, _FakeSource(streams), _stream_activity(), _stream_ctx())

    (values,) = session.updates
    assert "average_heartrate" not in values
    assert "max_heartrate" not in values


@pytest.mark.asyncio
async def test_apply_streams_derives_missing_distance_and_speed():
    from sources.engine import _apply_streams

    streams = {"time": [0, 600, 1200], "distance": [0.0, 2500.0, 6000.0]}
    session = _FakeSession()
    activity = _stream_activity(
        distance=0.0, average_speed=None, moving_time=1200, device_watts=False
    )

    await _apply_streams(session, _FakeSource(streams), activity, _stream_ctx())

    (values,) = session.updates
    assert values["distance"] == pytest.approx(6000.0)
    assert values["average_speed"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_apply_streams_keeps_source_distance():
    from sources.engine import _apply_streams

    streams = {"time": [0, 600], "distance": [0.0, 9999.0]}
    session = _FakeSession()

    await _apply_streams(session, _FakeSource(streams), _stream_activity(), _stream_ctx())

    (values,) = session.updates
    assert "distance" not in values
    assert "average_speed" not in values
