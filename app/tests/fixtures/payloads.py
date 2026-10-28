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
    "start_date": "2026-03-15T06:00:00Z",
    "start_date_local": "2026-03-14T23:00:00",
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
    "icu_joules": 306000,
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
    "name": "users/1557732547676114584/dataTypes/exercise/dataPoints/abc123",
    "dataSource": {"platform": "FITBIT", "device": {"displayName": "Fitbit Air"}},
    "exercise": {
        "exerciseType": "WALKING",
        "displayName": "Evening Walk",
        "notes": "with the dog",
        "activeDuration": "3600s",
        "interval": {
            "startTime": "2026-03-14T18:00:00Z",
            "startUtcOffset": "3600s",
            "endTime": "2026-03-14T19:05:00Z",
            "endUtcOffset": "3600s",
        },
        "metricsSummary": {
            "caloriesKcal": 210,
            "averageHeartRateBeatsPerMinute": "96",
            "distanceMillimeters": 4200000,
            "averageSpeedMillimetersPerSecond": 1166.7,
            "elevationGainMillimeters": 35000,
        },
        "exerciseMetadata": {"hasGps": True},
        "updateTime": "2026-03-14T20:00:00Z",
    },
    "updateTime": "2026-03-14T20:00:00Z",
}

#: A Strava session synced through Health Connect: the summary is empty.
GOOGLE_HEALTH_EXERCISE_HEALTH_CONNECT = {
    "name": "users/1557732547676114584/dataTypes/exercise/dataPoints/hc1",
    "dataSource": {
        "platform": "HEALTH_CONNECT",
        "application": {"packageName": "com.strava"},
    },
    "exercise": {
        "exerciseType": "STRENGTH_TRAINING",
        "displayName": "Kraft",
        "activeDuration": "1800s",
        "interval": {
            "startTime": "2026-03-14T19:30:00Z",
            "endTime": "2026-03-14T20:00:00Z",
        },
        "metricsSummary": {},
        "exerciseMetadata": {},
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
        "metricsSummary": {"averageHeartRateBeatsPerMinute": "145"},
    },
}

GOOGLE_HEALTH_EXERCISE_NO_START = {
    "name": "users/me/dataTypes/exercise/dataPoints/nostart",
    "exercise": {"exerciseType": "WALKING"},
}

GOOGLE_HEALTH_HR_POINTS = [
    {
        "heartRate": {
            "sampleTime": {
                "physicalTime": "2026-03-14T18:01:30Z",
                "utcOffset": "3600s",
            },
            "beatsPerMinute": "104",
        },
    },
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

# ── Hammerhead ──────────────────────────────────────────────────────

#: List-phase summaries carry no sport, HR, power, or elevation data at all —
#: only the detail-phase merge (with a downloaded and analyzed .fit file) adds
#: that, via the private "_fit_summary" key. As in the real API, ``duration``
#: is in milliseconds and ``createdAt`` is when the ride ended.
HAMMERHEAD_LIST_ACTIVITY = {
    "id": "1000.activity.abcd",
    "name": "My Epic Ride",
    "createdAt": "2026-03-14T18:08:00.000Z",
    "duration": 3720000,
    "distance": 41234.5,
}

HAMMERHEAD_DETAIL_ACTIVITY = {
    **HAMMERHEAD_LIST_ACTIVITY,
    "activityType": "MOUNTAIN_BIKE",
    "description": "Muddy but fun",
    "updatedAt": "2026-03-14T19:00:00.000Z",
    "_fit_summary": {
        "start_date": "2026-03-14T17:06:10Z",
        "elapsed_time": 3700,
        "moving_time": 3600,
        "distance": 41500.0,
        "average_heartrate": 148.2,
        "max_heartrate": 181.0,
        "has_heartrate": True,
        "average_watts": 212.4,
        "max_watts": 690.0,
        "device_watts": True,
        "weighted_average_watts": 228.9,
        "average_speed": 11.45,
        "total_elevation_gain": 312.0,
        "calories": 820.0,
        "device_name": "Hammerhead Karoo 3",
    },
}

HAMMERHEAD_ACTIVITY_NO_ID = {"name": "No id", "createdAt": "2026-03-14T17:05:00Z"}
HAMMERHEAD_ACTIVITY_NO_START = {"id": "1000.activity.nostart", "name": "No start"}

# ── Garmin ──────────────────────────────────────────────────────────

#: A Karoo ride imported into Garmin Connect, recorded from the Personal API
#: (personal fields dropped). The search payload carries float32 noise that
#: the detail payload has rounded away.
GARMIN_SEARCH_ACTIVITY = {
    "activityId": 24884964010,
    "activityName": "Radfahren",
    "startTimeLocal": "2026-10-24 14:12:22",
    "startTimeGMT": "2026-10-24 12:12:22",
    "activityType": {"typeId": 2, "typeKey": "cycling", "parentTypeId": 17},
    "distance": 30394.099609375,
    "duration": 3615.0,
    "elapsedDuration": 3862.0,
    "movingDuration": 3613.0,
    "elevationGain": 0.0,
    "averageSpeed": 8.407999992370605,
    "averageHR": 119.0,
    "maxHR": 130.0,
    "avgPower": 101.0,
    "maxPower": 269.0,
    "normPower": 104.0,
    "manufacturer": "HAMMERHEAD",
    "endTimeGMT": "2026-10-24 13:16:44",
}

GARMIN_DETAIL_ACTIVITY = {
    "activityId": 24884964010,
    "activityName": "Radfahren",
    "activityTypeDTO": {"typeId": 2, "typeKey": "cycling", "parentTypeId": 17},
    "metadataDTO": {"lastUpdateDate": "2026-10-28T18:35:03.0"},
    "summaryDTO": {
        "startTimeLocal": "2026-10-24T14:12:22.0",
        "startTimeGMT": "2026-10-24T12:12:22.0",
        "distance": 30394.1,
        "duration": 3615.0,
        "movingDuration": 3613.0,
        "elapsedDuration": 3862.0,
        "elevationGain": 0.0,
        "averageSpeed": 8.407999992370605,
        "averageHR": 119.0,
        "maxHR": 130.0,
        "averagePower": 101.0,
        "maxPower": 269.0,
        "normalizedPower": 104.0,
    },
}

#: What ``fitfiles`` derived from the same ride's FIT file: close to, but not
#: exactly, Garmin's own numbers. ``calories`` is invented (the ride has none)
#: to exercise filling a field the JSON lacks.
GARMIN_FIT_SUMMARY = {
    "start_date": "2026-10-24T12:12:23Z",
    "elapsed_time": 3857,
    "moving_time": 3615,
    "distance": 30384.0,
    "average_heartrate": 119.65550636413946,
    "max_heartrate": 130.0,
    "has_heartrate": True,
    "average_watts": 101.29468732706142,
    "max_watts": 269.0,
    "device_watts": True,
    "weighted_average_watts": 104.0,
    "average_speed": 7.9,
    "total_elevation_gain": None,
    "calories": 640.0,
    "device_name": "Hammerhead Karoo",
}

#: One ``allWeightMetrics`` entry: grams, epoch milliseconds. Field names and
#: units checked against a real Personal API response (a manual weigh-in);
#: ``date`` is local wall-clock time, ``timestampGMT`` true UTC.
GARMIN_WEIGHT_METRIC = {
    "samplePk": 1792562400123,
    "date": 1792569600000,
    "calendarDate": "2026-10-21",
    "weight": 81250.0,
    "bodyFat": 18.4,
    "muscleMass": 36100.0,
    "sourceType": "INDEX_SCALE",
    "timestampGMT": 1792562400000,
}

GOOGLE_HEALTH_TCX = """<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities><Activity Sport="Biking"><Lap><Track>
    <Trackpoint>
      <Time>2026-03-14T18:00:10Z</Time>
      <Position><LatitudeDegrees>48.1</LatitudeDegrees><LongitudeDegrees>11.5</LongitudeDegrees></Position>
      <AltitudeMeters>520.5</AltitudeMeters>
      <DistanceMeters>40</DistanceMeters>
    </Trackpoint>
    <Trackpoint>
      <Time>2026-03-14T18:00:00Z</Time>
      <Position><LatitudeDegrees>48.0</LatitudeDegrees><LongitudeDegrees>11.4</LongitudeDegrees></Position>
      <AltitudeMeters>520.0</AltitudeMeters>
      <DistanceMeters>0</DistanceMeters>
    </Trackpoint>
    <Trackpoint>
      <Time>2026-03-14T18:00:20Z</Time>
    </Trackpoint>
  </Track></Lap></Activity></Activities>
</TrainingCenterDatabase>
"""
