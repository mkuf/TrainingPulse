# Hammerhead

Hammerhead (Karoo bike computers) gives **activities** to TrainingPulse. Hammerhead uses OAuth. You connect it on the status page.

## Make the API client

1. Read the Hammerhead public API instructions at <https://support.hammerhead.io/hc/en-us/sections/42738685226523-Public-API>.
2. Make an API client as the instructions tell you.
3. Set the redirect URI. The URI must be exactly the same:

   ```text
   {app.base_url}/auth/hammerhead/callback
   ```

4. Copy the **Client ID** and the **Client Secret**.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.hammerhead` block.
3. Set `enabled` to `true`.
4. Set `client_id` to the Client ID.
5. Set `client_secret` to the Client Secret. Alternatively, set the environment variable `HAMMERHEAD_CLIENT_SECRET`.

```yaml
providers:
  hammerhead:
    enabled: true
    client_id: "abcd"
    client_secret: ${HAMMERHEAD_CLIENT_SECRET:-}
```

6. Restart the stack:

   ```bash
   docker compose up -d
   ```

## Connect Hammerhead

1. Open the status page at `http://your-host:8000/`.
2. Click **Connect** on the Hammerhead card.
3. Log in to Hammerhead and accept the access request.

## FIT files

The Hammerhead API does not give heart rate, power, cadence, or elevation. Only the `.fit` file of an activity contains these data. Thus, TrainingPulse downloads the `.fit` file of each activity one time. It keeps the file permanently in the database.

A Karoo can send a ride to the cloud some time after you record it. Thus, each sync also looks 14 days back.

## Limits

Hammerhead does not publish its rate limits. The Hammerhead card on the status page does not show a budget.
