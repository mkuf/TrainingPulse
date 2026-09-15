"""Recorded provider payloads for the source mapper tests.

Trimmed to the fields the mappers read, with ids, tokens, and GPS removed or
replaced. Shapes match what the real APIs return, including the alternate key
spellings each provider is known to use.
"""

from __future__ import annotations

# ── intervals.icu ───────────────────────────────────────────────────

ICU_LIST_ACTIVITY = {
    "id": "i87654321",
    "name": "Zwift Race",
    "type": "Ride",
    "start_date": "2026-03-14T17:05:00Z",
    "elapsed_time": 3720,
    "moving_time": 3600,
    "distance": 41234.5,
    "has_heartrate": True,
    "average_heartrate": 148.2,
    "max_heartrate": 181.0,
    "average_watts": 212.4,
    "max_watts": 690.0,
    "weighted_average_watts": 228.9,
    "device_watts": True,
    "average_speed": 11.45,
    "total_elevation_gain": 312.0,
    "kilojoules": 764.6,
    "calories": 820,
    "icu_training_load": 96.0,
    "gear_id": "b1234567",
    "device_name": "Wahoo KICKR",
    "updated": "2026-03-14T19:00:00Z",
}

#: intervals.icu also exposes several fields under icu_-prefixed aliases.
ICU_LIST_ACTIVITY_ALIASES = {
    "id": 555,
    "name": "Alias Ride",
    "sport_type": "VirtualRide",
    "start_date_local": "2026-03-15T06:00:00",
    "icu_elapsed_time": 1800,
    "icu_moving_time": 1700,
    "icu_average_heartrate": 130,
    "icu_max_heartrate": 160,
    "icu_average_watts": 180,
    "icu_max_watts": 400,
    "icu_weighted_avg_watts": 190,
    "icu_gear_id": "b999",
    "icu_device": "Garmin Edge",
    "training_load": 44,
    "icu_joules": 500,
}

ICU_ACTIVITY_NO_START = {"id": "i1", "name": "Broken"}

ICU_STREAMS_FLAT = {
    "time": [0, 1, 2],
    "heart_rate": [120, 122, 125],
    "power": [200, 210, 205],
    "speed": [8.1, 8.2, 8.3],
    "lat_lng": [[1.0, 2.0], [1.0, 2.1], [1.0, 2.2]],
}

ICU_STREAMS_WRAPPED = {
    "time": {"data": [0, 1]},
    "heartrate": {"values": [100, 101]},
    "_meta": {"ignored": True},
}

# ── Strava ──────────────────────────────────────────────────────────

STRAVA_LIST_ACTIVITY = {
    "id": 1122334455,
    "name": "Morning Run",
    "sport_type": "Run",
    "type": "Run",
    "start_date": "2026-03-14T06:30:00Z",
    "elapsed_time": 2700,
    "moving_time": 2650,
    "distance": 8043.2,
    "has_heartrate": True,
    "average_heartrate": 152.0,
    "max_heartrate": 175.0,
    "average_watts": None,
    "max_watts": None,
    "weighted_average_watts": None,
    "device_watts": False,
    "average_speed": 3.03,
    "total_elevation_gain": 88.0,
    "kilojoules": None,
    "gear_id": "g7654321",
    "athlete": {"id": 42424242},
    "updated_at": "2026-03-14T08:00:00Z",
}

#: Detail responses add description, calories, device and suffer score, and our
#: adapter attaches the resolved gear display name.
STRAVA_DETAIL_ACTIVITY = {
    **STRAVA_LIST_ACTIVITY,
    "description": "Easy shakeout",
    "private_note": "felt flat",
    "calories": 612.0,
    "device_name": "Garmin Forerunner 965",
    "suffer_score": 74.0,
    "gear_name": "Nike Pegasus 41",
}

STRAVA_ACTIVITY_NO_ID = {"name": "No id", "start_date": "2026-03-14T06:30:00Z"}
STRAVA_ACTIVITY_NO_START = {"id": 7, "name": "No start"}

STRAVA_STREAMS = {
    "time": {"data": [0, 1, 2], "series_type": "distance"},
    "heartrate": {"data": [140, 141, 142]},
    "watts": {"data": [0, 0, 0]},
    "latlng": {"data": [[1.0, 2.0], [1.0, 2.0], [1.0, 2.0]]},
}

# ── Google Health ───────────────────────────────────────────────────

GOOGLE_HEALTH_EXERCISE = {
    "name": "users/me/dataTypes/exercise/dataPoints/abc123",
    "updateTime": "2026-03-14T20:00:00Z",
    "exercise": {
        "exerciseType": "WALKING",
        "displayName": "Evening Walk",
        "notes": "with the dog",
        "activeDuration": "3600s",
        "interval": {
            "startTime": "2026-03-14T18:00:00Z",
            "endTime": "2026-03-14T19:05:00Z",
        },
        "metricsSummary": {
            "averageHeartRate": 96,
            "maxHeartRate": 118,
            "calories": 210,
            "distanceMeters": 4200.0,
        },
    },
}

#: Some responses nest the exercise under "data" and use snake_case keys.
GOOGLE_HEALTH_EXERCISE_SNAKE = {
    "name": "users/me/dataTypes/exercise/dataPoints/snake1",
    "update_time": "2026-03-14T21:00:00Z",
    "data": {
        "exercise": {
            "exercise_type": "STRENGTH_TRAINING",
            "active_duration": 1800,
            "interval": {
                "start_time": "2026-03-14T19:30:00Z",
                "end_time": "2026-03-14T20:00:00Z",
            },
            "metrics_summary": {
                "average_heart_rate": 105,
                "energyBurned": {"value": 180},
            },
        }
    },
}

GOOGLE_HEALTH_BIKE_EXERCISE = {
    "name": "users/me/dataTypes/exercise/dataPoints/bike1",
    "exercise": {
        "exerciseType": "BIKING",
        "activeDuration": "3600s",
        "interval": {
            "startTime": "2026-03-14T17:05:00Z",
            "endTime": "2026-03-14T18:07:00Z",
        },
        "metricsSummary": {"averageHeartRate": 145},
    },
}

GOOGLE_HEALTH_EXERCISE_NO_START = {
    "name": "users/me/dataTypes/exercise/dataPoints/nostart",
    "exercise": {"exerciseType": "WALKING"},
}

GOOGLE_HEALTH_HR_POINTS = [
    {
        "timeInterval": {"startTime": "2026-03-14T18:00:30Z"},
        "heartRate": {"beatsPerMinute": 95},
    },
    {
        "timeInterval": {"startTime": "2026-03-14T18:00:00Z"},
        "heartRate": {"beatsPerMinute": 90},
    },
    {
        "interval": {"startTime": "2026-03-14T18:01:00Z"},
        "heart_rate": {"beats_per_minute": 101},
    },
    # Unusable: no heart rate value.
    {"timeInterval": {"startTime": "2026-03-14T18:02:00Z"}},
    # Unusable: no timestamp.
    {"heartRate": {"beatsPerMinute": 110}},
]
