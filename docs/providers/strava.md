# Strava

Strava gives **activities** to TrainingPulse. Strava uses OAuth. You connect it on the status page.

## Make an API application

1. Log in to Strava.
2. Open <https://www.strava.com/settings/api>.
3. Make an API application.
4. Set **Authorization Callback Domain** to the host name of `app.base_url`. For example, if `app.base_url` is `https://training.example.com`, type `training.example.com`.
5. Copy the **Client ID** and the **Client Secret**.

The full callback URL is `{app.base_url}/auth/strava/callback`. TrainingPulse sends this URL to Strava. You do not type it.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.strava` block.
3. Set `enabled` to `true`.
4. Set `client_id` to the Client ID.
5. Set `client_secret` to the Client Secret. Alternatively, set the environment variable `STRAVA_CLIENT_SECRET`.

```yaml
providers:
  strava:
    enabled: true
    client_id: "12345"
    client_secret: ${STRAVA_CLIENT_SECRET:-}
```

6. Restart the stack:

   ```bash
   docker compose up -d
   ```

## Connect Strava

1. Open the status page at `http://your-host:8000/`.
2. Click **Connect** on the Strava card.
3. Log in to Strava and accept the access request.

Strava sends you back to the status page. The sync starts at the next interval.

## Limits

> **Note:** Strava lets TrainingPulse read 100 requests in 15 minutes and 1000 requests in one day. This is approximately 20 times less than intervals.icu.

A full history with streams can take some days. The sync stops when the budget is empty. It continues automatically when the budget is available again. The Strava card on the status page shows the remaining daily budget.

TrainingPulse does not fetch the detail and the streams of an activity that another provider shows. Thus, Strava spends its budget only on activities that you see. Refer to [Data and duplicates](../data.md).
