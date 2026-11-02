# Withings

Withings gives these data kinds to TrainingPulse:

- **body** from a Withings scale: weight, fat percentage, muscle mass, bone mass, body water percentage, and visceral fat rating, if your scale measures them.
- **blood_pressure** from a Withings blood pressure monitor: systolic and diastolic pressure, and pulse.
- **sleep** from a Withings watch or sleep tracker: start, end, time asleep, deep, light, REM, and awake time, naps, and the sleep-stage timeline.
- **wellness**: steps, active and total calories, and respiration while asleep. Withings does not give resting heart rate, nightly HRV, daily SpO2, or skin temperature, so these stay empty.
- **intraday**: heart rate (2-minute averages), HRV, and SpO2.

Withings uses OAuth. You connect it on the status page. TrainingPulse asks only for the access that the imported data kinds need. To import only body data, set `imports: [body]`.

## Make the Partner Hub app

1. Log in to the Withings Partner Hub.
2. Make an app.
3. Set the callback URL. The URL must be exactly the same:

   ```text
   {app.base_url}/plugins/withings/get_token
   ```

   Partner Hub can send a `HEAD` request to this URL to examine it. TrainingPulse answers this request.

4. Copy the **Client ID** and the **Client Secret**.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.withings` block.
3. Set `enabled` to `true`.
4. Set `client_id` to the Client ID.
5. Set `client_secret` to the Client Secret. Alternatively, set the environment variable `WITHINGS_CLIENT_SECRET`.
6. Keep `redirect_uri` empty. Set a value only if Withings must use a URL that is different from `app.base_url`.

```yaml
providers:
  withings:
    enabled: true
    client_id: "abcd"
    client_secret: ${WITHINGS_CLIENT_SECRET:-}
    redirect_uri: ""
```

7. Optional: add `imports` to take only some data kinds, for example `imports: [body, sleep]`.
8. Set `athlete.timezone` to your time zone, for example `Europe/Berlin`. TrainingPulse uses it to put each weigh-in, day, and intraday value on the correct day. A night uses the time zone that Withings gives for it.
9. Restart the stack:

   ```bash
   docker compose up -d
   ```

## Connect Withings

1. Open the status page at `http://your-host:8000/`.
2. Click **Connect** on the Withings card.
3. Log in to Withings and accept the access request.

If you add a data kind to `imports` later, click **Connect** again to give the new access.

Body and blood pressure data show on the **Nutrition, training and weight** dashboard in Grafana. Body data is hidden until you switch on **Show weight**. Sleep, wellness, and intraday data show on the **Sleep and recovery** dashboard.

TrainingPulse does not import the Withings sleep score and other Withings indexes. It calculates its own sleep and recovery scores. Refer to [Sleep and recovery scores](../sleep-and-recovery.md). Without a resting heart rate and nightly HRV from Withings, the recovery score has fewer parts when Withings is the only provider.
