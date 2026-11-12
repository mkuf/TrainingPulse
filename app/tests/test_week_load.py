"""The weekly load target, its statuses, and the Grafana panels that show it."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from metrics import (
    WEEK_TARGET,
    WEEK_TARGET_MIN_DAYS,
    WEEK_UPPER_LIMIT,
    TrainingWeek,
    week_start,
    week_status,
)

FITNESS_DASHBOARD = (
    Path(__file__).resolve().parents[2] / "grafana" / "dashboards" / "fitness.json"
)


def _week(load: float, start_ctl: float | None, finished: bool = False) -> TrainingWeek:
    return TrainingWeek(
        start=date(2026, 11, 9),
        through=date(2026, 11, 12),
        load=load,
        start_ctl=start_ctl,
        finished=finished,
    )


def test_target_is_seven_times_the_start_of_week_fitness():
    week = _week(350.0, 50.0)
    assert week.target == pytest.approx(350.0 * WEEK_TARGET)
    assert week.upper_limit == pytest.approx(350.0 * WEEK_UPPER_LIMIT)
    assert week.pct_of_target == pytest.approx(100.0 / WEEK_TARGET)
    assert week.end == date(2026, 11, 15)


def test_remaining_stops_at_zero():
    assert _week(200.0, 50.0).remaining == pytest.approx(150.0)
    assert _week(500.0, 50.0).remaining == 0.0


def test_no_target_without_start_fitness():
    week = _week(200.0, None)
    assert (week.target, week.upper_limit, week.pct_of_target) == (None, None, None)
    assert (week.remaining, week.status) == (None, None)


@pytest.mark.parametrize(
    "load, finished, status",
    [
        (100.0, False, "in_progress"),
        (100.0, True, "below_target"),
        (350.0, False, "on_target"),
        (455.0, True, "on_target"),
        (456.0, False, "above_limit"),
    ],
)
def test_week_status(load, finished, status):
    assert week_status(load, 350.0, 455.0, finished) == status


def test_week_starts_on_monday():
    assert week_start(date(2026, 11, 15)) == date(2026, 11, 9)
    assert week_start(date(2026, 11, 9)) == date(2026, 11, 9)


def _panel(title: str) -> dict:
    dashboard = json.loads(FITNESS_DASHBOARD.read_text())
    return next(p for p in dashboard["panels"] if p["title"] == title)


@pytest.mark.parametrize("title", ["Weekly training load by sport", "This week vs target"])
def test_panels_use_the_same_limits(title):
    sql = "\n".join(t["rawSql"] for t in _panel(title)["targets"])
    factors = {float(f) for f in re.findall(r"\* (\d+\.\d+)", sql)}
    assert factors == {WEEK_TARGET, WEEK_UPPER_LIMIT}
    assert f">= {WEEK_TARGET_MIN_DAYS}" in sql
