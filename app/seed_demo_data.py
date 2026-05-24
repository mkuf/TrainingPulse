"""Populate the database with fully synthetic Strava-style demo data.

Designed for screenshotting the Grafana dashboards without leaking real
training data. Generates ~12 months of varied activities (rides, runs, MTB,
virtual rides, walks, hikes, strength) with realistic streams, then reuses
[`metrics.py`](metrics.py) to derive TRIMP, HR / power zones, the power
curve, and the daily CTL / ATL / TSB curve.

Also seeds the FDDB (`daily_nutrition`) and Withings (`weight_measurements`)
addon databases when `FDDB_DATABASE_URL` / `WITHINGS_DATABASE_URL` are set
(as in docker-compose). OAuth token tables are never touched.

Run inside the app container:

    docker compose exec app python seed_demo_data.py --force

All seeded rows belong to athlete id 99_999_999 with activity ids starting
at 9_000_000_000, so they are easy to delete later:

    DELETE FROM activities       WHERE athlete_id = 99999999;
    DELETE FROM activity_streams WHERE activity_id >= 9000000000;
    DELETE FROM daily_metrics    WHERE athlete_id = 99999999;
    DELETE FROM athlete_settings WHERE athlete_id = 99999999;

    -- fddb_nutrition database
    TRUNCATE daily_nutrition;

    -- withings database
    DELETE FROM weight_measurements WHERE grpid >= 9000000000;
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fddb_plugin.config import settings as fddb_settings
from fddb_plugin.models import Base as FddbBase
from fddb_plugin.models import DailyNutrition
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from trainingpulse_common import make_async_engine, make_session_factory
from withings_plugin.config import settings as withings_settings
from withings_plugin.models import Base as WithingsBase
from withings_plugin.models import WeightMeasurement

from database import async_session, engine
from metrics import (
    calculate_best_interval,
    calculate_power_curve,
    calculate_power_zones,
    calculate_trimp_from_streams,
    estimate_trimp_without_hr,
    get_hr_zone_boundaries,
    get_power_zone_boundaries,
    recalculate_daily_metrics,
)
from models import Activity, ActivityStream, AthleteSettings, Base, DailyMetrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seed_demo_data")

# ── Constants ───────────────────────────────────────────────────────

DEMO_ATHLETE_ID = 99_999_999
DEMO_ACTIVITY_ID_BASE = 9_000_000_000
DEMO_GRPID_BASE = 9_000_000_000

MAX_HR = 185
REST_HR = 50
FTP = 250

# Sport mix: (sport_type, weight, profile)
# Profile keys:
#   speed_kmh:    target average speed (used to derive velocity_smooth)
#   with_power:   whether to emit a watts stream
#   with_hr:      whether to emit a heartrate stream
#   duration_min: (min, max) minutes for this sport
#   weekend_bias: probability bump that long sessions of this sport fall on a weekend
SPORTS: list[tuple[str, float, dict]] = [
    ("Ride",             0.30, {"speed_kmh": 28, "with_power": True,  "with_hr": True,  "duration_min": (60, 240)}),
    ("MountainBikeRide", 0.08, {"speed_kmh": 18, "with_power": True,  "with_hr": True,  "duration_min": (60, 180)}),
    ("VirtualRide",      0.10, {"speed_kmh": 30, "with_power": True,  "with_hr": True,  "duration_min": (45, 90)}),
    ("Run",              0.25, {"speed_kmh": 11, "with_power": False, "with_hr": True,  "duration_min": (30, 90)}),
    ("Walk",             0.12, {"speed_kmh": 5,  "with_power": False, "with_hr": True,  "duration_min": (30, 60)}),
    ("WeightTraining",   0.08, {"speed_kmh": 0,  "with_power": False, "with_hr": False, "duration_min": (30, 60)}),
    ("Hike",             0.07, {"speed_kmh": 4,  "with_power": False, "with_hr": True,  "duration_min": (60, 180)}),
]

# Intensity targets as fractions of MAX_HR / FTP. HR fractions are chosen so
# the steady-state target falls in the middle of the corresponding zone defined
# in metrics.get_hr_zone_boundaries (Z1: 50-60%, Z2: 60-70%, ...). For interval
# work, on_s / off_s control the on/off pattern in seconds.
INTENSITY: dict[str, dict] = {
    "recovery": {"hr_frac": 0.55, "pwr_frac": 0.50, "on_s": 0,    "off_s": 0},
    "Z2":       {"hr_frac": 0.65, "pwr_frac": 0.65, "on_s": 0,    "off_s": 0},
    "Z3":       {"hr_frac": 0.77, "pwr_frac": 0.85, "on_s": 0,    "off_s": 0},
    "Z4":       {"hr_frac": 0.85, "pwr_frac": 1.00, "on_s": 1200, "off_s": 300},
    "Z5":       {"hr_frac": 0.93, "pwr_frac": 1.15, "on_s": 240,  "off_s": 240},
}

# 4-week mesocycle pattern; each entry is the intensity distribution for the week.
MESOCYCLE = [
    # base week
    {"Z2": 0.70, "Z3": 0.20, "Z4": 0.05, "recovery": 0.05},
    # build week
    {"Z2": 0.50, "Z3": 0.30, "Z4": 0.15, "Z5": 0.05},
    # peak week
    {"Z2": 0.40, "Z3": 0.30, "Z4": 0.15, "Z5": 0.10, "recovery": 0.05},
    # recovery week
    {"Z2": 0.30, "recovery": 0.65, "Z3": 0.05},
]

NAME_TEMPLATES: dict[tuple[str, str], list[str]] = {
    ("Ride", "Z2"):        ["Easy Z2 spin", "Morning endurance ride", "Steady ride", "Aerobic ride"],
    ("Ride", "Z3"):        ["Sweet spot session", "Tempo ride", "Steady tempo work"],
    ("Ride", "Z4"):        ["Threshold intervals", "FTP work", "2x20 at threshold"],
    ("Ride", "Z5"):        ["VO2 max repeats", "Short hard intervals"],
    ("Ride", "recovery"):  ["Recovery spin", "Easy coffee ride"],
    ("VirtualRide", "Z2"): ["Indoor endurance", "Trainer Z2"],
    ("VirtualRide", "Z3"): ["Trainer sweet spot"],
    ("VirtualRide", "Z4"): ["Indoor threshold", "Trainer 2x20"],
    ("VirtualRide", "Z5"): ["Indoor VO2 intervals"],
    ("VirtualRide", "recovery"): ["Trainer easy spin"],
    ("MountainBikeRide", "Z2"): ["Trail spin", "Easy MTB"],
    ("MountainBikeRide", "Z3"): ["MTB tempo loop"],
    ("MountainBikeRide", "Z4"): ["Punchy MTB intervals"],
    ("MountainBikeRide", "Z5"): ["MTB enduro effort"],
    ("MountainBikeRide", "recovery"): ["Easy trail roll"],
    ("Run", "Z2"):         ["Easy run", "Aerobic run", "Z2 run"],
    ("Run", "Z3"):         ["Steady run", "Marathon-pace run"],
    ("Run", "Z4"):         ["Tempo run", "Threshold run"],
    ("Run", "Z5"):         ["VO2 intervals", "Hill repeats", "Track session"],
    ("Run", "recovery"):   ["Recovery jog", "Shakeout run"],
    ("Walk", "Z2"):        ["Morning walk", "Lunchtime walk"],
    ("Walk", "recovery"):  ["Recovery walk"],
    ("Walk", "Z3"):        ["Brisk walk"],
    ("Hike", "Z2"):        ["Weekend hike", "Trail hike", "Forest hike"],
    ("Hike", "Z3"):        ["Steady mountain hike"],
    ("Hike", "recovery"):  ["Easy nature walk"],
    ("WeightTraining", "Z2"): ["Upper body strength", "Lower body strength", "Full body session"],
    ("WeightTraining", "Z3"): ["Heavy lift day"],
    ("WeightTraining", "recovery"): ["Mobility & core"],
}


@dataclass
class AthleteProfile:
    athlete_id: int = DEMO_ATHLETE_ID
    max_hr: int = MAX_HR
    rest_hr: int = REST_HR
    ftp: int = FTP


# ── Stream synthesis ────────────────────────────────────────────────


def _seasonal_temp(month: int) -> float:
    """Rough northern-hemisphere temperature curve (C), peak in July."""
    return 15.0 + 12.0 * math.sin(((month - 4) / 12.0) * 2 * math.pi)


def synthesize_streams(
    sport: str,
    sport_cfg: dict,
    duration_s: int,
    intensity: str,
    profile: AthleteProfile,
    start_date: datetime,
    rng: random.Random,
) -> dict:
    """Generate 1 Hz streams for one synthetic activity."""
    intensity_cfg = INTENSITY[intensity]
    n = duration_s
    time_stream = list(range(n))

    on_s = intensity_cfg["on_s"]
    off_s = intensity_cfg["off_s"]
    has_intervals = on_s > 0 and off_s > 0
    interval_period = on_s + off_s if has_intervals else 1

    # Heart rate
    heartrate: list[int] = []
    target_hr = profile.max_hr * intensity_cfg["hr_frac"]
    easy_hr = profile.max_hr * 0.62
    if sport_cfg["with_hr"]:
        for i in range(n):
            ramp = min(1.0, i / 60.0)  # 60-second ramp-in
            base = profile.rest_hr + (target_hr - profile.rest_hr) * ramp
            drift = (i / max(n, 1)) * 0.03 * target_hr
            noise = rng.gauss(0, 2.5)
            if has_intervals:
                on = (i % interval_period) < on_s
                hr = (base if on else easy_hr) + drift + noise
            else:
                hr = base + drift + noise
            heartrate.append(int(max(profile.rest_hr, min(profile.max_hr + 5, hr))))

    # Power
    watts: list[int] = []
    if sport_cfg["with_power"]:
        target_w = profile.ftp * intensity_cfg["pwr_frac"]
        easy_w = profile.ftp * 0.45
        for i in range(n):
            ramp = min(1.0, i / 30.0)
            base = target_w * ramp
            noise = rng.gauss(0, max(8.0, target_w * 0.04))
            if has_intervals:
                on = (i % interval_period) < on_s
                w = (base if on else easy_w) + noise
            else:
                w = base + noise
            watts.append(int(max(0, w)))

    # Cadence
    cadence: list[int] = []
    if sport in ("Ride", "VirtualRide", "MountainBikeRide"):
        cadence = [int(max(40, 85 + rng.gauss(0, 4))) for _ in range(n)]
    elif sport == "Run":
        cadence = [int(max(140, 172 + rng.gauss(0, 3))) for _ in range(n)]

    # Temperature
    base_temp = _seasonal_temp(start_date.month) + rng.gauss(0, 1.0)
    temp = [round(base_temp + rng.gauss(0, 0.4), 1) for _ in range(n)]

    # Velocity (m/s) and distance (m)
    base_speed_ms = sport_cfg["speed_kmh"] / 3.6
    velocity_smooth: list[float] = []
    distance: list[float] = []
    cum = 0.0
    for i in range(n):
        if base_speed_ms > 0:
            modulation = 1.0
            if heartrate:
                modulation = 0.85 + 0.30 * (heartrate[i] / max(target_hr, 1))
            v = max(0.0, base_speed_ms * modulation + rng.gauss(0, base_speed_ms * 0.08))
        else:
            v = 0.0
        velocity_smooth.append(round(v, 2))
        cum += v
        distance.append(round(cum, 1))

    # Altitude (rolling hills, base 100 m)
    altitude = [
        round(
            100.0
            + 30.0 * math.sin(i / 600.0)
            + 10.0 * math.sin(i / 90.0 + 1.3)
            + rng.gauss(0, 0.3),
            1,
        )
        for i in range(n)
    ]

    data: dict = {"time": time_stream}
    if heartrate:
        data["heartrate"] = heartrate
    if watts:
        data["watts"] = watts
    if cadence:
        data["cadence"] = cadence
    data["temp"] = temp
    if base_speed_ms > 0:
        data["velocity_smooth"] = velocity_smooth
        data["distance"] = distance
    data["altitude"] = altitude
    return data


# ── Helpers ─────────────────────────────────────────────────────────


def _weighted_choice(rng: random.Random, options: list[tuple]) -> tuple:
    """Pick one (key, weight, ...) tuple in proportion to weight."""
    total = sum(opt[1] for opt in options)
    r = rng.uniform(0, total)
    upto = 0.0
    for opt in options:
        upto += opt[1]
        if r <= upto:
            return opt
    return options[-1]


def _pick_intensity(rng: random.Random, distribution: dict[str, float]) -> str:
    items = list(distribution.items())
    total = sum(w for _, w in items)
    r = rng.uniform(0, total)
    upto = 0.0
    for name, w in items:
        upto += w
        if r <= upto:
            return name
    return items[-1][0]


def _pick_name(rng: random.Random, sport: str, intensity: str) -> str:
    options = NAME_TEMPLATES.get((sport, intensity))
    if options:
        return rng.choice(options)
    return f"{sport} {intensity}"


def _normalized_power(watts: list[int]) -> float | None:
    """Approximate Coggan NP: 30 s rolling mean, mean of 4th power, 4th root."""
    window = 30
    if len(watts) < window:
        return None
    rolling = []
    running = sum(watts[:window])
    rolling.append(running / window)
    for i in range(window, len(watts)):
        running += watts[i] - watts[i - window]
        rolling.append(running / window)
    fourth_mean = sum(x ** 4 for x in rolling) / len(rolling)
    return fourth_mean ** 0.25


def _elevation_gain(altitude: list[float]) -> float:
    return sum(max(0.0, altitude[i] - altitude[i - 1]) for i in range(1, len(altitude)))


# ── Activity row construction ───────────────────────────────────────


def build_activity(
    activity_id: int,
    profile: AthleteProfile,
    sport: str,
    sport_cfg: dict,
    intensity: str,
    duration_s: int,
    start_date: datetime,
    data: dict,
) -> tuple[Activity, ActivityStream]:
    """Compose the Activity row and the ActivityStream row from synthesized data."""
    time_stream = data["time"]
    hr_stream = data.get("heartrate", [])
    watts_stream = data.get("watts", [])
    velocity = data.get("velocity_smooth", [])
    altitude = data.get("altitude", [])

    has_heartrate = bool(hr_stream)
    device_watts = bool(watts_stream)

    # TRIMP and HR zones
    trimp_value: float | None = None
    hr_zone_seconds: dict | None = None
    if has_heartrate:
        zone_bounds = get_hr_zone_boundaries(profile.max_hr, profile.rest_hr)
        trimp_value, hr_zone_seconds = calculate_trimp_from_streams(
            time_stream, hr_stream, zone_bounds
        )
    else:
        trimp_value = estimate_trimp_without_hr(sport, duration_s)

    # Power zones, best 20-min power, power curve
    power_zone_seconds: dict | None = None
    best_20min: float | None = None
    power_curve: dict | None = None
    if device_watts:
        p_bounds = get_power_zone_boundaries(profile.ftp)
        power_zone_seconds = calculate_power_zones(time_stream, watts_stream, p_bounds)
        best_20min = calculate_best_interval(watts_stream, 1200)
        power_curve = calculate_power_curve(watts_stream)

    distance_m = sum(velocity) if velocity else 0.0
    avg_speed = (distance_m / duration_s) if (velocity and duration_s > 0) else None
    avg_hr = (sum(hr_stream) / len(hr_stream)) if hr_stream else None
    max_hr_val = max(hr_stream) if hr_stream else None
    avg_w = (sum(watts_stream) / len(watts_stream)) if watts_stream else None
    max_w = max(watts_stream) if watts_stream else None
    np = _normalized_power(watts_stream) if watts_stream else None
    elev_gain = _elevation_gain(altitude) if altitude else None
    kj = (avg_w * duration_s / 1000.0) if avg_w is not None else None
    if kj is not None:
        calories = round(kj * 1.05, 0)
    elif avg_hr is not None:
        # Rough HR-based estimate for non-power sports: ~0.10 kcal/kg/min at Z2.
        calories = round(duration_s / 60.0 * 8.0 * (avg_hr / 140.0), 0)
    else:
        calories = round(duration_s / 60.0 * 5.0, 0)

    suffer = round((trimp_value or 0.0) * 0.8, 0)

    name = _pick_name(random.Random(activity_id), sport, intensity)

    activity = Activity(
        id=activity_id,
        athlete_id=profile.athlete_id,
        name=name,
        description=f"Demo {sport.lower()} ({intensity}).",
        sport_type=sport,
        start_date=start_date,
        elapsed_time=duration_s,
        moving_time=duration_s,
        distance=round(distance_m, 1),
        average_heartrate=round(avg_hr, 1) if avg_hr is not None else None,
        max_heartrate=float(max_hr_val) if max_hr_val is not None else None,
        has_heartrate=has_heartrate,
        average_watts=round(avg_w, 1) if avg_w is not None else None,
        max_watts=float(max_w) if max_w is not None else None,
        average_speed=round(avg_speed, 3) if avg_speed is not None else None,
        total_elevation_gain=round(elev_gain, 1) if elev_gain is not None else None,
        kilojoules=round(kj, 1) if kj is not None else None,
        calories=calories,
        device_watts=device_watts,
        device_name="Demo Bike Computer" if device_watts else "Demo Watch",
        gear_id=("demo-bike" if sport in {"Ride", "VirtualRide", "MountainBikeRide"} else "demo-shoes"),
        gear_name=("Demo Road Bike" if sport == "Ride"
                   else "Demo MTB" if sport == "MountainBikeRide"
                   else "Demo Trainer" if sport == "VirtualRide"
                   else "Demo Running Shoes"),
        weighted_average_watts=round(np, 1) if np is not None else None,
        suffer_score=float(suffer),
        strava_detail_synced=True,
        trimp=round(trimp_value, 1) if trimp_value is not None else None,
        hr_zone_seconds=hr_zone_seconds,
        power_zone_seconds=power_zone_seconds,
        best_20min_power=round(best_20min, 1) if best_20min is not None else None,
        power_curve=power_curve,
        synced_streams=True,
    )

    stream_types = ",".join(k for k in data.keys() if k != "time")
    stream_row = ActivityStream(
        activity_id=activity_id,
        data=data,
        stream_types=stream_types,
        sample_count=len(time_stream),
    )
    return activity, stream_row


# ── Scheduling ──────────────────────────────────────────────────────


def plan_activities(days: int, rng: random.Random) -> list[tuple[datetime, str, str, int]]:
    """Build the schedule: (start_date, sport, intensity, duration_seconds) per activity."""
    plan: list[tuple[datetime, str, str, int]] = []
    end_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = end_day - timedelta(days=days)

    day = start_day
    week_index = 0
    while day < end_day:
        block = MESOCYCLE[week_index % len(MESOCYCLE)]
        # Choose 4-6 active days in this week (Mon-Sun).
        days_this_week = sorted(rng.sample(range(7), k=rng.randint(4, 6)))
        for offset in days_this_week:
            activity_day = day + timedelta(days=offset)
            if activity_day >= end_day:
                continue
            sport, _, sport_cfg = _weighted_choice(rng, SPORTS)
            intensity = _pick_intensity(rng, block)

            # Walks, hikes and strength work always stay easy so weekly TRIMP
            # is dominated by the structured ride/run sessions.
            if sport in ("Walk", "Hike", "WeightTraining"):
                intensity = rng.choice(["recovery", "Z2"])

            d_min, d_max = sport_cfg["duration_min"]
            is_easy = intensity in ("recovery", "Z2")
            # Hard interval sessions are short and structured; easy sessions
            # can run long, especially on weekends.
            if intensity in ("Z4", "Z5"):
                duration_min = rng.randint(d_min, min(d_max, 90))
            elif offset >= 5 and is_easy and sport in ("Ride", "Run", "MountainBikeRide", "Hike"):
                duration_min = rng.randint(int(d_max * 0.6), d_max)
            else:
                duration_min = rng.randint(d_min, d_max)
            duration_s = duration_min * 60

            # Random start time between 06:00 and 18:00 UTC.
            hour = rng.randint(6, 18)
            minute = rng.choice([0, 15, 30, 45])
            start_dt = activity_day.replace(hour=hour, minute=minute)
            plan.append((start_dt, sport, intensity, duration_s))

        day += timedelta(days=7)
        week_index += 1
    plan.sort(key=lambda t: t[0])
    return plan


# ── Addon data (FDDB + Withings) ─────────────────────────────────────


def _history_start_day(days: int) -> datetime:
    end_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return end_day - timedelta(days=days)


def generate_daily_nutrition(
    start_day: date,
    days: int,
    rng: random.Random,
    hard_training_days: set[date] | None = None,
) -> list[DailyNutrition]:
    """One FDDB row per calendar day with macros that bump on hard-training days."""
    hard = hard_training_days or set()
    rows: list[DailyNutrition] = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        kcal = rng.uniform(2200, 2600)
        if day.weekday() >= 5:
            kcal += rng.uniform(100, 300)
        if day in hard:
            kcal += rng.uniform(200, 400)
        kcal = round(kcal, 1)
        protein_g = round(kcal * 0.25 / 4, 1)
        carbs_g = round(kcal * 0.45 / 4, 1)
        fat_g = round(kcal * 0.30 / 9, 1)
        rows.append(
            DailyNutrition(
                date=day,
                kcal=kcal,
                protein_g=protein_g,
                carbs_g=carbs_g,
                sugar_g=round(carbs_g * rng.uniform(0.15, 0.35), 1),
                fat_g=fat_g,
                fiber_g=round(rng.uniform(25, 40), 1),
            )
        )
    return rows


def generate_weight_measurements(
    start_day: date,
    days: int,
    rng: random.Random,
) -> list[WeightMeasurement]:
    """Morning weigh-ins ~3–4×/week with a slow downward trend."""
    rows: list[WeightMeasurement] = []
    grpid = DEMO_GRPID_BASE
    span = max(days, 1)
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        weigh_in = day.weekday() in (0, 2, 4, 6) or (day.weekday() == 1 and rng.random() < 0.4)
        if not weigh_in:
            continue
        progress = offset / span
        weight_kg = round(72.5 - progress * 1.5 + rng.gauss(0, 0.15), 2)
        fat_mass_pct = round(16.5 - progress * 0.5 + rng.gauss(0, 0.3), 1)
        measured_at = datetime(
            day.year,
            day.month,
            day.day,
            7,
            rng.choice([0, 15, 30]),
            tzinfo=timezone.utc,
        )
        rows.append(
            WeightMeasurement(
                grpid=grpid,
                measured_at=measured_at,
                weight_kg=weight_kg,
                fat_mass_pct=fat_mass_pct,
                deviceid="demo-scale",
            )
        )
        grpid += 1
    return rows


# ── DB ops ──────────────────────────────────────────────────────────


async def _table_counts(session: AsyncSession) -> dict[str, int]:
    tables = {
        "activities": Activity,
        "activity_streams": ActivityStream,
        "daily_metrics": DailyMetrics,
        "athlete_settings": AthleteSettings,
    }
    counts: dict[str, int] = {}
    for name, model in tables.items():
        result = await session.execute(select(func.count()).select_from(model))
        counts[name] = int(result.scalar_one() or 0)
    return counts


async def _addon_table_counts(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one() or 0)


async def _collect_all_counts(
    main_session: AsyncSession,
    fddb_session: AsyncSession | None,
    withings_session: AsyncSession | None,
) -> dict[str, int]:
    counts = await _table_counts(main_session)
    if fddb_session is not None:
        counts["daily_nutrition"] = await _addon_table_counts(fddb_session, DailyNutrition)
    if withings_session is not None:
        counts["weight_measurements"] = await _addon_table_counts(withings_session, WeightMeasurement)
    return counts


async def _truncate(session: AsyncSession) -> None:
    logger.info("--force: truncating activities, activity_streams, daily_metrics, athlete_settings")
    for table in (ActivityStream, Activity, DailyMetrics, AthleteSettings):
        await session.execute(delete(table))
    await session.commit()


async def _truncate_addons(
    fddb_session: AsyncSession | None,
    withings_session: AsyncSession | None,
) -> None:
    if fddb_session is not None:
        logger.info("--force: truncating daily_nutrition")
        await fddb_session.execute(delete(DailyNutrition))
        await fddb_session.commit()
    if withings_session is not None:
        logger.info("--force: truncating demo weight_measurements (grpid >= %d)", DEMO_GRPID_BASE)
        await withings_session.execute(
            delete(WeightMeasurement).where(WeightMeasurement.grpid >= DEMO_GRPID_BASE)
        )
        await withings_session.commit()


async def _ensure_addon_schema(
    fddb_engine: AsyncEngine | None,
    withings_engine: AsyncEngine | None,
) -> None:
    if fddb_engine is not None:
        async with fddb_engine.begin() as conn:
            await conn.run_sync(FddbBase.metadata.create_all)
    if withings_engine is not None:
        async with withings_engine.begin() as conn:
            await conn.run_sync(WithingsBase.metadata.create_all)


@asynccontextmanager
async def _optional_session(session_factory) -> AsyncIterator[AsyncSession | None]:
    if session_factory is None:
        yield None
        return
    async with session_factory() as session:
        yield session


async def _seed_addons(
    args: argparse.Namespace,
    plan: list[tuple[datetime, str, str, int]],
    rng: random.Random,
    fddb_session_factory,
    withings_session_factory,
) -> None:
    history_start = _history_start_day(args.days)
    start_date = history_start.date()
    hard_training_days = {
        start_dt.date() for start_dt, _, intensity, _ in plan if intensity in ("Z4", "Z5")
    }

    nutrition_rows = generate_daily_nutrition(start_date, args.days, rng, hard_training_days)
    weight_rows = generate_weight_measurements(start_date, args.days, rng)

    if fddb_session_factory is not None:
        async with fddb_session_factory() as session:
            session.add_all(nutrition_rows)
            await session.commit()
        logger.info("Inserted %d daily_nutrition rows", len(nutrition_rows))

    if withings_session_factory is not None:
        async with withings_session_factory() as session:
            session.add_all(weight_rows)
            await session.commit()
        logger.info(
            "Inserted %d weight_measurements rows (grpid %d-%d)",
            len(weight_rows),
            DEMO_GRPID_BASE,
            DEMO_GRPID_BASE + len(weight_rows) - 1 if weight_rows else DEMO_GRPID_BASE,
        )


async def _upsert_profile(session: AsyncSession, profile: AthleteProfile) -> AthleteSettings:
    existing = (
        await session.execute(
            select(AthleteSettings).where(AthleteSettings.athlete_id == profile.athlete_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = AthleteSettings(
            athlete_id=profile.athlete_id,
            max_hr=profile.max_hr,
            rest_hr=profile.rest_hr,
            ftp=profile.ftp,
            hr_zones=None,
        )
        session.add(existing)
    else:
        existing.max_hr = profile.max_hr
        existing.rest_hr = profile.rest_hr
        existing.ftp = profile.ftp
        existing.hr_zones = None
    await session.commit()
    return existing


async def _seed(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    profile = AthleteProfile()

    fddb_engine = make_async_engine(fddb_settings.DATABASE_URL) if fddb_settings.DATABASE_URL else None
    withings_engine = (
        make_async_engine(withings_settings.DATABASE_URL) if withings_settings.DATABASE_URL else None
    )
    fddb_session_factory = make_session_factory(fddb_engine) if fddb_engine is not None else None
    withings_session_factory = (
        make_session_factory(withings_engine) if withings_engine is not None else None
    )

    # Make sure tables exist (mirrors main.py lifespan behavior, for standalone runs).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_addon_schema(fddb_engine, withings_engine)

    async with async_session() as session:
        async with _optional_session(fddb_session_factory) as fddb_session:
            async with _optional_session(withings_session_factory) as withings_session:
                counts = await _collect_all_counts(session, fddb_session, withings_session)
                non_empty = {t: c for t, c in counts.items() if c > 0}
                if non_empty and not args.force:
                    logger.error(
                        "Refusing to seed: existing rows found (%s). Re-run with --force to wipe.",
                        ", ".join(f"{t}={c}" for t, c in non_empty.items()),
                    )
                    sys.exit(2)
                if args.force:
                    await _truncate(session)
                    await _truncate_addons(fddb_session, withings_session)

                await _upsert_profile(session, profile)

                plan = plan_activities(args.days, rng)
                logger.info(
                    "Generating %d activities across %d days for athlete %d",
                    len(plan),
                    args.days,
                    profile.athlete_id,
                )

                best_20_overall: float = 0.0
                batch_activities: list[Activity] = []
                batch_streams: list[ActivityStream] = []
                BATCH = 25

                for i, (start_dt, sport, intensity, duration_s) in enumerate(plan):
                    sport_cfg = next(cfg for name, _, cfg in SPORTS if name == sport)
                    data = synthesize_streams(
                        sport, sport_cfg, duration_s, intensity, profile, start_dt, rng
                    )
                    activity, stream_row = build_activity(
                        DEMO_ACTIVITY_ID_BASE + i,
                        profile,
                        sport,
                        sport_cfg,
                        intensity,
                        duration_s,
                        start_dt,
                        data,
                    )
                    if (
                        activity.best_20min_power is not None
                        and activity.best_20min_power > best_20_overall
                    ):
                        best_20_overall = activity.best_20min_power

                    batch_activities.append(activity)
                    batch_streams.append(stream_row)

                    if len(batch_activities) >= BATCH:
                        session.add_all(batch_activities)
                        session.add_all(batch_streams)
                        await session.commit()
                        logger.info("Inserted %d / %d activities", i + 1, len(plan))
                        batch_activities.clear()
                        batch_streams.clear()

                if batch_activities:
                    session.add_all(batch_activities)
                    session.add_all(batch_streams)
                    await session.commit()

                if best_20_overall > 0:
                    settings_row = (
                        await session.execute(
                            select(AthleteSettings).where(
                                AthleteSettings.athlete_id == profile.athlete_id
                            )
                        )
                    ).scalar_one()
                    settings_row.estimated_ftp = int(round(best_20_overall * 0.95))
                    await session.commit()
                    logger.info(
                        "Best 20-min power = %.0f W, estimated_ftp = %d W",
                        best_20_overall,
                        settings_row.estimated_ftp,
                    )

                logger.info("Recalculating daily metrics (CTL / ATL / TSB)...")
                await recalculate_daily_metrics(session, profile.athlete_id)

                await _seed_addons(
                    args,
                    plan,
                    rng,
                    fddb_session_factory,
                    withings_session_factory,
                )

    await engine.dispose()
    if fddb_engine is not None:
        await fddb_engine.dispose()
    if withings_engine is not None:
        await withings_engine.dispose()
    logger.info(
        "Done. Athlete id = %d, activity ids %d-%d.",
        DEMO_ATHLETE_ID,
        DEMO_ACTIVITY_ID_BASE,
        DEMO_ACTIVITY_ID_BASE + len(plan) - 1,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=365, help="History length in days (default: 365).")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42).")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Truncate activities, activity_streams, daily_metrics, athlete_settings, "
            "daily_nutrition, and demo weight_measurements first."
        ),
    )
    args = parser.parse_args()
    asyncio.run(_seed(args))


if __name__ == "__main__":
    main()
