# Strava Fitness & Performance Tracker

A self-hosted Docker stack that syncs your Strava activities, calculates training load metrics, and visualizes everything in Grafana.

## What It Does

- **Syncs all activities** from your Strava account (full historical backfill + ongoing polling)
- **Metadata & Notes**: Syncs activity names, descriptions, and sport types for rich filtering.
- **Calculates Relative Effort (TRIMP)** from heart rate stream data.
- **Power Analysis**: Tracks Power (Watts) and calculates **Best 20-minute Power** and **Estimated FTP**.
- **Full Telemetry**: Persists raw streams for Heart Rate, Power, Cadence, Temperature, and Grade.
- **Tracks Fitness, Fatigue, and Form** (CTL/ATL/TSB) — the same "Fitness & Freshness" chart that requires a Strava subscription.
- **Handles OAuth automatically** — authenticates once, never expires.
- **Respects Strava rate limits** — sleeps when approaching limits, resumes on restart.

## Metrics Explained

| Metric | Also Known As | Description |
|--------|--------------|-------------|
| **TRIMP** | Relative Effort | Heart rate zone–weighted training load per activity |
| **CTL** | Fitness | 42-day exponential moving average of daily TRIMP |
| **ATL** | Fatigue | 7-day exponential moving average of daily TRIMP |
| **TSB** | Form | CTL − ATL. Positive = fresh, negative = fatigued |
| **Best 20m** | — | Highest average power sustained for 20 continuous minutes |
| **Est. FTP** | — | 95% of your best 20-minute power |

## Quick Start

### 1. Configure

```bash
cp .env.example .env
# Edit .env with your Strava Client ID and Secret
```

Get your Client ID and Secret from [strava.com/settings/api](https://www.strava.com/settings/api).

Set the **Authorization Callback Domain** to your server hostname (e.g., `localhost` or `myserver.local`).

### 2. Start

```bash
docker compose up -d
```

### 3. Authorize

Open `http://your-server:8000/` and click **Connect with Strava**.

The initial backfill will start automatically. Check progress at `http://your-server:8000/`.

### 4. Connect Grafana

Add a **PostgreSQL** data source in Grafana:

| Setting | Value |
|---------|-------|
| Host | `strava-fitness-db:5432` (adjust for your Docker network) |
| Database | `strava_fitness` |
| User | `strava` |
| Password | *(your POSTGRES_PASSWORD from .env)* |
| TLS/SSL | Disable |

Then import `grafana/dashboards/fitness.json`.

## Architecture

```
Strava API → [strava-fitness app] → PostgreSQL → Grafana
               (FastAPI + Python)
```

- **No Prometheus needed** — Grafana queries PostgreSQL directly
- Activities + HR streams stored in PostgreSQL
- Background scheduler polls Strava every 15 minutes
- Token auto-refresh ensures continuous operation

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Status page with current metrics |
| `GET /auth/strava` | Start Strava OAuth flow |
| `GET /sync/status` | JSON sync status (for monitoring) |
| `POST /sync/trigger` | Manually trigger a sync |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STRAVA_CLIENT_ID` | — | Your Strava API client ID |
| `STRAVA_CLIENT_SECRET` | — | Your Strava API client secret |
| `POSTGRES_USER` | `strava` | PostgreSQL username |
| `POSTGRES_PASSWORD` | `changeme` | PostgreSQL password |
| `POSTGRES_DB` | `strava_fitness` | PostgreSQL database name |
| `APP_BASE_URL` | `http://localhost:8000` | Public URL of the app (for OAuth callback) |
| `MAX_HR` | *(Strava)* | **Override** for Max HR. If set, ignores Strava zones. |
| `REST_HR` | *(Strava)* | **Override** for Resting HR. |
| `FTP` | *(Strava)* | **Override** for FTP. |
| `SYNC_INTERVAL_MINUTES` | `15` | Polling interval |

### Overriding Metrics
By default, the app fetches your **Max HR** and **FTP** from your Strava profile. However, if you want to manually set these (e.g., if Strava's auto-detected Max HR is incorrect), you can set `MAX_HR`, `REST_HR`, or `FTP` in your `.env` file.

**Note:** If you override `MAX_HR` or `REST_HR`, the app will ignore any custom heart rate zones from Strava and calculate consistent percentage-based zones instead.
