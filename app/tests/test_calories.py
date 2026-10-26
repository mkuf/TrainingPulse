"""Tests for the day-one calorie estimates in ``calories.py``."""

from __future__ import annotations

from datetime import date

import pytest
from calories import KCAL_PER_KJ, _Weights, estimate_calories

ATHLETE = {"ftp": 200, "max_hr": 190, "rest_hr": 60}


def estimate(**overrides):
    fields = dict(
        sport_type="Ride",
        moving_time=3600,
        distance=None,
        elevation_gain=None,
        average_watts=None,
        device_watts=False,
        average_heartrate=None,
        weight_kg=75.0,
        **ATHLETE,
    )
    fields.update(overrides)
    return estimate_calories(**fields)


# ── Power ───────────────────────────────────────────────────────────


def test_power_matches_intervals_icu_rule():
    result = estimate(average_watts=200, device_watts=True)

    # One hour at 200 W is 720 kJ; at 22% efficiency that is 782.2 kcal.
    assert result.method == "power"
    assert result.kcal == pytest.approx(720 / (4.184 * 0.22))


def test_power_reproduces_an_intervals_icu_ride():
    # 2026-10-24: 366.6 kJ of work, intervals.icu reported 398 kcal.
    result = estimate(average_watts=366.6, device_watts=True, moving_time=1000)

    assert round(result.kcal) == 398


def test_power_does_not_depend_on_weight():
    light = estimate(average_watts=150, device_watts=True, weight_kg=60)
    heavy = estimate(average_watts=150, device_watts=True, weight_kg=140)

    assert light.kcal == heavy.kcal


def test_estimated_power_is_not_treated_as_measured():
    result = estimate(average_watts=180, device_watts=False)

    assert result is None


def test_power_applies_to_any_sport():
    result = estimate(sport_type="Run", average_watts=250, device_watts=True)

    assert result.method == "power"


# ── Cycling with heart rate ─────────────────────────────────────────


def test_hr_at_threshold_rides_at_ftp():
    # 0.88 of the 60..190 heart rate reserve is 174.4 bpm.
    result = estimate(average_heartrate=60 + 0.88 * 130)

    assert result.method == "hr_power"
    assert result.kcal == pytest.approx(KCAL_PER_KJ * 200 * 3600 / 1000)


def test_hr_power_scales_with_ftp_not_weight():
    base = estimate(average_heartrate=140)

    assert estimate(average_heartrate=140, weight_kg=140).kcal == base.kcal
    assert estimate(average_heartrate=140, ftp=300).kcal == pytest.approx(
        base.kcal * 1.5
    )


def test_hr_at_or_below_rest_gives_no_estimate():
    assert estimate(average_heartrate=55) is None


def test_hr_above_max_is_capped():
    assert estimate(average_heartrate=210).kcal == estimate(average_heartrate=190).kcal


def test_cycling_without_power_or_hr_gives_no_estimate():
    assert estimate(sport_type="GravelRide") is None


def test_cycling_names_from_other_providers_are_recognised():
    assert estimate(sport_type="BIKING", average_heartrate=140).method == "hr_power"


# ── Walk, hike, run ─────────────────────────────────────────────────


def test_walk_uses_acsm_distance_climb_and_rest():
    result = estimate(
        sport_type="Walk", moving_time=3600, distance=5000, elevation_gain=100
    )

    # O2 ml/kg: rest 3.5*60 + horizontal 0.1*5000 + vertical 1.8*100 = 890.
    assert result.method == "acsm"
    assert result.kcal == pytest.approx(890 * 75 / 1000 * 5)


def test_run_costs_more_per_metre_than_walking():
    walk = estimate(sport_type="Walk", distance=5000)
    run = estimate(sport_type="Run", distance=5000)

    assert run.kcal > walk.kcal


def test_trail_run_with_space_is_a_run():
    assert estimate(sport_type="Trail Run", distance=8000).method == "acsm"


def test_walk_without_distance_falls_back_to_met():
    result = estimate(sport_type="Walk", distance=0)

    assert result.method == "met"
    assert result.kcal == pytest.approx(3.5 * 75)


# ── Other sports ────────────────────────────────────────────────────


def test_met_sport_is_met_times_weight_times_hours():
    result = estimate(sport_type="WeightTraining", moving_time=1800)

    assert result.method == "met"
    assert result.kcal == pytest.approx(3.5 * 75 * 0.5)


def test_unknown_sport_gives_no_estimate():
    assert estimate(sport_type="Kitesurf") is None


def test_weight_bearing_sport_without_weight_gives_no_estimate():
    assert estimate(sport_type="Walk", distance=5000, weight_kg=None) is None


def test_zero_moving_time_gives_no_estimate():
    assert estimate(average_watts=200, device_watts=True, moving_time=0) is None


# ── Weight lookup ───────────────────────────────────────────────────


def test_nearest_weigh_in_wins():
    weights = _Weights(
        [(date(2026, 1, 10), 80.0), (date(2026, 1, 1), 82.0)], fallback=70.0
    )

    assert weights.near(date(2026, 1, 4)) == 82.0
    assert weights.near(date(2026, 1, 8)) == 80.0
    assert weights.near(date(2027, 1, 1)) == 80.0


def test_configured_weight_is_the_fallback():
    assert _Weights([], fallback=70.0).near(date(2026, 1, 1)) == 70.0
    assert _Weights([], fallback=None).near(date(2026, 1, 1)) is None
