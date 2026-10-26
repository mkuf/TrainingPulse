"""Calories and kilojoules computed by TrainingPulse.

Computed once, when an activity's streams are processed, with the athlete
values configured at that time (like TRIMP): a later FTP or weight change does
not rewrite history, and a full resync recomputes it with current values.
Readers show ``COALESCE(computed_calories, calories)``, so the imported value
only appears where nothing is computed.

Computed values replace imported ones because devices disagree wildly at the
same effort: a Fitbit reported ~1.9x what a Galaxy Watch did for identical
heart rates, and Strava's HR formula ~1.7x what measured power implies.
Rules, first match wins:

1. ``power``: measured power. Work is the stream sum (else average watts x
   moving time); kcal = kJ / (4.184 x 0.22), intervals.icu's rule.
2. ``acsm``: walk, hike, run with a distance, from the ACSM metabolic
   equations (horizontal, vertical and resting oxygen cost) and body weight.
3. ``hr_power``: heart rate in any other sport. Heart rate is turned into
   power via FTP, then rule 1 applies. Metabolic rate follows heart rate
   reserve across sports, and unlike weight-based formulas this does not
   overshoot heavier athletes.
4. The imported value, when the source has one: nothing is computed.
5. ``met``: MET x weight x time for sports listed in ``SPORT_METS``. Last,
   because MET formulas scale with body weight and overshoot heavy athletes.
"""

from __future__ import annotations

from dataclasses import dataclass

#: intervals.icu's rule: kcal = kJ / (4.184 x 0.22), i.e. 22% gross
#: efficiency. Strava uses x 1.115 (~21.4%), 2.6% higher.
GROSS_EFFICIENCY = 0.22
KCAL_PER_KJ = 1 / (4.184 * GROSS_EFFICIENCY)
#: Fraction of heart rate reserve typically held at FTP.
HRR_AT_FTP = 0.88
#: Oxygen cost at rest (1 MET), ml/kg/min.
RESTING_VO2 = 3.5
#: Energy per litre of oxygen, kcal.
KCAL_PER_LITRE_O2 = 5.0
#: ACSM oxygen cost per metre travelled, ml/kg: (horizontal, vertical).
ACSM_WALK = (0.1, 1.8)
ACSM_RUN = (0.2, 0.9)
#: Longest gap a power sample is credited for; recorders sampling sparser
#: than 1 Hz stay well below it. After a longer gap (auto-pause, dropout) the
#: sample counts as one second, like intervals.icu's ``icu_joules``.
MAX_SAMPLE_GAP_SECONDS = 5


def _key(sport_type: str | None) -> str:
    """``Trail Run``, ``TRAIL_RUN`` and ``TrailRun`` all become ``trailrun``."""
    return "".join(ch for ch in (sport_type or "").lower() if ch.isalnum())


WALKING = {_key(s) for s in ("Walk", "Hike", "Walking", "Hiking")}
RUNNING = {
    _key(s)
    for s in ("Run", "TrailRun", "VirtualRun", "Running", "TreadmillRunning")
}

#: Compendium of Physical Activities (2024), general-intensity METs.
SPORT_METS = {
    _key(sport): met
    for sport, met in {
        # Walking and running without a distance.
        "Walk": 3.5, "Walking": 3.5, "Hike": 6.0, "Hiking": 6.0,
        "Run": 9.0, "Running": 9.0, "TrailRun": 9.0, "VirtualRun": 9.0,
        "TreadmillRunning": 9.0,
        # Gym and fitness.
        "WeightTraining": 3.5, "Weightlifting": 3.5, "Workout": 5.0,
        "Crossfit": 8.0, "HighIntensityIntervalTraining": 8.0, "Hiit": 8.0,
        "Yoga": 2.5, "Pilates": 3.0, "Elliptical": 5.0, "StairStepper": 9.0,
        "Rowing": 7.0, "VirtualRow": 7.0,
        # Water.
        "Swim": 7.0, "Swimming": 7.0, "Kayaking": 5.0, "Canoeing": 4.0,
        "StandUpPaddling": 6.0, "Surfing": 3.0,
        # Snow and ice.
        "AlpineSki": 5.3, "BackcountrySki": 9.0, "NordicSki": 9.0,
        "Snowboard": 5.3, "Snowshoe": 7.0, "IceSkate": 7.0,
        # Other.
        "InlineSkate": 7.5, "RockClimbing": 7.5, "Golf": 4.8,
        "Tennis": 7.3, "Badminton": 5.5, "Squash": 7.3, "TableTennis": 4.0,
        "Soccer": 7.0, "Basketball": 6.5,
    }.items()
}


@dataclass(frozen=True)
class ActivityEnergyInputs:
    """The activity fields the rules read, captured before any DB rollback."""

    sport_type: str | None
    moving_time: int
    distance: float | None
    elevation_gain: float | None
    average_watts: float | None
    device_watts: bool
    average_heartrate: float | None
    imported_calories: float | None


@dataclass(frozen=True)
class AthleteEnergyInputs:
    ftp: int
    max_hr: int
    rest_hr: int
    weight_kg: float | None


@dataclass(frozen=True)
class Energy:
    kcal: float | None = None
    kilojoules: float | None = None
    method: str | None = None


def kilojoules_from_stream(time_data: list, watts_data: list) -> float | None:
    """Mechanical work from a power stream; ``None`` samples count as 0 W.

    For 1 Hz recordings this is the plain sum of the samples, which is exactly
    how intervals.icu reports ``icu_joules``.
    """
    if not watts_data or not time_data or len(time_data) != len(watts_data):
        return None
    joules = 0.0
    for i in range(1, len(watts_data)):
        dt = time_data[i] - time_data[i - 1]
        if dt <= 0:
            continue
        seconds = dt if dt <= MAX_SAMPLE_GAP_SECONDS else 1
        joules += max(watts_data[i] or 0, 0) * seconds
    return joules / 1000.0 if joules > 0 else None


def compute_energy(
    activity: ActivityEnergyInputs,
    athlete: AthleteEnergyInputs,
    *,
    stream_kilojoules: float | None = None,
) -> Energy:
    """kcal (and kJ for measured power) by the first rule that applies."""
    moving = activity.moving_time or 0
    if moving <= 0:
        return Energy()
    sport = _key(activity.sport_type)
    weight = athlete.weight_kg

    if activity.device_watts:
        kj = stream_kilojoules
        if kj is None and activity.average_watts and activity.average_watts > 0:
            kj = activity.average_watts * moving / 1000.0
        if kj:
            return Energy(kj * KCAL_PER_KJ, kj, "power")

    is_foot = sport in WALKING or sport in RUNNING
    if is_foot and weight and activity.distance and activity.distance > 0:
        horizontal, vertical = ACSM_RUN if sport in RUNNING else ACSM_WALK
        oxygen_ml_per_kg = (
            RESTING_VO2 * moving / 60.0
            + horizontal * activity.distance
            + vertical * max(activity.elevation_gain or 0.0, 0.0)
        )
        kcal = oxygen_ml_per_kg * weight / 1000.0 * KCAL_PER_LITRE_O2
        return Energy(kcal, None, "acsm")

    hr = activity.average_heartrate
    if hr and athlete.max_hr > athlete.rest_hr:
        hrr = min(max((hr - athlete.rest_hr) / (athlete.max_hr - athlete.rest_hr), 0.0), 1.0)
        watts = athlete.ftp * hrr / HRR_AT_FTP
        if watts > 0:
            return Energy(watts * moving / 1000.0 * KCAL_PER_KJ, None, "hr_power")

    if activity.imported_calories is not None:
        return Energy()

    met = SPORT_METS.get(sport)
    if met is None or not weight:
        return Energy()
    return Energy(met * weight * moving / 3600.0, None, "met")
