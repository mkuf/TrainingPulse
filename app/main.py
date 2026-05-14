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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from database import async_session, engine
from models import Activity, ActivityStream, Base, DailyMetrics, StravaToken
from strava_client import StravaClient
from sync import _snapshot_rate_limits, run_sync, sync_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def trigger_sync_task(force_resync: bool = False):
    """Background task: sync activities from Strava."""
    async with async_session() as session:
        await run_sync(session, force_resync=force_resync)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: create tables, start scheduler."""
    # Create all tables (schema migrations / DDL are applied manually outside the app)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified")

    # Start the scheduler
    scheduler.add_job(
        trigger_sync_task,
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

# Mount static files for assets and favicon
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    asyncio.create_task(trigger_sync_task())

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


def _seconds_until_next_quarter_hour() -> int:
    """Seconds until the next :00 / :15 / :30 / :45 UTC mark.

    Strava resets the 15-min read budget on these boundaries. Pure wall-clock
    math; does not hit the API.
    """
    now = datetime.now(timezone.utc)
    minutes_into_window = now.minute % 15
    seconds_into_window = minutes_into_window * 60 + now.second
    return max(0, 15 * 60 - seconds_into_window)


async def _progress_counts(session, athlete_id: int) -> dict:
    """One-shot DB query that returns cumulative sync progress for an athlete.

    Returns ``{total, details_done, details_pending, streams_done, streams_pending}``.
    Authoritative for the UI: ``sync_state`` in-memory counters are only used
    for ticking phase/rate-limit info.
    """
    row = (
        await session.execute(
            select(
                func.count(Activity.id),
                func.count(Activity.id).filter(
                    Activity.strava_detail_synced == False  # noqa: E712
                ),
                func.count(Activity.id).filter(
                    Activity.synced_streams == False  # noqa: E712
                ),
            ).where(Activity.athlete_id == athlete_id)
        )
    ).one()
    total, details_pending, streams_pending = row
    return {
        "total": total,
        "details_done": max(0, total - details_pending),
        "details_pending": details_pending,
        "streams_done": max(0, total - streams_pending),
        "streams_pending": streams_pending,
    }


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

        counts = await _progress_counts(session, token.athlete_id)
        activity_count = counts["total"]
        processed_count = counts["streams_done"]

        # Get latest daily metrics
        latest_result = await session.execute(
            select(DailyMetrics)
            .where(DailyMetrics.athlete_id == token.athlete_id)
            .order_by(DailyMetrics.date.desc())
            .limit(1)
        )
        latest_metrics = latest_result.scalar_one_or_none()

    # Initial values for the progress rows; the JS poller refreshes these every
    # few seconds. We render cumulative values from the DB so the bars don't
    # snap back to 0 between sync runs.
    list_synced_initial = activity_count
    list_total_initial = max(sync_state.total_activities, activity_count)
    details_done_initial = counts["details_done"]
    details_total_initial = counts["total"]
    streams_done_initial = counts["streams_done"]
    streams_total_initial = counts["total"]
    last_sync_initial = (
        sync_state.last_sync.strftime("%Y-%m-%d %H:%M UTC")
        if sync_state.last_sync
        else "Never"
    )
    running_label_initial = "🟢 Yes" if sync_state.is_running else "⚪ No"
    error_row_initial = (
        f"<tr id='sync-error-row'><td>Error</td><td class='error' id='sync-error'>{sync_state.last_error}</td></tr>"
        if sync_state.last_error
        else "<tr id='sync-error-row' style='display:none;'><td>Error</td><td class='error' id='sync-error'></td></tr>"
    )

    sync_info = f"""
    <div class="card">
        <h3>🔄 Sync Status</h3>
        <table>
            <tr><td>Phase</td><td><strong id="sync-phase">{sync_state.phase}</strong></td></tr>
            <tr><td>Running</td><td id="sync-running">{running_label_initial}</td></tr>
            <tr><td>Last sync</td><td id="sync-last">{last_sync_initial}</td></tr>
            <tr><td>Activities in DB</td><td id="sync-db-count">{activity_count}</td></tr>
            <tr>
                <td>List backfill</td>
                <td>
                    <span id="sync-list-text">{list_synced_initial}/{list_total_initial}</span>
                    <progress id="sync-list-bar" value="{list_synced_initial}" max="{max(list_total_initial, 1)}"></progress>
                </td>
            </tr>
            <tr>
                <td>Activity details</td>
                <td>
                    <span id="sync-details-text">{details_done_initial}/{details_total_initial}</span>
                    <progress id="sync-details-bar" value="{details_done_initial}" max="{max(details_total_initial, 1)}"></progress>
                </td>
            </tr>
            <tr>
                <td>Streams processed</td>
                <td>
                    <span id="sync-streams-text">{streams_done_initial}/{streams_total_initial}</span>
                    <progress id="sync-streams-bar" value="{streams_done_initial}" max="{max(streams_total_initial, 1)}"></progress>
                </td>
            </tr>
            <tr>
                <td>Rate limit (15-min)</td>
                <td>
                    <span id="sync-rl-15min">{sync_state.rate_limit_15min_usage}/{sync_state.rate_limit_15min_limit}</span>
                    <span class="rl-meta" id="sync-rl-resets"></span>
                </td>
            </tr>
            <tr>
                <td>Rate limit (daily)</td>
                <td id="sync-rl-daily">{sync_state.rate_limit_daily_usage}/{sync_state.rate_limit_daily_limit}</td>
            </tr>
            <tr>
                <td>Last checked</td>
                <td>
                    <span id="sync-rl-checked">Never</span>
                    <button type="button" id="sync-rl-refresh" class="btn btn-sm" {"disabled" if sync_state.is_running else ""}>Refresh</button>
                </td>
            </tr>
            {error_row_initial}
        </table>
        <div class="sync-actions">
            <form action="/sync/trigger" method="post" style="display:inline;">
                <button type="submit" class="btn" {"disabled" if sync_state.is_running else ""}>Sync Now</button>
            </form>
            <form action="/sync/full" method="post" style="display:inline;" onsubmit="return confirm('Full resync will re-fetch all data and may take a long time. Continue?');">
                <button type="submit" class="btn btn-warning" {"disabled" if sync_state.is_running else ""}>Full Resync</button>
            </form>
        </div>
    </div>
    <script>
    (function() {{
        const POLL_MS = 3000;
        function setProgress(textId, barId, fetched, total) {{
            const text = document.getElementById(textId);
            const bar = document.getElementById(barId);
            if (text) text.textContent = fetched + "/" + total;
            if (bar) {{
                bar.max = Math.max(total, 1);
                bar.value = fetched;
            }}
        }}
        function fmtLast(iso) {{
            if (!iso) return "Never";
            const d = new Date(iso);
            const pad = n => String(n).padStart(2, "0");
            return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate())
                + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + " UTC";
        }}
        function fmtAgo(iso) {{
            if (!iso) return "Never";
            const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
            if (secs < 10) return "just now";
            if (secs < 60) return secs + "s ago";
            const mins = Math.floor(secs / 60);
            if (mins < 60) return mins + " min ago";
            const hours = Math.floor(mins / 60);
            if (hours < 24) return hours + "h " + (mins % 60) + "m ago";
            return Math.floor(hours / 24) + "d ago";
        }}
        function fmtResetsIn(secs) {{
            if (secs == null) return "";
            const s = Math.max(0, Math.floor(secs));
            const m = Math.floor(s / 60);
            const r = s % 60;
            return "resets in " + m + "m " + (r < 10 ? "0" : "") + r + "s";
        }}
        async function tick() {{
            try {{
                const res = await fetch("/sync/status", {{ cache: "no-store" }});
                if (!res.ok) return;
                const s = await res.json();
                const phaseEl = document.getElementById("sync-phase");
                if (phaseEl) phaseEl.textContent = s.phase;
                const runEl = document.getElementById("sync-running");
                if (runEl) runEl.textContent = s.is_running ? "🟢 Yes" : "⚪ No";
                const lastEl = document.getElementById("sync-last");
                if (lastEl) lastEl.textContent = fmtLast(s.last_sync);
                setProgress("sync-list-text", "sync-list-bar", s.list.synced, s.list.total);
                setProgress("sync-details-text", "sync-details-bar", s.details.fetched, s.details.total);
                setProgress("sync-streams-text", "sync-streams-bar", s.streams.fetched, s.streams.total);
                const rl15 = document.getElementById("sync-rl-15min");
                if (rl15) rl15.textContent = s.rate_limit.fifteen_min.used + "/" + s.rate_limit.fifteen_min.limit;
                const rlD = document.getElementById("sync-rl-daily");
                if (rlD) rlD.textContent = s.rate_limit.daily.used + "/" + s.rate_limit.daily.limit;
                const rlResets = document.getElementById("sync-rl-resets");
                if (rlResets) rlResets.textContent = fmtResetsIn(s.rate_limit.fifteen_min_resets_in_seconds);
                const rlChecked = document.getElementById("sync-rl-checked");
                if (rlChecked) rlChecked.textContent = fmtAgo(s.rate_limit.last_checked_at);
                const rlBtn = document.getElementById("sync-rl-refresh");
                if (rlBtn) rlBtn.disabled = !!s.is_running;
                const errRow = document.getElementById("sync-error-row");
                const errCell = document.getElementById("sync-error");
                if (errRow && errCell) {{
                    if (s.last_error) {{
                        errCell.textContent = s.last_error;
                        errRow.style.display = "";
                    }} else {{
                        errCell.textContent = "";
                        errRow.style.display = "none";
                    }}
                }}
                // Reload once after a sync finishes so the metrics card refreshes too.
                if (window.__wasRunning && !s.is_running) {{
                    window.location.reload();
                    return;
                }}
                window.__wasRunning = s.is_running;
            }} catch (e) {{ /* swallow */ }}
        }}
        const rlBtn = document.getElementById("sync-rl-refresh");
        if (rlBtn) {{
            rlBtn.addEventListener("click", async function() {{
                if (rlBtn.disabled) return;
                rlBtn.disabled = true;
                const prev = rlBtn.textContent;
                rlBtn.textContent = "Refreshing...";
                try {{
                    await fetch("/sync/refresh-rate-limit", {{ method: "POST", cache: "no-store" }});
                }} catch (e) {{ /* swallow */ }}
                rlBtn.textContent = prev;
                await tick();
            }});
        }}
        tick();
        setInterval(tick, POLL_MS);
    }})();
    </script>
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
            <div style="display: flex; align-items: center; gap: 1rem; margin-bottom: 2rem;">
                <img src="/static/assets/favicon.png" width="48" height="48" alt="Logo" style="border-radius: 8px;">
                <h2 style="margin: 0;">Strava Fitness Tracker</h2>
            </div>
            {sync_info}
            {metrics_info}
            {grafana_info}
            """,
        )
    )


async def _build_status_payload() -> dict:
    """Compose the JSON used by both ``/sync/status`` and the refresh endpoint."""
    async with async_session() as session:
        token = (await session.execute(select(StravaToken).limit(1))).scalar_one_or_none()
        if token is None:
            counts = {
                "total": 0,
                "details_done": 0,
                "details_pending": 0,
                "streams_done": 0,
                "streams_pending": 0,
            }
        else:
            counts = await _progress_counts(session, token.athlete_id)

    # List backfill: cumulative "synced" is the DB count; "total" is the DB
    # count except during an active backfill where the in-memory total may be
    # transiently larger as pages are fetched.
    list_total = max(counts["total"], sync_state.total_activities)

    rl_last = sync_state.rate_limit_last_checked_at
    return {
        "is_running": sync_state.is_running,
        "phase": sync_state.phase,
        "list": {
            "synced": counts["total"],
            "total": list_total,
        },
        "details": {
            "fetched": counts["details_done"],
            "pending": counts["details_pending"],
            "total": counts["total"],
        },
        "streams": {
            "fetched": counts["streams_done"],
            "pending": counts["streams_pending"],
            "total": counts["total"],
        },
        "rate_limit": {
            "fifteen_min": {
                "used": sync_state.rate_limit_15min_usage,
                "limit": sync_state.rate_limit_15min_limit,
            },
            "daily": {
                "used": sync_state.rate_limit_daily_usage,
                "limit": sync_state.rate_limit_daily_limit,
            },
            "last_checked_at": rl_last.isoformat() if rl_last else None,
            "fifteen_min_resets_in_seconds": _seconds_until_next_quarter_hour(),
        },
        "last_error": sync_state.last_error,
        "last_sync": sync_state.last_sync.isoformat() if sync_state.last_sync else None,
    }


@app.get("/sync/status")
async def sync_status():
    """JSON endpoint for sync status (consumed by the home-page poller).

    Progress counts come from the DB so they reflect cumulative state across
    sync runs (e.g. clicking "Sync Now" doesn't reset the bars to 0). Phase,
    running flag, rate-limit usage, and last error come from ``sync_state``.
    """
    return await _build_status_payload()


@app.post("/sync/refresh-rate-limit")
async def refresh_rate_limit():
    """Make exactly one lightweight Strava call to refresh rate-limit headers.

    Costs 1 read against the user's quota. Returns 409 if a sync is already
    in progress (since that sync is updating the numbers anyway).
    """
    if sync_state.is_running:
        return JSONResponse(
            status_code=409,
            content={"error": "A sync is already running; rate limits will update from that."},
        )
    async with async_session() as session:
        token = (await session.execute(select(StravaToken).limit(1))).scalar_one_or_none()
        if token is None:
            return JSONResponse(
                status_code=400,
                content={"error": "Not authenticated. Connect with Strava first."},
            )
        client = StravaClient(session)
        try:
            await client.get_athlete()
            _snapshot_rate_limits(client)
        except Exception as e:
            logger.warning("Rate-limit refresh failed: %s", e)
            return JSONResponse(status_code=502, content={"error": str(e)})
        finally:
            await client.close()
    return await _build_status_payload()


@app.post("/sync/trigger")
async def trigger_sync():
    """Manually trigger a sync."""
    if sync_state.is_running:
        return RedirectResponse("/", status_code=303)
    asyncio.create_task(trigger_sync_task())
    return RedirectResponse("/", status_code=303)


@app.post("/sync/full")
async def full_resync():
    """Manually trigger a full historical resync."""
    if sync_state.is_running:
        return RedirectResponse("/", status_code=303)
    asyncio.create_task(trigger_sync_task(force_resync=True))
    return RedirectResponse("/", status_code=303)


# ── HTML template ───────────────────────────────────────────────────


def _page(title: str, body: str) -> str:
    """Minimal HTML page wrapper with inline CSS."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/png" href="/static/assets/favicon.png">
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
        .btn-warning {{ background: #ff9800; color: #000; }}
        .btn-warning:hover {{ background: #f57c00; }}
        .btn[disabled] {{ opacity: 0.5; cursor: not-allowed; }}
        .btn-sm {{
            padding: 0.25rem 0.75rem;
            font-size: 0.8rem;
            margin-top: 0;
            margin-left: 0.5rem;
            font-weight: 500;
        }}
        .rl-meta {{
            color: #888;
            font-size: 0.85rem;
            margin-left: 0.5rem;
        }}
        .sync-actions {{ margin-top: 1.5rem; display: flex; gap: 0.5rem; }}
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
        progress {{
            display: block;
            width: 100%;
            height: 8px;
            margin-top: 0.25rem;
            border: none;
            background: #2a2a2a;
            border-radius: 4px;
            overflow: hidden;
            -webkit-appearance: none;
            appearance: none;
        }}
        progress::-webkit-progress-bar {{
            background: #2a2a2a;
            border-radius: 4px;
        }}
        progress::-webkit-progress-value {{
            background: #fc4c02;
            border-radius: 4px;
            transition: width 0.3s ease;
        }}
        progress::-moz-progress-bar {{
            background: #fc4c02;
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    {body}
</body>
</html>"""
