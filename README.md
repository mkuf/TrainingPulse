# Strava Fitness & Performance Tracker

A self-hosted Docker stack that syncs your Strava activities, calculates training load metrics, and visualizes everything in Grafana.

## What It Does

- **Syncs all activities** from your Strava account (full historical backfill + ongoing polling)
- **Calculates Relative Effort (TRIMP)** from heart rate stream data
- **Tracks Fitness, Fatigue, and Form** (CTL/ATL/TSB) — the same "Fitness & Freshness" chart that requires a Strava subscription
- **Handles OAuth automatically** — authenticates once, never expires
- **Respects Strava rate limits** — sleeps when approaching limits, resumes on restart

## Metrics Explained

| Metric | Also Known As | Description |
|--------|--------------|-------------|
| **TRIMP** | Relative Effort | Heart rate zone–weighted training load per activity |
| **CTL** | Fitness | 42-day exponential moving average of daily TRIMP |
| **ATL** | Fatigue | 7-day exponential moving average of daily TRIMP |
| **TSB** | Form | CTL − ATL. Positive = fresh, negative = fatigued |

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
| `DEFAULT_MAX_HR` | `190` | Fallback max HR if Strava zones unavailable |
| `DEFAULT_REST_HR` | `60` | Fallback resting HR |
| `SYNC_INTERVAL_MINUTES` | `15` | Polling interval |
