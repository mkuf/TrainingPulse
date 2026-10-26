"""Tests for the computed calories and kilojoules in ``calories.py``."""

from __future__ import annotations

import pytest
from calories import (
    ACSM_WALK,
    KCAL_PER_KJ,
    KCAL_PER_LITRE_O2,
    MAX_SAMPLE_GAP_SECONDS,
    RESTING_VO2,
    ActivityEnergyInputs,
    AthleteEnergyInputs,
    compute_energy,
    kilojoules_from_stream,
)

ATHLETE = AthleteEnergyInputs(ftp=200, max_hr=190, rest_hr=60, weight_kg=75.0)


def compute(*, athlete=ATHLETE, stream_kilojoules=None, **overrides):
    fields = dict(
        sport_type="Ride",
        moving_time=3600,
        distance=None,
        elevation_gain=None,
        average_watts=None,
        device_watts=False,
        average_heartrate=None,
        imported_calories=None,
    )
    fields.update(overrides)
    return compute_energy(
        ActivityEnergyInputs(**fields), athlete, stream_kilojoules=stream_kilojoules
    )


# ── Power ───────────────────────────────────────────────────────────


def test_power_uses_intervals_icu_rule():
    energy = compute(average_watts=200, device_watts=True)

    # One hour at 200 W is 720 kJ; at 22% efficiency that is 782.2 kcal.
    assert energy.method == "power"
    assert energy.kilojoules == pytest.approx(720.0)
    assert energy.kcal == pytest.approx(720 / (4.184 * 0.22))


def test_power_reproduces_an_intervals_icu_ride():
    # 2026-10-24: 366.6 kJ of work, intervals.icu reported 398 kcal.
    energy = compute(device_watts=True, average_watts=100, stream_kilojoules=366.6)

    assert round(energy.kcal) == 398


def test_stream_work_wins_over_average_watts():
    energy = compute(average_watts=150, device_watts=True, stream_kilojoules=600.0)

    assert energy.kilojoules == pytest.approx(600.0)


def test_power_ignores_imported_calories_and_weight():
    base = compute(average_watts=150, device_watts=True)
    other = compute(
        average_watts=150,
        device_watts=True,
        imported_calories=2000,
        athlete=AthleteEnergyInputs(ftp=200, max_hr=190, rest_hr=60, weight_kg=140),
    )

    assert other == base


def test_estimated_power_is_not_treated_as_measured():
    assert compute(average_watts=180, device_watts=False).method is None


# ── Walk, hike, run ─────────────────────────────────────────────────


def test_walk_uses_acsm_distance_climb_and_rest():
    energy = compute(sport_type="Walk", distance=5000, elevation_gain=100)

    # O2 ml/kg: rest 3.5*60 + horizontal 0.1*5000 + vertical 1.8*100 = 890.
    assert energy.method == "acsm"
    assert energy.kcal == pytest.approx(890 * 75 / 1000 * 5)
    assert energy.kilojoules is None


def test_acsm_wins_over_heart_rate_for_walks():
    energy = compute(sport_type="Walk", distance=5000, average_heartrate=110)

    assert energy.method == "acsm"


def test_run_costs_more_per_metre_than_walking():
    walk = compute(sport_type="Walk", distance=5000)
    run = compute(sport_type="Trail Run", distance=5000)

    assert run.kcal > walk.kcal


def test_walk_without_distance_uses_heart_rate():
    assert compute(sport_type="Walk", average_heartrate=110).method == "hr_power"


# ── Heart rate ──────────────────────────────────────────────────────


def test_hr_at_threshold_rides_at_ftp():
    # 0.88 of the 60..190 heart rate reserve is 174.4 bpm.
    energy = compute(average_heartrate=60 + 0.88 * 130)

    assert energy.method == "hr_power"
    assert energy.kcal == pytest.approx(KCAL_PER_KJ * 200 * 3600 / 1000)
    assert energy.kilojoules is None


@pytest.mark.parametrize("sport", ["Crossfit", "WeightTraining", "Workout", "Swim"])
def test_hr_power_replaces_met_for_heart_rate_sports(sport):
    assert compute(sport_type=sport, average_heartrate=121).method == "hr_power"


def test_hr_power_ignores_imported_calories():
    # The Fitbit-vs-Galaxy case: same heart rate, very different device kcal.
    galaxy = compute(sport_type="Crossfit", average_heartrate=121, imported_calories=570)
    fitbit = compute(sport_type="Crossfit", average_heartrate=121, imported_calories=1050)

    assert galaxy == fitbit


def test_hr_power_scales_with_ftp_not_weight():
    base = compute(average_heartrate=140)
    heavy = compute(
        average_heartrate=140,
        athlete=AthleteEnergyInputs(ftp=200, max_hr=190, rest_hr=60, weight_kg=140),
    )
    strong = compute(
        average_heartrate=140,
        athlete=AthleteEnergyInputs(ftp=300, max_hr=190, rest_hr=60, weight_kg=75),
    )

    assert heavy.kcal == base.kcal
    assert strong.kcal == pytest.approx(base.kcal * 1.5)


def test_hr_above_max_is_capped():
    assert compute(average_heartrate=210).kcal == compute(average_heartrate=190).kcal


# ── Imported, then MET ──────────────────────────────────────────────


def test_imported_value_is_kept_when_nothing_else_applies():
    energy = compute(sport_type="WeightTraining", imported_calories=250)

    assert energy.kcal is None
    assert energy.method is None


def test_met_is_the_last_resort():
    energy = compute(sport_type="WeightTraining", moving_time=1800)

    assert energy.method == "met"
    assert energy.kcal == pytest.approx(3.5 * 75 * 0.5)


def test_hr_at_or_below_rest_falls_through_to_met():
    assert compute(sport_type="Workout", average_heartrate=55).method == "met"


def test_cycling_without_power_or_hr_gets_nothing():
    assert compute(sport_type="GravelRide").method is None


def test_unknown_sport_gets_nothing():
    assert compute(sport_type="Kitesurf").method is None


def test_weight_needed_for_acsm_and_met():
    no_weight = AthleteEnergyInputs(ftp=200, max_hr=190, rest_hr=60, weight_kg=None)

    assert compute(sport_type="Walk", distance=5000, athlete=no_weight).method is None
    assert compute(sport_type="Yoga", athlete=no_weight).method is None


def test_zero_moving_time_gets_nothing():
    assert compute(average_watts=200, device_watts=True, moving_time=0).method is None


# ── Stream work ─────────────────────────────────────────────────────


def test_stream_work_sums_watts_times_seconds():
    assert kilojoules_from_stream(list(range(3601)), [200] * 3601) == pytest.approx(
        720.0
    )


def test_stream_work_counts_one_second_after_a_pause():
    # A sample after a pause or dropout counts one second, like intervals.icu.
    time = [0, 1, 2, 2 + MAX_SAMPLE_GAP_SECONDS + 1, 3 + MAX_SAMPLE_GAP_SECONDS + 1]

    assert kilojoules_from_stream(time, [300, None, 300, 300, 300]) == pytest.approx(
        0.9
    )


def test_stream_work_credits_sparse_recording():
    # Smart recording every 3 s: each sample stands for its 3 s.
    assert kilojoules_from_stream([0, 3, 6, 9], [200] * 4) == pytest.approx(1.8)


def test_stream_work_reproduces_icu_joules_for_1hz_data():
    watts = [0, 210, 190, None, 205]

    assert kilojoules_from_stream(list(range(5)), watts) == pytest.approx(
        sum(w or 0 for w in watts[1:]) / 1000
    )


def test_stream_work_needs_aligned_streams():
    assert kilojoules_from_stream([0, 1], [100]) is None
    assert kilojoules_from_stream([0, 1, 2], [0, 0, 0]) is None


def test_acsm_constants_are_the_published_ones():
    assert (RESTING_VO2, ACSM_WALK, KCAL_PER_LITRE_O2) == (3.5, (0.1, 1.8), 5.0)
