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


def test_recovery_is_centred_on_the_personal_baseline():
    score, parts = ws.recovery_score(hrv_ratio=1.0, resting_hr_delta=0.0, sleep=50.0)

    assert score == 50
    assert parts["hrv"]["score"] == 50
    assert parts["resting_hr"]["score"] == 50


def test_high_hrv_and_low_resting_hr_mean_good_recovery():
    score, _ = ws.recovery_score(hrv_ratio=1.2, resting_hr_delta=-5.0, sleep=100.0)
    assert score == 100

    score, _ = ws.recovery_score(hrv_ratio=0.8, resting_hr_delta=5.0, sleep=0.0)
    assert score == 0


def test_recovery_needs_a_personal_baseline():
    assert ws.recovery_score(hrv_ratio=None, resting_hr_delta=None, sleep=90.0) == (
        None,
        {},
    )


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
