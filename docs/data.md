# Data and duplicates

## Providers and data kinds

A **provider** is an account that you connect. A provider gives one or more **data kinds**:

| Data kind | Providers |
|---|---|
| `activities` | intervals.icu, Strava, Google Health, Hammerhead, Garmin Connect |
| `body` | Withings, Garmin Connect |
| `nutrition` | FDDB |
| `sleep` | Garmin Connect, Google Health |
| `wellness` | Garmin Connect, Google Health |
| `intraday` | Garmin Connect, Google Health |

- `sleep`: one main sleep per night: start, end, time asleep, deep, light, REM, and awake time, naps of that day, and the sleep-stage timeline if the provider gives one.
- `wellness`: one row per day: resting heart rate, HRV, respiration while asleep, SpO2, skin temperature deviation, steps, and calories.
- `intraday`: values with a time stamp: heart rate (2-minute averages), HRV every 5 minutes while you sleep, and SpO2.

Heart-rate values have the same resolution for all providers, so you can compare them. SpO2 values keep the resolution of the device: Google Health gives one value per minute, Garmin gives hourly averages. Thus, the SpO2 curve of a night is only for display. To compare nights, use the daily SpO2 average and low in `wellness`.

TrainingPulse imports only these values, because most wearables give them. It does not import scores that only one provider calculates, for example the Garmin sleep score, stress, or Body Battery. These scores are different for each provider and cannot be compared. Instead, TrainingPulse calculates its own sleep score and recovery score from the values above. Refer to [Sleep and recovery scores](sleep-and-recovery.md).

Each data kind has one table, one set of dashboards, and one set of MCP tools. This is the same for all providers.

To import only some data kinds from a provider, add an `imports` list to its block in `config.yaml`. If you do not add `imports`, TrainingPulse imports all data kinds that the provider gives.

## Duplicates

One workout, weigh-in, or diary day is frequently in more than one service. TrainingPulse keeps the rows of all providers. It shows only one row for each item.

`data.<kind>.precedence` sets the order of the providers for each data kind. The first provider has the highest priority.

```yaml
data:
  activities:
    precedence: [icu, strava, google_health, hammerhead]
```

TrainingPulse uses these rules to find duplicates:

- **Activities:** two activities are duplicates if they start less than 15 minutes apart *and* their time ranges overlap.
- **All other data kinds:** TrainingPulse compares each calendar day in `athlete.timezone`. The provider with the highest priority that has data on that day gives all data for that day. A night belongs to the day on which it ends. Intraday values belong to the day of their time stamp.

A hidden row keeps `superseded_by`, which points to the row that TrainingPulse shows. The Grafana dashboards, the MCP tools, and the views `active_activities`, `active_body_measurements`, `active_daily_nutrition`, `active_sleep_sessions`, `active_daily_wellness`, and `active_wellness_samples` use only the rows that TrainingPulse shows. Thus, TrainingPulse does not count an item two times.

TrainingPulse does not fetch the detail and the streams of a hidden activity. Thus, a provider with a small budget, for example Strava, does not spend requests on activities that you do not see.

### Change the order

1. Change the order in `data.<kind>.precedence`.
2. Restart the stack:

   ```bash
   docker compose up -d
   ```

The next sync calculates again which rows show. It fetches the data that the new visible rows do not have. You do not have to download all history again.

> **Note:** The order does not change when you disable a provider. The history of a disabled provider keeps its position. Thus, the rows of a different provider do not replace it.

## Data that a provider deletes

Each sync lists the last `refresh_days` days again (see `data.<kind>.refresh_days`). When data from these days is not in the list anymore, TrainingPulse removes it. The first of these days is not checked, because providers do not agree on which day a record at the edge belongs to. Data older than `refresh_days` is never removed, because a provider can stop to give old data because of a retention limit.

**Activities** are hidden, not deleted. TrainingPulse also hides an activity when the provider answers that the activity does not exist. The row, the streams, and the FIT file stay in the database, and `deleted_upstream_at` has a value. The activity does not hide the copies from other providers. If the provider lists the activity again, TrainingPulse shows it again.

**Body, nutrition, sleep, wellness, and intraday data** are deleted. TrainingPulse can download them again. If a different provider has data for that day, that data shows.

## Stored data

**From your providers:**

- Activity summaries: sport, time, distance, heart rate, and power if available. The `source` column is `icu`, `strava`, `google_health`, or `hammerhead`.
- Activity streams: heart rate, power, and GPS if available. TrainingPulse uses them for maps, zones, and TRIMP.
- For Hammerhead: the `.fit` file of each activity. TrainingPulse downloads it one time and keeps it permanently. Refer to [Hammerhead](providers/hammerhead.md).
- Activities that a provider deleted. TrainingPulse hides them but keeps them.
- Body measurements (weight, fat %, muscle mass) and daily nutrition totals, with the name of the provider.
- Sleep, daily wellness values, and intraday values, with the name of the provider.
- OAuth tokens for Strava, Google Health, Hammerhead, and Withings. TrainingPulse refreshes them automatically. intervals.icu and FDDB use the credentials in `config.yaml`.
- Athlete settings from `config.yaml`. TrainingPulse does not request a profile, zones, or FTP from a provider. An imported resting heart rate replaces `athlete.rest_hr`. Refer to [Athlete settings](athlete-settings.md).

**Calculated by TrainingPulse:**

- TRIMP, time in heart-rate and power zones, normalized power, the power curve, estimated FTP, and daily CTL, ATL, and TSB.
- Values that a provider does not give, if the streams have them: maximum and average heart rate, maximum speed, and cadence.
