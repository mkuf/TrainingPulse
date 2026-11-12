"""Tests for the pure helpers behind the status page's Today summary."""

from __future__ import annotations

from datetime import date

import pytest
from today import (
    FORM_BAR_MAX,
    FORM_BAR_MIN,
    WEEK_BAR_MAX,
    duration,
    form_bands,
    form_marker,
    nutrition_window,
    recovery_bands,
    sleep_stages,
    week_bands,
    week_marker,
)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        # Monday: no day of this week is finished, so last week.
        (date(2026, 11, 2), (date(2026, 10, 26), date(2026, 11, 1), True)),
        # Tuesday: Monday only.
        (date(2026, 11, 3), (date(2026, 11, 2), date(2026, 11, 2), False)),
        # Sunday: Monday to Saturday; Sunday is still being logged.
        (date(2026, 11, 8), (date(2026, 11, 2), date(2026, 11, 7), False)),
    ],
)
def test_nutrition_window_covers_finished_days_of_the_week(today, expected):
    assert nutrition_window(today) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, None), (0, "0:00"), (59, "0:01"), (27_720, "7:42"), (36_000, "10:00")],
)
def test_duration_is_hours_and_minutes(seconds, expected):
    assert duration(seconds) == expected


def test_sleep_stages_are_in_display_order():
    assert sleep_stages(4_320, 6_060, 16_140, 1_320) == (
        ("deep", 4_320),
        ("rem", 6_060),
        ("light", 16_140),
        ("awake", 1_320),
    )


@pytest.mark.parametrize(
    "stages", [(None, 6_060, 16_140, 1_320), (4_320, None, 16_140, 1_320), (4_320, 6_060, None, None)]
)
def test_sleep_stages_need_all_three_sleep_stages(stages):
    assert sleep_stages(*stages) == ()


def test_sleep_stages_leave_out_what_has_no_time():
    assert sleep_stages(4_320, 0, 16_140, None) == (("deep", 4_320), ("light", 16_140))


def test_recovery_bands_cover_the_whole_range_lowest_first():
    bands = recovery_bands()

    assert [status for status, _, _ in bands] == [
        "well_below_normal",
        "below_normal",
        "normal",
        "above_normal",
    ]
    assert [label for _, label, _ in bands] == [
        "Well below normal: 0–39",
        "Below normal: 40–59",
        "Normal: 60–84",
        "Above normal: 85–100",
    ]
    assert sum(width for _, _, width in bands) == 100


def test_form_bands_cover_the_bar_lowest_first():
    bands = form_bands()

    assert [status for status, _, _ in bands] == [
        "high_risk",
        "optimal_training",
        "neutral",
        "fresh",
        "transition",
    ]
    assert [label for _, label, _ in bands] == [
        "High risk: below \u221230 %",
        "Optimal training: \u221230 to \u221210 %",
        "Neutral: \u221210 to +5 %",
        "Fresh: +5 to +25 %",
        "Transition: from +25 %",
    ]
    assert sum(width for _, _, width in bands) == FORM_BAR_MAX - FORM_BAR_MIN


@pytest.mark.parametrize(
    "pct, left",
    [(FORM_BAR_MIN, 0.0), (FORM_BAR_MAX, 100.0), (-200, 0.0), (90, 100.0), (-30, 22.2)],
)
def test_form_marker_stays_on_the_bar(pct, left):
    assert form_marker(pct) == left


def test_week_bands_cover_the_bar_lowest_first():
    bands = week_bands()

    assert [band for band, _, _ in bands] == ["below_target", "on_target", "above_limit"]
    assert [label for _, label, _ in bands] == [
        "Below target: under 100 %",
        "Target reached: 100 to 130 %",
        "Above upper limit: over 130 %",
    ]
    assert sum(width for _, _, width in bands) == WEEK_BAR_MAX


@pytest.mark.parametrize("pct, left", [(0, 0.0), (80, 50.0), (WEEK_BAR_MAX, 100.0), (400, 100.0)])
def test_week_marker_stays_on_the_bar(pct, left):
    assert week_marker(pct) == left
