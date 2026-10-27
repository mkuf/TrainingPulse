# Upgrading

An upgrade keeps your activity history. TrainingPulse changes the database schema when it starts. It does all changes in one transaction. If a change fails, the database does not change.

## Upgrade the stack

1. Make a backup of the database.
2. Get the new release.
3. Read the sections below that apply to your old release.
4. Do the steps in these sections.
5. Start the stack:

   ```bash
   docker compose up -d --build
   ```

## From a release without Strava

In these releases, TrainingPulse did not have Strava.

1. Change your configuration to `config.yaml`. Refer to [Change to config.yaml](#change-to-configyaml).
2. Start the stack.
3. Open the status page.
4. Click **Connect** on the Strava card. You must connect Strava one time again, because the Strava tokens moved to a different table.

TrainingPulse adds the `suffer_score` column again. It fetches the detail of the old `strava_<id>` rows again to get this value. It fetches only the rows that show. It does not fetch the streams again, because they are still in the database.

Old numeric Strava activity IDs change to `strava_<id>`.

## Change to config.yaml

> **Caution:** This change is not compatible with old releases. TrainingPulse does not read the old environment variables (for example `INTERVALS_ICU_API_KEY`, `SYNC_RECONCILE_DAYS`, or `ENABLED_PLUGINS`) as settings.

1. Copy the example file:

   ```bash
   cp config.example.yaml config.yaml
   ```

2. Move each value from `.env` to `config.yaml`.
3. Keep only the Postgres credentials and `MCP_PORT` in `.env`.
4. Add `athlete.max_hr`, `athlete.rest_hr`, and `athlete.ftp`. Old releases used default values (190, 60, 200). These defaults are gone. If the values are missing, the app does not start.

You can also set an environment variable `TP_<SECTION>__<KEY>` for each key. For example, `TP_ATHLETE__MAX_HR=185`.

## From the Withings or FDDB plugins

TrainingPulse moves the plugin data into the tables `body_measurements` and `daily_nutrition` when it starts. Then it deletes the old `withings` and `fddb` schemas if they are empty. The Withings token also moves. You do not have to connect Withings again. The Withings callback URL does not change.

1. In `config.yaml`, move `plugins.withings` to `providers.withings`.
2. Move `plugins.fddb` to `providers.fddb`.
3. Remove `sync_interval_minutes` and `sync_lookback_days`. One sync runs all providers. To set the history length, use `data.body.history_days` and `data.nutrition.history_days`.
4. Set `athlete.timezone`, for example `Europe/Berlin`. The default is `UTC`. TrainingPulse uses it to put each weigh-in on the correct day.
5. Start the stack. If an old key is in the file, the log shows the key and its new location.

The pages `/plugins/withings/` and `/plugins/fddb/` are gone. The two providers show on the status page.

### Heart-rate zones

> **Caution:** TrainingPulse now uses only the zones from `config.yaml`. If you used custom zones from intervals.icu, your zones change. This changes TRIMP and all history of CTL, ATL, and TSB.

1. Make sure that `athlete.max_hr` and `athlete.rest_hr` are correct.
2. Open the status page.
3. Click **Full resync**.
