# Garmin Connect

Garmin Connect gives **activities** and **body measurements** (weight, fat %, muscle mass) to TrainingPulse. TrainingPulse uses a Garmin Connect Personal API key. You do not connect Garmin on the status page.

> **Note:** This provider is a first version. Garmin does not publish response schemas for the Personal API. Some field names are taken from the Garmin Connect web app and are not yet checked against a real account.

## Make the API key

1. Log in to <https://connect.garmin.com> in a web browser.
2. Open <https://connect.garmin.com/app/settings/personalApiToken>.
3. Click **Create Key** and accept the terms.
4. Select an expiration: 30 days, 90 days, 180 days, or one year.
5. Copy the key. Garmin shows the key only one time.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.garmin` block.
3. Set `enabled` to `true`.
4. Set `api_key` to the key. Alternatively, set the environment variable `GARMIN_PERSONAL_API_KEY`.

```yaml
providers:
  garmin:
    enabled: true
    api_key: ${GARMIN_PERSONAL_API_KEY:-}
```

To import only activities, add `imports: [activities]`.

5. Restart the stack:

   ```bash
   docker compose up -d
   ```

The sync starts automatically. The Garmin Connect card on the status page shows the progress.

## Key expiry

The key expires on the date that you selected. Garmin sends an email approximately one week before. When the key expires, the sync stops with an error. Make a new key and replace `api_key`.

TrainingPulse exchanges the key for an access token that is valid for 24 hours. It gets a new token when necessary. You do not have to do anything.

## FIT files

TrainingPulse downloads the original `.fit` file of each activity one time. It keeps the file permanently in the database. Heart rate, power, and the streams come from this file. Garmin removes some proprietary values from the file, for example VO2max and recovery time.

## Limits

Garmin does not publish rate limits for the Personal API. The Garmin Connect card on the status page does not show a budget.

## Health data

Garmin also has sleep, HRV, stress, Body Battery, resting heart rate, and SpO2 data. TrainingPulse does not import these data yet.
