# Data and duplicates

## Providers and data kinds

A **provider** is an account that you connect. A provider gives one or more **data kinds**:

| Data kind | Providers |
|---|---|
| `activities` | intervals.icu, Strava, Google Health, Hammerhead |
| `body` | Withings |
| `nutrition` | FDDB |

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
- **Body and nutrition:** TrainingPulse compares each calendar day in `athlete.timezone`. The provider with the highest priority that has data on that day gives all data for that day.

A hidden row keeps `superseded_by`, which points to the row that TrainingPulse shows. The Grafana dashboards, the MCP tools, and the views `active_activities`, `active_body_measurements`, and `active_daily_nutrition` use only the rows that TrainingPulse shows. Thus, TrainingPulse does not count an item two times.

TrainingPulse does not fetch the detail and the streams of a hidden activity. Thus, a provider with a small budget, for example Strava, does not spend requests on activities that you do not see.

### Change the order

1. Change the order in `data.<kind>.precedence`.
2. Restart the stack:

   ```bash
   docker compose up -d
   ```

The next sync calculates again which rows show. It fetches the data that the new visible rows do not have. You do not have to download all history again.

> **Note:** The order does not change when you disable a provider. The history of a disabled provider keeps its position. Thus, the rows of a different provider do not replace it.

## Activities that a provider deletes

A provider can stop to give an activity. For example, you delete it there, or it is older than a retention limit. TrainingPulse then hides the activity. It does not delete it. The row, the streams, and the FIT file stay in the database, and `deleted_upstream_at` has a value. The activity does not hide the copies from other providers. If the provider lists the activity again, TrainingPulse shows it again.

## Stored data

**From your providers:**

- Activity summaries: sport, time, distance, heart rate, and power if available. The `source` column is `icu`, `strava`, `google_health`, or `hammerhead`.
- Activity streams: heart rate, power, and GPS if available. TrainingPulse uses them for maps, zones, and TRIMP.
- The Strava "relative effort", in `suffer_score`. Only Strava gives this value. For other providers, it is empty.
- For Hammerhead: the `.fit` file of each activity. TrainingPulse downloads it one time and keeps it permanently. Refer to [Hammerhead](providers/hammerhead.md).
- Activities that a provider deleted. TrainingPulse hides them but keeps them.
- Body measurements (weight, fat %, muscle mass) and daily nutrition totals, with the name of the provider.
- OAuth tokens for Strava, Google Health, Hammerhead, and Withings. TrainingPulse refreshes them automatically. intervals.icu and FDDB use the credentials in `config.yaml`.
- Athlete settings from `config.yaml`. TrainingPulse does not request a profile, zones, or FTP from a provider.

**Calculated by TrainingPulse:**

- TRIMP, time in heart-rate and power zones, estimated FTP, and daily CTL, ATL, and TSB.
