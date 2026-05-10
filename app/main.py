"""Strava Fitness Tracker — FastAPI application.

Provides:
- OAuth2 login flow with Strava
- Status page showing sync progress and current metrics
- Background scheduler for periodic syncing
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from database import async_session, engine
from models import Activity, ActivityStream, Base, DailyMetrics, StravaToken
from sync import run_sync, sync_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def scheduled_sync():
    """Background task: sync activities from Strava."""
    async with async_session() as session:
        await run_sync(session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: create tables, start scheduler."""
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified")

    # Start the scheduler
    scheduler.add_job(
        scheduled_sync,
        "interval",
        minutes=settings.SYNC_INTERVAL_MINUTES,
        id="strava_sync",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # Run immediately on startup
    )
    scheduler.start()
    logger.info(
        "Scheduler started: syncing every %d minutes", settings.SYNC_INTERVAL_MINUTES
    )

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    await engine.dispose()


app = FastAPI(title="Strava Fitness Tracker", lifespan=lifespan)


# ── OAuth endpoints ─────────────────────────────────────────────────


@app.get("/auth/strava")
async def auth_strava():
    """Redirect to Strava's OAuth authorization page."""
    params = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.oauth_callback_url,
        "approval_prompt": "auto",
        "scope": settings.STRAVA_SCOPES,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(f"{settings.STRAVA_AUTH_URL}?{query}")


@app.get("/auth/callback")
async def auth_callback(code: str | None = None, error: str | None = None):
    """Handle the OAuth callback from Strava."""
    if error:
        return HTMLResponse(
            _page("Authorization Failed", f"<p class='error'>Error: {error}</p>"),
            status_code=400,
        )

    if not code:
        return HTMLResponse(
            _page("Authorization Failed", "<p class='error'>No authorization code received.</p>"),
            status_code=400,
        )

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        response = await client.post(
            settings.STRAVA_TOKEN_URL,
            data={
                "client_id": settings.STRAVA_CLIENT_ID,
                "client_secret": settings.STRAVA_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
            },
        )

    if response.status_code != 200:
        return HTMLResponse(
            _page(
                "Authorization Failed",
                f"<p class='error'>Token exchange failed: {response.text}</p>",
            ),
            status_code=400,
        )

    data = response.json()
    athlete = data.get("athlete", {})
    athlete_id = athlete.get("id")

    # Store token in database
    async with async_session() as session:
        stmt = pg_insert(StravaToken).values(
            athlete_id=athlete_id,
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["athlete_id"],
            set_={
                "access_token": stmt.excluded.access_token,
                "refresh_token": stmt.excluded.refresh_token,
                "expires_at": stmt.excluded.expires_at,
            },
        )
        await session.execute(stmt)
        await session.commit()

    logger.info(
        "Authenticated athlete: %s %s (ID: %d)",
        athlete.get("firstname", ""),
        athlete.get("lastname", ""),
        athlete_id,
    )

    # Trigger an immediate sync
    asyncio.create_task(scheduled_sync())

    return HTMLResponse(
        _page(
            "Connected!",
            f"""
            <div class="card success">
                <h2>✅ Connected to Strava</h2>
                <p>Welcome, <strong>{athlete.get('firstname', '')} {athlete.get('lastname', '')}</strong>!</p>
                <p>Your activities are now being synced. This may take a while for the initial backfill.</p>
                <p><a href="/" class="btn">View Status →</a></p>
            </div>
            """,
        )
    )


# ── Status / Home page ──────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def home():
    """Status page showing sync state and current metrics."""
    async with async_session() as session:
        # Check if authenticated
        token_result = await session.execute(select(StravaToken).limit(1))
        token = token_result.scalar_one_or_none()

        if token is None:
            return HTMLResponse(
                _page(
                    "Setup Required",
                    f"""
                    <div class="card">
                        <h2>🚴 Strava Fitness Tracker</h2>
                        <p>Connect your Strava account to get started.</p>
                        <p>Make sure you have set the <strong>Authorization Callback Domain</strong>
                           in your <a href="https://www.strava.com/settings/api" target="_blank">
                           Strava API settings</a> to match your server hostname.</p>
                        <a href="/auth/strava" class="btn btn-strava">Connect with Strava</a>
                    </div>
                    """,
                )
            )

        # Get activity stats
        count_result = await session.execute(
            select(func.count(Activity.id)).where(
                Activity.athlete_id == token.athlete_id
            )
        )
        activity_count = count_result.scalar_one()

        processed_result = await session.execute(
            select(func.count(Activity.id)).where(
                Activity.athlete_id == token.athlete_id,
                Activity.synced_streams == True,  # noqa: E712
            )
        )
        processed_count = processed_result.scalar_one()

        # Get latest daily metrics
        latest_result = await session.execute(
            select(DailyMetrics)
            .where(DailyMetrics.athlete_id == token.athlete_id)
            .order_by(DailyMetrics.date.desc())
            .limit(1)
        )
        latest_metrics = latest_result.scalar_one_or_none()

    # Build status HTML
    sync_info = f"""
    <div class="card">
        <h3>🔄 Sync Status</h3>
        <table>
            <tr><td>Phase</td><td><strong>{sync_state.phase}</strong></td></tr>
            <tr><td>Running</td><td>{"🟢 Yes" if sync_state.is_running else "⚪ No"}</td></tr>
            <tr><td>Last sync</td><td>{sync_state.last_sync.strftime('%Y-%m-%d %H:%M UTC') if sync_state.last_sync else 'Never'}</td></tr>
            <tr><td>Activities in DB</td><td>{activity_count}</td></tr>
            <tr><td>Streams processed</td><td>{processed_count}/{activity_count}</td></tr>
            {"<tr><td>Error</td><td class='error'>" + sync_state.last_error + "</td></tr>" if sync_state.last_error else ""}
        </table>
    </div>
    """

    metrics_info = ""
    if latest_metrics:
        # Determine form status
        tsb = latest_metrics.tsb
        if tsb > 15:
            form_label = "🟢 Fresh / Race Ready"
        elif tsb > 5:
            form_label = "🔵 Rested"
        elif tsb > -10:
            form_label = "🟡 Neutral"
        elif tsb > -25:
            form_label = "🟠 Training Hard"
        else:
            form_label = "🔴 Overreaching"

        metrics_info = f"""
        <div class="card">
            <h3>📊 Current Metrics ({latest_metrics.date})</h3>
            <div class="metrics-grid">
                <div class="metric">
                    <span class="metric-value">{latest_metrics.ctl:.1f}</span>
                    <span class="metric-label">Fitness (CTL)</span>
                </div>
                <div class="metric">
                    <span class="metric-value">{latest_metrics.atl:.1f}</span>
                    <span class="metric-label">Fatigue (ATL)</span>
                </div>
                <div class="metric">
                    <span class="metric-value">{latest_metrics.tsb:.1f}</span>
                    <span class="metric-label">Form (TSB)</span>
                </div>
            </div>
            <p class="form-status">{form_label}</p>
        </div>
        """

    grafana_info = f"""
    <div class="card">
        <h3>📈 Grafana Setup</h3>
        <p>Add a PostgreSQL data source in Grafana with these settings:</p>
        <table>
            <tr><td>Host</td><td><code>strava-fitness-db:5432</code> (or your Docker network address)</td></tr>
            <tr><td>Database</td><td><code>strava_fitness</code></td></tr>
            <tr><td>User</td><td><code>strava</code></td></tr>
            <tr><td>Password</td><td><em>(your POSTGRES_PASSWORD)</em></td></tr>
            <tr><td>TLS/SSL</td><td>Disable</td></tr>
        </table>
    </div>
    """

    return HTMLResponse(
        _page(
            "Strava Fitness Tracker",
            f"""
            <h2>🚴 Strava Fitness Tracker</h2>
            {sync_info}
            {metrics_info}
            {grafana_info}
            """,
        )
    )


@app.get("/sync/status")
async def sync_status():
    """JSON endpoint for sync status (useful for monitoring)."""
    return {
        "is_running": sync_state.is_running,
        "phase": sync_state.phase,
        "total_activities": sync_state.total_activities,
        "synced_activities": sync_state.synced_activities,
        "streams_fetched": sync_state.streams_fetched,
        "last_error": sync_state.last_error,
        "last_sync": sync_state.last_sync.isoformat() if sync_state.last_sync else None,
    }


@app.post("/sync/trigger")
async def trigger_sync():
    """Manually trigger a sync."""
    if sync_state.is_running:
        return {"status": "already_running"}
    asyncio.create_task(scheduled_sync())
    return {"status": "started"}


# ── HTML template ───────────────────────────────────────────────────


def _page(title: str, body: str) -> str:
    """Minimal HTML page wrapper with inline CSS."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — Strava Fitness Tracker</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f0f0f;
            color: #e0e0e0;
            padding: 2rem;
            max-width: 800px;
            margin: 0 auto;
            line-height: 1.6;
        }}
        h2 {{ color: #fc4c02; margin-bottom: 1.5rem; }}
        h3 {{ color: #fc4c02; margin-bottom: 1rem; }}
        .card {{
            background: #1a1a1a;
            border: 1px solid #333;
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }}
        .card.success {{ border-color: #4caf50; }}
        table {{ width: 100%; border-collapse: collapse; }}
        td {{
            padding: 0.5rem 0;
            border-bottom: 1px solid #222;
        }}
        td:first-child {{ color: #888; width: 40%; }}
        code {{
            background: #2a2a2a;
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            font-size: 0.9rem;
        }}
        .btn {{
            display: inline-block;
            padding: 0.75rem 1.5rem;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            margin-top: 1rem;
        }}
        .btn-strava {{
            background: #fc4c02;
            color: white;
        }}
        .btn-strava:hover {{ background: #e04400; }}
        .btn {{ background: #333; color: #fff; }}
        .btn:hover {{ background: #444; }}
        .error {{ color: #ef5350; }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 1rem;
            margin: 1rem 0;
        }}
        .metric {{
            text-align: center;
            padding: 1rem;
            background: #222;
            border-radius: 8px;
        }}
        .metric-value {{
            display: block;
            font-size: 2rem;
            font-weight: 700;
            color: #fc4c02;
        }}
        .metric-label {{
            display: block;
            font-size: 0.85rem;
            color: #888;
            margin-top: 0.25rem;
        }}
        .form-status {{
            text-align: center;
            font-size: 1.1rem;
            margin-top: 0.5rem;
        }}
        a {{ color: #fc4c02; }}
    </style>
</head>
<body>
    {body}
</body>
</html>"""
