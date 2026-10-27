# Withings

Withings gives **body** data to TrainingPulse: weight, fat percentage, and muscle mass. Withings uses OAuth. You connect it on the status page.

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

7. Set `athlete.timezone` to your time zone, for example `Europe/Berlin`. TrainingPulse uses it to put each weigh-in on the correct day.
8. Restart the stack:

   ```bash
   docker compose up -d
   ```

## Connect Withings

1. Open the status page at `http://your-host:8000/`.
2. Click **Connect** on the Withings card.
3. Log in to Withings and accept the access request.

Body data shows in the Grafana dashboards in the **Addons** folder.
