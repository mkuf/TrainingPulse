"""Tests for the pure helpers behind the status page's Today summary."""

from __future__ import annotations

from datetime import date

import pytest
from today import duration, nutrition_window


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
