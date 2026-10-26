"""Calorie estimates for activities whose source reports none.

Every rule works from the first activity: it needs only the configured athlete
values (``ftp``, ``max_hr``, ``rest_hr``) and, for weight-bearing sports, body
weight. No history and no power meter are required. Rules, first match wins:

1. ``power``: any activity with measured power, using intervals.icu's rule.
2. ``hr_power``: cycling with heart rate. Heart rate is turned into an
   estimated power via FTP, then rule 1 applies. Energy on a bike follows
   power, not body mass, which is why weight-based HR formulas overshoot for
   heavier riders.
3. ``acsm``: walking, hiking and running with a distance, from the ACSM
   metabolic equations (horizontal, vertical and resting oxygen cost).
4. ``met``: everything else listed in ``SPORT_METS``, as MET x weight x time.

Cycling without power or heart rate, and unknown sports, get no estimate:
a guess there would be presented as data.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Activity, BodyMeasurement

logger = logging.getLogger(__name__)

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


def _key(sport_type: str | None) -> str:
    """``Trail Run``, ``TRAIL_RUN`` and ``TrailRun`` all become ``trailrun``."""
    return "".join(ch for ch in (sport_type or "").lower() if ch.isalnum())


CYCLING = {
    _key(s)
    for s in (
        "Ride", "GravelRide", "MountainBikeRide", "VirtualRide", "EBikeRide",
        "EMountainBikeRide", "Velomobile", "Handcycle", "Biking", "Cycling",
        "IndoorCycling", "Spinning", "BikingStationary", "MountainBiking",
    )
}
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
class CalorieEstimate:
    kcal: float
    method: str


def kcal_from_power(average_watts: float, moving_time: int) -> float:
    return KCAL_PER_KJ * average_watts * moving_time / 1000.0


def estimate_calories(
    *,
    sport_type: str | None,
    moving_time: int,
    distance: float | None,
    elevation_gain: float | None,
    average_watts: float | None,
    device_watts: bool,
    average_heartrate: float | None,
    weight_kg: float | None,
    ftp: int,
    max_hr: int,
    rest_hr: int,
) -> CalorieEstimate | None:
    """kcal for one activity, or None when nothing sound supports a number."""
    if not moving_time or moving_time <= 0:
        return None
    sport = _key(sport_type)

    if device_watts and average_watts and average_watts > 0:
        return CalorieEstimate(kcal_from_power(average_watts, moving_time), "power")

    if sport in CYCLING:
        if not average_heartrate or max_hr <= rest_hr:
            return None
        hrr = min(max((average_heartrate - rest_hr) / (max_hr - rest_hr), 0.0), 1.0)
        watts = ftp * hrr / HRR_AT_FTP
        if watts <= 0:
            return None
        return CalorieEstimate(kcal_from_power(watts, moving_time), "hr_power")

    if not weight_kg:
        return None

    if (sport in WALKING or sport in RUNNING) and distance and distance > 0:
        horizontal, vertical = ACSM_RUN if sport in RUNNING else ACSM_WALK
        oxygen_ml_per_kg = (
            RESTING_VO2 * moving_time / 60.0
            + horizontal * distance
            + vertical * max(elevation_gain or 0.0, 0.0)
        )
        kcal = oxygen_ml_per_kg * weight_kg / 1000.0 * KCAL_PER_LITRE_O2
        return CalorieEstimate(kcal, "acsm")

    met = SPORT_METS.get(sport)
    if met is None:
        return None
    return CalorieEstimate(met * weight_kg * moving_time / 3600.0, "met")


class _Weights:
    """Nearest weigh-in by day, falling back to the configured weight."""

    def __init__(self, rows: list[tuple[date, float]], fallback: float | None):
        rows.sort()
        self._days = [day for day, _ in rows]
        self._kg = [kg for _, kg in rows]
        self._fallback = fallback

    def near(self, day: date) -> float | None:
        if not self._days:
            return self._fallback
        i = bisect.bisect_left(self._days, day)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(self._days)]
        best = min(candidates, key=lambda j: abs((self._days[j] - day).days))
        return self._kg[best]


async def recalculate_estimated_calories(
    session: AsyncSession,
    athlete_id: int,
    *,
    ftp: int,
    max_hr: int,
    rest_hr: int,
    fallback_weight_kg: float | None,
) -> int:
    """Refresh estimates for every activity without source calories.

    Recomputed each sync rather than once, so a changed FTP, HR range or a
    later weigh-in reaches older activities too. Returns rows changed.
    """
    weights = _Weights(
        [
            (day, kg)
            for day, kg in (
                await session.execute(
                    select(BodyMeasurement.day, BodyMeasurement.weight_kg).where(
                        BodyMeasurement.superseded_by.is_(None)
                    )
                )
            ).all()
        ],
        fallback_weight_kg,
    )
    activities = (
        await session.execute(
            select(Activity).where(
                Activity.athlete_id == athlete_id,
                Activity.deleted_upstream_at.is_(None),
            )
        )
    ).scalars()

    changes = []
    for activity in activities:
        estimate = None
        if activity.calories is None:
            estimate = estimate_calories(
                sport_type=activity.sport_type,
                moving_time=activity.moving_time,
                distance=activity.distance,
                elevation_gain=activity.total_elevation_gain,
                average_watts=activity.average_watts,
                device_watts=activity.device_watts,
                average_heartrate=activity.average_heartrate,
                weight_kg=weights.near(activity.start_date.date()),
                ftp=ftp,
                max_hr=max_hr,
                rest_hr=rest_hr,
            )
        kcal = round(estimate.kcal, 1) if estimate else None
        method = estimate.method if estimate else None
        if (activity.estimated_calories, activity.estimated_calories_method) != (
            kcal,
            method,
        ):
            changes.append(
                {
                    "id": activity.id,
                    "estimated_calories": kcal,
                    "estimated_calories_method": method,
                }
            )

    if changes:
        await session.execute(update(Activity), changes)
        await session.commit()
        logger.info("Updated estimated calories for %d activities", len(changes))
    return len(changes)
