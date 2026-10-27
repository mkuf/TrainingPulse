# Google Health

Google Health gives **activities** to TrainingPulse, with heart rate and GPS routes. Google Health uses OAuth. You connect it on the status page.

> **Note:** Google accepts only two types of redirect URI: `http://localhost`, or HTTPS on a public domain. Before you start, make sure that `app.base_url` is one of these types. If your server has a LAN name (for example `homeserver.lan` or `nas.local`), refer to [Use Google Health on a home server](#use-google-health-on-a-home-server) first.

## Make the OAuth client

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Make a project, or select an existing project.
3. Enable the **Google Health API** for the project.
4. Open **Google Auth Platform** and set the consent screen:
   1. Set the user type to **External**. If you have a Google Workspace account, you can set **Internal**. Refer to [Token lifetime](#token-lifetime).
   2. Keep the publishing status on **Testing**.
   3. Go to **Audience → Test users**.
   4. Add your Google account.
5. Add these scopes:
   - `googlehealth.activity_and_fitness.readonly`
   - `googlehealth.health_metrics_and_measurements.readonly`
   - `googlehealth.location.readonly` (for GPS routes)
6. Make an OAuth client of the type **Web application**.
7. Add this authorized redirect URI. The URI must be exactly the same:

   ```text
   {app.base_url}/auth/google/callback
   ```

8. Copy the **Client ID** and the **Client secret**.

## Set the configuration

1. Open `config.yaml`.
2. Find the `providers.google_health` block.
3. Set `enabled` to `true`.
4. Set `client_id` to the Client ID.
5. Set `client_secret` to the Client secret. Alternatively, set the environment variable `GOOGLE_HEALTH_CLIENT_SECRET`.

```yaml
providers:
  google_health:
    enabled: true
    client_id: "1234-abcd.apps.googleusercontent.com"
    client_secret: ${GOOGLE_HEALTH_CLIENT_SECRET:-}
```

6. Restart the stack:

   ```bash
   docker compose up -d
   ```

## Connect Google Health

1. Open the status page at `{app.base_url}/`.
2. Click **Connect** on the Google Health card.
3. Log in with the Google account that you added as a test user.
4. Accept all scopes.

## Token lifetime

> **Note:** The Google Health scopes are sensitive. Google does not let you set the publishing status to **In production** without an app verification.

In **Testing** mode, Google stops the refresh token after 7 days. Then the Google Health sync stops. To start it again, click **Connect** on the status page again.

If you have a Google Workspace account, set the user type to **Internal**. An internal app does not need a verification and has no 7-day limit.

## Use Google Health on a home server

Google does not accept a LAN name such as `homeserver.lan` or `nas.local`. The error message is "must end with a public top-level domain".

Your browser does the redirect, not Google. Thus, the URI must not be accessible from the internet. Google must only accept the name. Use one of these three procedures.

### Procedure A: Tailscale

`ts.net` is a public suffix. Google accepts it.

1. In Tailscale, enable **MagicDNS** and **HTTPS certificates**.
2. On the server, run:

   ```bash
   tailscale serve --bg 8000
   ```

3. Set `app.base_url` to `https://<server>.<tailnet>.ts.net`.

The app is accessible only from your tailnet.

### Procedure B: A domain that you own

1. Make a subdomain, for example `training.example.com`.
2. In your local DNS, point the subdomain to the LAN IP address of the server.
3. Put a reverse proxy (for example Caddy or Traefik) in front of port `8000`.
4. Set the proxy to get its certificate with a DNS-01 challenge.
5. Set `app.base_url` to `https://training.example.com`.

You do not open a port to the internet.

### Procedure C: SSH tunnel

Use this procedure only if `app.base_url` is `http://localhost:8000`.

1. On your computer, run:

   ```bash
   ssh -L 8000:localhost:8000 you@homeserver
   ```

2. Open `http://localhost:8000/`.
3. Click **Connect** on the Google Health card.

In **Testing** mode, you must do this procedure each week.

### After the procedure

Set the redirect URI in the Google Cloud Console to `{app.base_url}/auth/google/callback`. Use the new value of `app.base_url`.

## Limits

Google does not publish its quota. The Google Health card on the status page does not show a budget.
