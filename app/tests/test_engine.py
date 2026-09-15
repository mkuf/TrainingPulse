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
