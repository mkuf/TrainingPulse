"""Tests for the sleep and recovery scores TrainingPulse computes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import wellness_scores as ws

BERLIN = ZoneInfo("Europe/Berlin")


def _night(**overrides) -> ws.NightInputs:
    values = dict(
        asleep_seconds=8 * 3600,
        in_bed_seconds=8 * 3600 / 0.95,
        awake_seconds=10 * 60,
        bedtime_spread_min=15.0,
        hr_dip_pct=15.0,
        hrv_ratio=1.0,
    )
    values.update(overrides)
    return ws.NightInputs(**values)


def test_scale_runs_both_ways_and_clamps():
    assert ws.scale(0.75, 0.5, 1.0) == 50
    assert ws.scale(2.0, 0.5, 1.0) == 100
    assert ws.scale(35.0, 60.0, 10.0) == 50
    assert ws.scale(None, 0, 1) is None


def test_an_ideal_night_scores_100():
    score, parts = ws.sleep_score(_night(), sleep_need_hours=8)

    assert score == 100
    assert set(parts) == set(ws.SLEEP_WEIGHTS)


def test_short_sleep_costs_the_duration_part():
    night = _night(asleep_seconds=6 * 3600, in_bed_seconds=6 * 3600 / 0.95)
    score, parts = ws.sleep_score(night, sleep_need_hours=8)

    assert parts["duration"]["score"] == 50
    assert score == pytest.approx(100 - 0.35 * 50, abs=0.1)


def test_missing_parts_are_left_out_and_weights_rescaled():
    score, parts = ws.sleep_score(
        _night(bedtime_spread_min=None, hr_dip_pct=None, hrv_ratio=None),
        sleep_need_hours=8,
    )

    assert parts["regularity"]["score"] is None
    assert parts["physiology"]["score"] is None
    assert score == 100


def test_a_night_without_time_asleep_has_no_score():
    assert ws.sleep_score(_night(asleep_seconds=None), sleep_need_hours=8) == (None, {})


HRV_USUAL = ws.Usual(median=80.0, spread=8.0)
REST_USUAL = ws.Usual(median=46.0, spread=2.0)


def _recovery(hrv_ms=80.0, resting_hr=46.0, hrv_usual=HRV_USUAL, rest_usual=REST_USUAL):
    return ws.recovery_score(
        hrv_ms=hrv_ms,
        hrv_usual=hrv_usual,
        resting_hr=resting_hr,
        resting_hr_usual=rest_usual,
    )


def test_a_usual_day_scores_70():
    score, parts = _recovery()

    assert score == 70
    assert set(parts) == set(ws.RECOVERY_WEIGHTS)
    assert parts["hrv"] == {"value": 1.0, "score": 70, "weight": 60}
    assert parts["resting_hr"] == {"value": 0.0, "score": 70, "weight": 40}


def test_recovery_counts_in_the_athletes_own_variation():
    # One standard deviation better on both: higher HRV, lower resting HR.
    score, parts = _recovery(hrv_ms=88.0, resting_hr=44.0)
    assert score == 85
    assert parts["hrv"]["value"] == 1.1
    assert parts["resting_hr"]["value"] == -2.0

    # The same 8 ms are half as unusual for someone who varies twice as much.
    score, _ = _recovery(hrv_ms=72.0, hrv_usual=ws.Usual(80.0, 16.0))
    assert score == pytest.approx(0.6 * 62.5 + 0.4 * 70, abs=0.1)


def test_recovery_parts_are_clamped():
    assert _recovery(hrv_ms=160.0, resting_hr=30.0)[0] == 100
    assert _recovery(hrv_ms=20.0, resting_hr=70.0)[0] == 0


def test_recovery_uses_the_part_that_has_a_usual_value():
    score, parts = _recovery(hrv_ms=None, resting_hr=48.0)

    assert parts["hrv"]["score"] is None
    assert score == 55


def test_recovery_needs_a_usual_value():
    assert _recovery(hrv_usual=None, rest_usual=None) == (None, {})
    assert _recovery(hrv_ms=None, resting_hr=None) == (None, {})


@pytest.mark.parametrize(
    "score, status",
    [
        (100, "above_normal"),
        (85, "above_normal"),
        (84.9, "normal"),
        (70, "normal"),
        (60, "normal"),
        (59.9, "below_normal"),
        (40, "below_normal"),
        (39.9, "well_below_normal"),
        (0, "well_below_normal"),
        (None, None),
    ],
)
def test_recovery_status_names_the_band(score, status):
    assert ws.recovery_status(score) == status


def test_usual_needs_two_weeks_and_has_a_minimum_spread():
    day = date(2026, 10, 29)
    history = {day - timedelta(days=offset): 60.0 for offset in range(1, 14)}
    assert ws.usual(history, day, min_spread=3.0) is None

    history[day - timedelta(days=14)] = 60.0
    history[day] = 999.0  # the day itself is never part of its own usual value
    assert ws.usual(history, day, min_spread=3.0) == ws.Usual(60.0, 3.0)

    history[day - timedelta(days=1)] = 88.0
    found = ws.usual(history, day, min_spread=3.0)
    assert found.median == 60.0
    assert found.spread == pytest.approx(7.48, abs=0.01)


def test_baseline_needs_a_week_of_the_last_30_days():
    day = date(2026, 10, 29)
    history = {day - timedelta(days=offset): 60.0 + offset for offset in range(1, 7)}
    assert ws.baseline(history, day) is None

    history[day - timedelta(days=7)] = 67.0
    history[day] = 999.0  # the day itself is never part of its own baseline
    history[day - timedelta(days=31)] = 999.0  # nor anything older than 30 days
    assert ws.baseline(history, day) == 64.0


def test_bedtime_spread_treats_midnight_as_continuous():
    day = date(2026, 10, 29)

    def at(days_back: int, hour: int, minute: int = 0) -> datetime:
        local = datetime(2026, 10, 28, hour, minute, tzinfo=BERLIN) - timedelta(
            days=days_back
        )
        return local.astimezone(timezone.utc)

    bedtimes = {
        day: at(0, 23, 30),
        day - timedelta(days=1): datetime(2026, 10, 28, 0, 30, tzinfo=BERLIN),
        day - timedelta(days=2): at(2, 23, 30),
    }
    # 23:30, 00:30, 23:30 → spread of about 28 minutes, not 11 hours.
    assert ws.bedtime_spread(bedtimes, day, BERLIN) == pytest.approx(28.3, abs=0.1)


def test_bedtime_spread_needs_three_nights():
    day = date(2026, 10, 29)
    bedtimes = {day: datetime(2026, 10, 28, 22, tzinfo=BERLIN)}
    assert ws.bedtime_spread(bedtimes, day, BERLIN) is None
