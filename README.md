<p align="center">
  <img src="app/static/assets/favicon.svg" width="200" alt="TrainingPulse logo">
</p>

# TrainingPulse

Self-hosted sync and dashboards for your training: **intervals.icu** and **Google Health**, PostgreSQL storage, training-load metrics (**TRIMP**, CTL, ATL, TSB, FTP estimates), and Grafana visualization.

## Trademarks and API use

**TrainingPulse is not affiliated with, endorsed by, or sponsored by intervals.icu or Google.** Each person who runs this stack must use their own API credentials and comply with the respective terms of service.

## Disclaimer

This project is largely AI-generated. The numbers it computes — including training load, fitness, fatigue, form, and FTP estimates — are informational only and **are not medical advice**. Verify anything you plan to act on, and consult a qualified professional for health or training decisions.

## Grafana dashboards

Example views from the bundled dashboards.

<table>
<tr>
<td align="center" valign="top" width="25%">
<a href="screenshots/activity_detail.png"><img src="screenshots/activity_detail.png" width="200" alt="Activity detail dashboard"></a><br />
<sub>Activity detail</sub>
</td>
<td align="center" valign="top" width="25%">
<a href="screenshots/fitness.png"><img src="screenshots/fitness.png" width="200" alt="Fitness and Freshness dashboard"></a><br />
<sub>Fitness &amp; Freshness</sub>
</td>
<td align="center" valign="top" width="25%">
<a href="screenshots/account_overview.png"><img src="screenshots/account_overview.png" width="200" alt="Account overview dashboard"></a><br />
<sub>Account overview</sub>
</td>
<td align="center" valign="top" width="25%">
<a href="screenshots/nutrition_training_weight.png"><img src="screenshots/nutrition_training_weight.png" width="200" alt="Nutrition, training and weight dashboard"></a><br />
<sub>Nutrition &amp; weight</sub>
</td>
</tr>
</table>

## What you need

- Docker and Docker Compose
- An **intervals.icu** API key (Settings → Developer Settings)
- Optional: a **Google Cloud OAuth client** with Google Health API enabled (redirect URI `{APP_BASE_URL}/auth/google/callback`)

## Setup

1. **Configure your environment.** Copy the example file and fill in credentials:

   ```bash
   cp .env.example .env
   ```

   At minimum, set `INTERVALS_ICU_API_KEY`, `POSTGRES_PASSWORD`, and `APP_BASE_URL`. For Google Health, also set `GOOGLE_HEALTH_CLIENT_ID` / `GOOGLE_HEALTH_CLIENT_SECRET`.

2. **Start the stack.** From the repo root:

   ```bash
   docker compose up -d
   ```

   This brings up four containers: the FastAPI app on port `8000`, Grafana on port `3000`, the MCP service on port `8001`, and PostgreSQL (internal only).

3. **Connect sources.** Open the app at `http://your-host:8000/`. intervals.icu syncs automatically when the API key is set. For Google Health, click **Connect Google Health** to complete Google OAuth. Ongoing syncs run every 15 minutes.

   **Google OAuth tip:** set the consent screen publishing status to **In production** (you can leave the app unverified for personal use) so refresh tokens are not limited to 7 days. Testing mode requires reconnecting about weekly.

4. **Open Grafana.** Browse to `http://your-host:3000/`. The Postgres datasource **`trainingpulse`** and the bundled dashboards are provisioned automatically.

5. **Optional: connect an MCP client.** The read-only MCP service is available at `http://your-host:8001/mcp`.

## Optional plugins (Withings, FDDB)

TrainingPulse can load **in-process plugins** for body weight (Withings) and daily nutrition (FDDB). Plugin tables live in Postgres schemas `withings` and `fddb` inside the same database as core data. Addon dashboards appear in Grafana under the **Addons** folder.

1. Set `ENABLED_PLUGINS=withings,fddb` in `.env` (comma-separated; omit or leave empty for core-only).
2. Add plugin credentials (`WITHINGS_CLIENT_*`, or `FDDB_USER` / `FDDB_PW` / `FDDB_COOKIE` — see `.env.example`).
3. Restart: `docker compose up -d --build`

| Plugin | Setup UI | Partner callback (Withings) |
|--------|----------|---------------------------|
| Withings | `/plugins/withings/` | `{APP_BASE_URL}/plugins/withings/get_token` |
| FDDB | `/plugins/fddb/` | n/a (cookie auth) |

## What is stored

**From intervals.icu / Google Health and stored locally:**

- Activity summaries (sport, time, distance, HR, power when available) with a `source` of `icu` or `google_health`
- Activity streams (HR, power, GPS when available) for maps and zone/TRIMP calculations
- Athlete settings (HR zones, FTP) from intervals.icu sport-settings unless overridden in the environment
- Google OAuth tokens for Google Health (auto-refreshed); intervals.icu uses the API key from env only

**Computed locally:**

- TRIMP, HR/power zone time, estimated FTP, daily CTL / ATL / TSB

**Overlap:** Bike-like Google Health sessions are skipped when an intervals.icu activity already covers the same time window.

Out of the box Max HR is `190`, Resting HR is `60`, and FTP comes from intervals.icu (falling back to `200`). Override with `MAX_HR`, `REST_HR`, or `FTP` in `.env`.

Upgrading from the old Strava-based schema **wipes** local activities and re-syncs from the new providers (IDs are incompatible).

For architecture, the HTTP API, environment variables, and metric formulas, see [`DEVELOPERS.md`](DEVELOPERS.md).
