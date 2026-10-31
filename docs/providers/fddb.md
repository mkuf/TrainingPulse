# FDDB

FDDB gives **nutrition** data to TrainingPulse: daily calories and macronutrients. FDDB has no API. TrainingPulse reads your diary pages on fddb.info. You do not connect FDDB on the status page.

TrainingPulse needs three values: your user name, your password, and the `fddb` session cookie.

## Get the session cookie

1. Log in to <https://fddb.info> in your browser.
2. Open the developer tools of the browser.
3. Open the **Network** tab.
4. Load a page on fddb.info again.
5. Click a request to fddb.info.
6. In the request headers, find the cookie with the name `fddb`.
7. Copy its value. Do not copy the `fddb=` prefix.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.fddb` block.
3. Set `enabled` to `true`.
4. Set `user` to your FDDB user name.
5. Set `password` to your FDDB password. Alternatively, set the environment variable `FDDB_PW`.
6. Set `cookie` to the cookie value. Alternatively, set the environment variable `FDDB_COOKIE`.

```yaml
providers:
  fddb:
    enabled: true
    user: "your-name"
    password: ${FDDB_PW:-}
    cookie: ${FDDB_COOKIE:-}
    lang: de
    request_delay_ms: 300
```

7. Restart the stack:

   ```bash
   docker compose up -d
   ```

The sync starts automatically.

## If the sync stops

If FDDB shows its login page, the FDDB sync stops. The status page shows an authentication error. The cookie is possibly not valid now. Do the steps in [Get the session cookie](#get-the-session-cookie) again. Then restart the stack.

## Settings

| Key | Function |
|---|---|
| `lang` | Language of the diary pages |
| `request_delay_ms` | Time between two page requests, in milliseconds |

Nutrition data shows on the **Nutrition, training and weight** dashboard in Grafana.
