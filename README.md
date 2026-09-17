<p align="center">
  <img src="app/static/assets/favicon.svg" width="200" alt="TrainingPulse logo">
</p>

# TrainingPulse

Self-hosted sync and dashboards for your training: **intervals.icu**, **Strava**, **Google Health**, and **Hammerhead** (Karoo bike computers), PostgreSQL storage, training-load metrics (**TRIMP**, CTL, ATL, TSB, FTP estimates), and Grafana visualization.

## Trademarks and API use

**TrainingPulse is not affiliated with, endorsed by, or sponsored by intervals.icu, Strava, Google, or Hammerhead/SRAM.** Each person who runs this stack must use their own API credentials and comply with the respective terms of service. Activity data shown from Strava is **Powered by Strava**.

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
- Your max HR, resting HR, and FTP — these are **required** and are the only source of zone data (see [Athlete settings](#athlete-settings))
- At least one activity source:
  - An **intervals.icu** API key (Settings → Developer Settings)
  - A **Strava API application** (<https://www.strava.com/settings/api>), callback domain set to your app host
  - A **Google Cloud OAuth client** with the Google Health API enabled (redirect URI `{app.base_url}/auth/google/callback`)
  - A **Hammerhead API client** (<https://support.hammerhead.io/hc/en-us/sections/42738685226523-Public-API>), redirect URI set to exactly `{app.base_url}/auth/hammerhead/callback`

## Setup

1. **Write your configuration.** All application settings live in a single `config.yaml`:

   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

   Edit `config.yaml`: set the required `athlete` values, then enable the sources you want under `sources`. `.env` only carries Compose-level variables (the Postgres credentials and the published MCP port).

   `config.yaml` is gitignored. You can put credentials in it directly, or keep them in the environment and reference them as `${VAR}` — the example file shows both. Every key also has a `TP_SECTION__KEY` environment override, e.g. `TP_SYNC__RECONCILE_DAYS=7`.

   Startup fails with every problem listed at once if the file has an unknown key, a wrong type, a missing required value, or a source that is enabled without credentials.

2. **Start the stack.** From the repo root:

   ```bash
   docker compose up -d
   ```

   This brings up four containers: the FastAPI app on port `8000`, Grafana on port `3000`, the MCP service on port `8001`, and PostgreSQL (internal only).

3. **Connect sources.** Open the app at `http://your-host:8000/`. intervals.icu syncs automatically once its API key is set. Strava, Google Health, and Hammerhead use OAuth — click **Connect** next to each. Ongoing syncs run every 15 minutes.

   **Google OAuth tip:** set the consent screen publishing status to **In production** (you can leave the app unverified for personal use) so refresh tokens are not limited to 7 days. Testing mode requires reconnecting about weekly.

   **Strava rate limits:** Strava's default read budget is 100 requests per 15 minutes and 1000 per day — roughly twenty times tighter than intervals.icu. A full multi-year backfill including streams can take several days. It pauses and resumes automatically, and the status page shows the remaining budget.

   **Hammerhead:** every activity's `.fit` file is downloaded once and permanently stored — it is the only source of heart rate, power, cadence, and elevation data for Hammerhead-sourced activities, since the API's own JSON payload doesn't carry any of that.

4. **Open Grafana.** Browse to `http://your-host:3000/`. The Postgres datasource **`trainingpulse`** and the bundled dashboards are provisioned automatically.

5. **Optional: connect an MCP client.** The read-only MCP service is available at `http://your-host:8001/mcp`.

## Athlete settings

`athlete.max_hr`, `athlete.rest_hr`, and `athlete.ftp` are **required** and have no defaults. No provider is asked for a profile, so these values alone determine your heart-rate zones and power zones, and therefore TRIMP and all of CTL / ATL / TSB. Getting them wrong produces plausible-looking but incorrect numbers, which is why startup refuses to guess.

Heart-rate zones are computed from `max_hr` and `rest_hr` using the standard five-zone percentage model. After changing any of these values, use **Full Resync** on the status page to recalculate history.

## Multiple sources and duplicates

The same workout often lands in more than one service. `sources.precedence` lists sources highest-priority first (default `icu, strava, google_health, hammerhead`). When two activities start within 15 minutes of each other *and* their time ranges overlap, the higher-precedence one is shown and the others are kept but hidden, with `superseded_by` pointing at the winner. The `active_activities` view and every Grafana dashboard and MCP tool read only the visible rows, so nothing is double-counted.

Hidden activities do not spend API calls on detail or stream fetches. That is what keeps a tight-budget source like Strava from burning its quota on workouts you will never see.

Precedence is reversible: reorder `sources.precedence`, restart, and the next sync recomputes which rows are visible and fetches whatever the new winners are missing. No re-backfill is needed. Precedence is positional and independent of the `enabled` flag, so disabling a source does not silently promote another source's rows over its history.

## Optional plugins (Withings, FDDB)

TrainingPulse can load **in-process plugins** for body weight (Withings) and daily nutrition (FDDB). Plugin tables live in Postgres schemas `withings` and `fddb` inside the same database as core data. Addon dashboards appear in Grafana under the **Addons** folder.

1. Set `enabled: true` under `plugins.withings` or `plugins.fddb` in `config.yaml`.
2. Add that plugin's credentials in the same block — see `config.example.yaml`.
3. Restart: `docker compose up -d`

| Plugin | Setup UI | Partner callback (Withings) |
|--------|----------|---------------------------|
| Withings | `/plugins/withings/` | `{app.base_url}/plugins/withings/get_token` |
| FDDB | `/plugins/fddb/` | n/a (cookie auth) |

## What is stored

**From intervals.icu / Strava / Google Health / Hammerhead and stored locally:**

- Activity summaries (sport, time, distance, HR, power when available) with a `source` of `icu`, `strava`, `google_health`, or `hammerhead`
- Activity streams (HR, power, GPS when available) for maps and zone/TRIMP calculations
- Strava's "relative effort" as `suffer_score` (Strava only; NULL elsewhere)
- For Hammerhead: the raw `.fit` file itself, downloaded once per activity and permanently stored — it backs both the activity summary and its streams, since Hammerhead's API returns no HR/power/elevation data on its own
- OAuth tokens for Strava, Google Health, and Hammerhead (auto-refreshed); intervals.icu uses its configured API key
- Athlete settings, projected from `config.yaml` — **no provider profile, zone, or FTP data is ever requested**

**Computed locally:**

- TRIMP, HR/power zone time, estimated FTP, daily CTL / ATL / TSB

## Upgrading

Upgrading **preserves your activity history**. The schema is migrated in place: Strava-era numeric ids become `strava_<id>`, and a Strava-era `activity_streams` table is re-prefixed rather than dropped.

If you are coming from a release where Strava had been removed:

- The `suffer_score` column is re-added, and preserved `strava_<id>` rows are re-queued for a detail fetch to recover it. Their streams are left alone because the earlier migration kept them. Only rows that win on precedence are actually fetched.
- Strava OAuth now lives in the shared `oauth_tokens` table, so you need to reconnect Strava once.
- **Configuration is a breaking change.** All environment variables have been replaced by `config.yaml` plus the `TP_SECTION__KEY` override scheme. Copy `config.example.yaml` and migrate your `.env` values across; the old flat names (`INTERVALS_ICU_API_KEY`, `SYNC_RECONCILE_DAYS`, `ENABLED_PLUGINS`, …) are no longer read.
- **Heart-rate zones now come only from configuration.** If you previously relied on custom zones from intervals.icu, zones are now computed from `athlete.max_hr` and `athlete.rest_hr`. This changes TRIMP and therefore all historical CTL / ATL / TSB. Run **Full Resync** after checking your values.

For architecture, the HTTP API, the configuration schema, and metric formulas, see [`DEVELOPERS.md`](DEVELOPERS.md).
