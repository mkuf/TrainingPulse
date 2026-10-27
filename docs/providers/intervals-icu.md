# intervals.icu

intervals.icu gives **activities** to TrainingPulse. TrainingPulse uses your personal API key. You do not connect intervals.icu on the status page.

## Get the API key

1. Log in to <https://intervals.icu>.
2. Open **Settings**.
3. Go to **Developer Settings**.
4. Copy the **API key**.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.icu` block.
3. Set `enabled` to `true`.
4. Set `api_key` to your API key.

   You can also keep the key in the environment. Use the value `${INTERVALS_ICU_API_KEY}` and set the variable `INTERVALS_ICU_API_KEY`.

```yaml
providers:
  icu:
    enabled: true
    api_key: ${INTERVALS_ICU_API_KEY}
```

5. Restart the stack:

   ```bash
   docker compose up -d
   ```

The sync starts automatically. The intervals.icu card on the status page shows the progress.

## Limits

intervals.icu lets TrainingPulse send approximately 2500 requests in 15 minutes and 5000 requests in one day. These limits are sufficient for a full history.

TrainingPulse does not read your zones, FTP, or sport settings from intervals.icu. It uses only the values in `athlete`. Refer to [Athlete settings](../athlete-settings.md).
