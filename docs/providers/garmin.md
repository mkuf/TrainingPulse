# Garmin Connect

Garmin Connect gives **activities**, **body measurements** (weight, fat %, muscle mass), **sleep**, **daily wellness**, and **intraday** values to TrainingPulse. TrainingPulse uses a Garmin Connect Personal API key. You do not connect Garmin on the status page.

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

## Sleep, wellness, and intraday data

TrainingPulse imports these data from Garmin:

| Data kind | Data | Requests |
|---|---|---|
| `sleep` | One main sleep per night: start and end, time asleep, deep, light, REM, and awake time, and naps of that day | 1 for each 28 days |
| `wellness` | One row per day: resting heart rate, HRV (night average), respiration while asleep, SpO2 (if Pulse Ox is on), steps, active and total calories | 3 for each 90 days |
| `intraday` | Heart rate every 2 minutes and HRV every 5 minutes while you sleep | 2 for each day |

TrainingPulse does not import the Garmin sleep score, stress, Body Battery, or HRV status. These values exist only at Garmin. TrainingPulse calculates its own sleep score and recovery score from the values above, the same way for every provider. Refer to [Sleep and recovery scores](../sleep-and-recovery.md).

Garmin puts a night on the day on which it ends. TrainingPulse uses `athlete.timezone` for Garmin's calendar days. Set it to the time zone of your Garmin account.

The `intraday` data kind needs many requests. Thus, the first sync gets only the last 30 days. To get more, increase `data.intraday.history_days`.

The imported resting heart rate replaces `athlete.rest_hr`. Refer to [Athlete settings](../athlete-settings.md#resting-heart-rate).

To import only some data kinds, add `imports`, for example `imports: [activities, sleep, wellness]`.

### Data that the Personal API does not give

The Garmin Connect app shows a sleep timeline with the sleep stages, respiration, and stress over the night. The Personal API does not give these timelines. It gives only the totals of each night. For a Garmin night, the **Sleep and recovery** dashboard shows the heart rate and HRV values, but no stage timeline.

Garmin has no skin temperature in the Personal API.

SpO2 values are empty if Pulse Ox is off on the device. To record SpO2 during sleep, turn on Pulse Ox in the device settings in Garmin Connect. This uses more battery.
