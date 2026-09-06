"""TrainingPulse — FastAPI application.

Provides:
- intervals.icu API-key connection + Google Health OAuth
- Status page showing sync progress and current metrics
- Background scheduler for periodic syncing
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from config import settings
from database import async_session, engine
from google_health_client import GoogleHealthClient
from intervals_client import IntervalsClient
from models import Activity, AthleteSettings, DailyMetrics, OAuthToken
from plugins.registry import dispose_plugins, enabled_plugin_names, load_plugins, setup_plugins
from schema_migrate import ensure_schema
from sync import _snapshot_rate_limits, run_sync, sync_state

_PLUGIN_LINKS: dict[str, tuple[str, str]] = {
    "withings": ("Withings (body weight)", "/plugins/withings/"),
    "fddb": ("FDDB (nutrition)", "/plugins/fddb/"),
}

APP_DISPLAY_NAME = "TrainingPulse"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def trigger_sync_task(force_resync: bool = False):
    """Background task: sync activities from enabled providers."""
    async with async_session() as session:
        await run_sync(session, force_resync=force_resync)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: migrate schema, start scheduler, load plugins."""
    async with engine.begin() as conn:
        await ensure_schema(conn)
    logger.info("Database tables created/verified")

    app.state.scheduler = scheduler
    plugins = load_plugins()
    app.state.plugins = await setup_plugins(app, scheduler, plugins)

    scheduler.add_job(
        trigger_sync_task,
        "interval",
        minutes=settings.SYNC_INTERVAL_MINUTES,
        id="trainingpulse_sync",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    logger.info(
        "Scheduler started: syncing every %d minutes", settings.SYNC_INTERVAL_MINUTES
    )

    yield

    scheduler.shutdown(wait=False)
    await dispose_plugins(app.state.plugins)
    await engine.dispose()


app = FastAPI(title=APP_DISPLAY_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Google Health OAuth ─────────────────────────────────────────────


@app.get("/auth/google")
async def auth_google():
    """Redirect to Google OAuth for Google Health."""
    if not settings.google_health_configured():
        return HTMLResponse(
            _page(
                "Google Health Not Configured",
                "<p class='error'>Set GOOGLE_HEALTH_CLIENT_ID and "
                "GOOGLE_HEALTH_CLIENT_SECRET in .env.</p>",
            ),
            status_code=400,
        )
    return RedirectResponse(GoogleHealthClient.authorization_url())


@app.get("/auth/google/callback")
async def auth_google_callback(code: str | None = None, error: str | None = None):
    """Handle Google OAuth callback."""
    if error:
        return HTMLResponse(
            _page("Authorization Failed", f"<p class='error'>Error: {error}</p>"),
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            _page(
                "Authorization Failed",
                "<p class='error'>No authorization code received.</p>",
            ),
            status_code=400,
        )

    try:
        payload = await GoogleHealthClient.exchange_code(code)
    except Exception as e:
        return HTMLResponse(
            _page(
                "Authorization Failed",
                f"<p class='error'>Token exchange failed: {e}</p>",
            ),
            status_code=400,
        )

    async with async_session() as session:
        client = GoogleHealthClient(session)
        await client.store_tokens(payload)
        await client.close()

    logger.info("Google Health connected")
    asyncio.create_task(trigger_sync_task())

    return HTMLResponse(
        _page(
            "Connected!",
            """
            <div class="card success">
                <h2>Google Health linked</h2>
                <p>Your Google account is linked. Activities will sync on the next run.</p>
                <p><a href="/" class="btn btn-neutral">View Status →</a></p>
            </div>
            """,
        )
    )


@app.post("/auth/google/disconnect")
async def auth_google_disconnect():
    async with async_session() as session:
        client = GoogleHealthClient(session)
        await client.disconnect()
        await client.close()
    return RedirectResponse("/", status_code=303)


# ── Status helpers ───────────────────────────────────────────────────


async def _progress_counts(session, athlete_id: int | None) -> dict:
    empty = {
        "total": 0,
        "details_done": 0,
        "details_pending": 0,
        "streams_done": 0,
        "streams_pending": 0,
        "by_source": {"icu": 0, "google_health": 0},
    }
    if athlete_id is None:
        return empty
    row = (
        await session.execute(
            select(
                func.count(Activity.id),
                func.count(Activity.id).filter(
                    Activity.detail_synced == False  # noqa: E712
                ),
                func.count(Activity.id).filter(
                    Activity.synced_streams == False  # noqa: E712
                ),
            ).where(Activity.athlete_id == athlete_id)
        )
    ).one()
    total, details_pending, streams_pending = row
    by_source = {"icu": 0, "google_health": 0, "strava": 0}
    source_rows = (
        await session.execute(
            select(Activity.source, func.count(Activity.id))
            .where(Activity.athlete_id == athlete_id)
            .group_by(Activity.source)
        )
    ).all()
    for source, count in source_rows:
        if source in ("google_health", "fitbit"):
            by_source["google_health"] += count
        elif source == "icu":
            by_source["icu"] += count
        elif source == "strava":
            by_source["strava"] += count
    return {
        "total": total,
        "details_done": max(0, total - details_pending),
        "details_pending": details_pending,
        "streams_done": max(0, total - streams_pending),
        "streams_pending": streams_pending,
        "by_source": by_source,
    }


async def _primary_athlete_id(session) -> int | None:
    result = await session.execute(select(AthleteSettings.athlete_id).limit(1))
    aid = result.scalar_one_or_none()
    if aid is not None:
        return aid
    result = await session.execute(select(Activity.athlete_id).limit(1))
    return result.scalar_one_or_none()


def _plugin_links_html(active_names: set[str]) -> str:
    enabled = enabled_plugin_names()
    if not enabled:
        return ""
    items: list[str] = []
    for name in enabled:
        label, path = _PLUGIN_LINKS[name]
        if name in active_names:
            items.append(f'<li><a href="{path}">{label}</a></li>')
        else:
            items.append(
                f'<li>{label} <span class="fine-print">(not configured — see .env)</span></li>'
            )
    return f"""
    <div class="card">
        <h3>Plugins</h3>
        <ul style="margin: 0.35rem 0 0 1.15rem; padding: 0;">
            {"".join(items)}
        </ul>
    </div>
    """


def _sources_card_html(*, icu_ok: bool, google_health_connected: bool) -> str:
    icu_status = (
        '<span class="ok">Configured</span>'
        if icu_ok
        else '<span class="error">Missing API key</span>'
    )
    if not settings.google_health_enabled():
        gh_status = '<span class="fine-print">Disabled</span>'
        gh_action = ""
    elif google_health_connected:
        gh_status = '<span class="ok">Connected</span>'
        gh_action = """
        <form action="/auth/google/disconnect" method="post" class="inline-form">
            <button type="submit" class="btn btn-neutral btn-sm">Disconnect</button>
        </form>
        """
    else:
        gh_status = '<span class="warn">Not connected</span>'
        gh_action = '<a href="/auth/google" class="btn btn-neutral btn-sm">Connect</a>'

    return f"""
    <div class="card sources-card">
        <h3>Sources</h3>
        <div class="source-row">
            <span class="source-name">intervals.icu</span>
            <span class="source-meta">{icu_status}</span>
        </div>
        <div class="source-row">
            <span class="source-name">Google Health</span>
            <span class="source-meta">{gh_status}{gh_action}</span>
        </div>
    </div>
    """


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Status page showing sync state and current metrics."""
    active_plugins = {p.name for p in getattr(request.app.state, "plugins", [])}
    async with async_session() as session:
        icu_ok = settings.icu_enabled()
        google_health_connected = False
        if settings.google_health_enabled():
            token = (
                await session.execute(
                    select(OAuthToken).where(OAuthToken.provider == "google_health").limit(1)
                )
            ).scalar_one_or_none()
            google_health_connected = token is not None

        any_source = icu_ok or google_health_connected
        athlete_id = await _primary_athlete_id(session)

        if not any_source:
            return HTMLResponse(
                _page(
                    "Setup Required",
                    f"""
                    <div class="page-header">
                        <img src="/static/assets/favicon.svg" width="40" height="40" alt="{APP_DISPLAY_NAME}" class="logo">
                        <div>
                            <h2>{APP_DISPLAY_NAME}</h2>
                        </div>
                    </div>
                    {_sources_card_html(icu_ok=icu_ok, google_health_connected=google_health_connected)}
                    """,
                )
            )

        counts = await _progress_counts(session, athlete_id)
        activity_count = counts["total"]
        by_source = counts["by_source"]

        latest_metrics = None
        if athlete_id is not None:
            latest_result = await session.execute(
                select(DailyMetrics)
                .where(DailyMetrics.athlete_id == athlete_id)
                .order_by(DailyMetrics.date.desc())
                .limit(1)
            )
            latest_metrics = latest_result.scalar_one_or_none()

    details_done_initial = counts["details_done"]
    details_total_initial = counts["total"]
    details_pending_initial = counts["details_pending"]
    streams_done_initial = counts["streams_done"]
    streams_total_initial = counts["total"]
    streams_pending_initial = counts["streams_pending"]
    last_sync_initial = (
        sync_state.last_sync.strftime("%Y-%m-%d %H:%M UTC")
        if sync_state.last_sync
        else "Never"
    )
    error_row_initial = (
        f"<tr id='sync-error-row'><td>Error</td><td class='error' id='sync-error'>{sync_state.last_error}</td></tr>"
        if sync_state.last_error
        else "<tr id='sync-error-row' style='display:none;'><td>Error</td><td class='error' id='sync-error'></td></tr>"
    )
    if sync_state.is_running and sync_state.phase != "idle":
        status_label = sync_state.phase
        provider_label = f"({sync_state.provider})" if sync_state.provider else ""
    else:
        status_label = "Idle"
        provider_label = ""

    details_row_style = "" if details_pending_initial else "display:none;"
    streams_row_style = "" if streams_pending_initial else "display:none;"

    rate_limit_rows = ""
    if icu_ok:
        rate_limit_rows = f"""
            <tr>
                <td>intervals.icu (15-min)</td>
                <td id="sync-rl-15min">{sync_state.rate_limit_15min_usage}/{sync_state.rate_limit_15min_limit}</td>
            </tr>
            <tr>
                <td>intervals.icu (daily)</td>
                <td id="sync-rl-daily">{sync_state.rate_limit_daily_usage}/{sync_state.rate_limit_daily_limit}</td>
            </tr>
            <tr>
                <td>Quota checked</td>
                <td>
                    <span id="sync-rl-checked">Never</span>
                    <button type="button" id="sync-rl-refresh" class="btn btn-neutral btn-sm" {"disabled" if sync_state.is_running else ""}>Refresh</button>
                </td>
            </tr>
        """

    sync_info = f"""
    <div class="card">
        <h3>Sync</h3>
        <table>
            <tr><td>Status</td><td><strong id="sync-phase">{status_label}</strong>
                <span class="rl-meta" id="sync-provider">{provider_label}</span></td></tr>
            <tr><td>Activities</td><td id="sync-db-count">
                {activity_count} total
                <span class="rl-meta" id="sync-by-source">(icu {by_source["icu"]} · google {by_source["google_health"]} · legacy strava {by_source.get("strava", 0)})</span>
            </td></tr>
            <tr id="sync-details-row" style="{details_row_style}">
                <td>Activity details</td>
                <td>
                    <span id="sync-details-text">{details_done_initial}/{details_total_initial}</span>
                    <progress id="sync-details-bar" value="{details_done_initial}" max="{max(details_total_initial, 1)}"></progress>
                </td>
            </tr>
            <tr id="sync-streams-row" style="{streams_row_style}">
                <td>Streams</td>
                <td>
                    <span id="sync-streams-text">{streams_done_initial}/{streams_total_initial}</span>
                    <progress id="sync-streams-bar" value="{streams_done_initial}" max="{max(streams_total_initial, 1)}"></progress>
                </td>
            </tr>
            {rate_limit_rows}
            {error_row_initial}
        </table>
        <div class="sync-actions">
            <form action="/sync/trigger" method="post" style="display:inline;">
                <button type="submit" class="btn btn-neutral" {"disabled" if sync_state.is_running else ""}>Sync Now</button>
            </form>
            <form action="/sync/full" method="post" style="display:inline;" onsubmit="return confirm('Full resync will re-fetch all data and may take a long time. Continue?');">
                <button type="submit" class="btn btn-caution" {"disabled" if sync_state.is_running else ""}>Full Resync</button>
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
        function setPendingRow(rowId, textId, barId, fetched, pending, total) {{
            const row = document.getElementById(rowId);
            if (row) row.style.display = pending > 0 ? "" : "none";
            if (pending > 0) setProgress(textId, barId, fetched, total);
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
        async function tick() {{
            try {{
                const res = await fetch("/sync/status", {{ cache: "no-store" }});
                if (!res.ok) return;
                const s = await res.json();
                const phaseEl = document.getElementById("sync-phase");
                if (phaseEl) {{
                    phaseEl.textContent = (s.is_running && s.phase !== "idle") ? s.phase : "Idle";
                }}
                const provEl = document.getElementById("sync-provider");
                if (provEl) {{
                    provEl.textContent = (s.is_running && s.provider) ? "(" + s.provider + ")" : "";
                }}
                const lastEl = document.getElementById("sync-last");
                if (lastEl) lastEl.textContent = "Last sync " + fmtLast(s.last_sync);
                const by = s.activities_by_source || {{}};
                const dbEl = document.getElementById("sync-db-count");
                if (dbEl) {{
                    const total = (by.icu || 0) + (by.google_health || 0) + (by.strava || 0);
                    dbEl.innerHTML = total + " total"
                        + ' <span class="rl-meta" id="sync-by-source">(icu ' + (by.icu || 0)
                        + " · google " + (by.google_health || 0) + " · legacy strava " + (by.strava || 0) + ")</span>";
                }}
                setPendingRow("sync-details-row", "sync-details-text", "sync-details-bar",
                    s.details.fetched, s.details.pending, s.details.total);
                setPendingRow("sync-streams-row", "sync-streams-text", "sync-streams-bar",
                    s.streams.fetched, s.streams.pending, s.streams.total);
                const rl15 = document.getElementById("sync-rl-15min");
                if (rl15 && s.rate_limit && s.rate_limit.fifteen_min) {{
                    rl15.textContent = s.rate_limit.fifteen_min.used + "/" + s.rate_limit.fifteen_min.limit;
                }}
                const rlD = document.getElementById("sync-rl-daily");
                if (rlD && s.rate_limit && s.rate_limit.daily) {{
                    rlD.textContent = s.rate_limit.daily.used + "/" + s.rate_limit.daily.limit;
                }}
                const rlChecked = document.getElementById("sync-rl-checked");
                if (rlChecked && s.rate_limit) rlChecked.textContent = fmtAgo(s.rate_limit.last_checked_at);
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
        tsb = latest_metrics.tsb
        if tsb > 15:
            form_label = "Fresh / Race Ready"
        elif tsb > 5:
            form_label = "Rested"
        elif tsb > -10:
            form_label = "Neutral"
        elif tsb > -25:
            form_label = "Training Hard"
        else:
            form_label = "Overreaching"

        metrics_info = f"""
        <div class="card">
            <h3>Fitness</h3>
            <div class="metrics-grid">
                <div class="metric">
                    <span class="metric-value">{latest_metrics.ctl:.1f}</span>
                    <span class="metric-label">CTL</span>
                </div>
                <div class="metric">
                    <span class="metric-value">{latest_metrics.atl:.1f}</span>
                    <span class="metric-label">ATL</span>
                </div>
                <div class="metric">
                    <span class="metric-value">{latest_metrics.tsb:.1f}</span>
                    <span class="metric-label">TSB</span>
                </div>
            </div>
            <p class="form-status">{form_label} · {latest_metrics.date}</p>
        </div>
        """

    return HTMLResponse(
        _page(
            APP_DISPLAY_NAME,
            f"""
            <div class="page-header">
                <img src="/static/assets/favicon.svg" width="40" height="40" alt="{APP_DISPLAY_NAME}" class="logo">
                <div>
                    <h2>{APP_DISPLAY_NAME}</h2>
                    <p class="header-meta" id="sync-last">Last sync {last_sync_initial}</p>
                </div>
            </div>
            {metrics_info}
            {_sources_card_html(icu_ok=icu_ok, google_health_connected=google_health_connected)}
            {sync_info}
            {_plugin_links_html(active_plugins)}
            """,
        )
    )


async def _build_status_payload() -> dict:
    async with async_session() as session:
        athlete_id = await _primary_athlete_id(session)
        counts = await _progress_counts(session, athlete_id)

    rl_last = sync_state.rate_limit_last_checked_at
    payload = {
        "is_running": sync_state.is_running,
        "phase": sync_state.phase,
        "provider": sync_state.provider,
        "activities_by_source": counts["by_source"],
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
        "last_error": sync_state.last_error,
        "last_sync": sync_state.last_sync.isoformat() if sync_state.last_sync else None,
    }
    if settings.icu_enabled():
        payload["rate_limit"] = {
            "fifteen_min": {
                "used": sync_state.rate_limit_15min_usage,
                "limit": sync_state.rate_limit_15min_limit,
            },
            "daily": {
                "used": sync_state.rate_limit_daily_usage,
                "limit": sync_state.rate_limit_daily_limit,
            },
            "last_checked_at": rl_last.isoformat() if rl_last else None,
        }
    else:
        payload["rate_limit"] = None
    return payload


@app.get("/sync/status")
async def sync_status():
    return await _build_status_payload()


@app.post("/sync/refresh-rate-limit")
async def refresh_rate_limit():
    """Probe intervals.icu athlete endpoint to refresh rate-limit headers."""
    if sync_state.is_running:
        return JSONResponse(
            status_code=409,
            content={"error": "A sync is already running; rate limits will update from that."},
        )
    if not settings.icu_enabled():
        return JSONResponse(
            status_code=400,
            content={"error": "intervals.icu is not configured."},
        )
    client = IntervalsClient()
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
    if sync_state.is_running:
        return RedirectResponse("/", status_code=303)
    asyncio.create_task(trigger_sync_task())
    return RedirectResponse("/", status_code=303)


@app.post("/sync/full")
async def full_resync():
    if sync_state.is_running:
        return RedirectResponse("/", status_code=303)
    asyncio.create_task(trigger_sync_task(force_resync=True))
    return RedirectResponse("/", status_code=303)


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/static/assets/favicon.svg">
    <title>{title} — {APP_DISPLAY_NAME}</title>
    <style>
        :root {{
            --accent: #22d3ee;
            --accent-hover: #06b6d4;
            --bg-page: #0f0f0f;
            --bg-card: #1a1a1a;
            --bg-elevated: #222;
            --border: #333;
            --text: #e0e0e0;
            --text-muted: #888;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-page);
            color: var(--text);
            padding: 1.5rem;
            max-width: 720px;
            margin: 0 auto;
            line-height: 1.5;
        }}
        h2 {{ color: var(--accent); margin: 0; font-size: 1.35rem; }}
        h3 {{ color: var(--accent); margin-bottom: 0.75rem; font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.04em; }}
        .page-header {{
            display: flex;
            align-items: center;
            gap: 0.85rem;
            margin-bottom: 1.25rem;
        }}
        .page-header .logo {{ border-radius: 8px; flex-shrink: 0; }}
        .header-meta {{
            color: var(--text-muted);
            font-size: 0.85rem;
            margin-top: 0.15rem;
        }}
        .card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1.15rem 1.25rem;
            margin-bottom: 1rem;
        }}
        .fine-print {{
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
        .sources-card .source-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            padding: 0.55rem 0;
            border-bottom: 1px solid #222;
        }}
        .sources-card .source-row:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .sources-card .source-row:first-of-type {{ padding-top: 0; }}
        .source-name {{ font-weight: 500; }}
        .source-meta {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: wrap;
            justify-content: flex-end;
        }}
        .inline-form {{ display: inline; }}
        table {{ width: 100%; border-collapse: collapse; }}
        td {{
            padding: 0.45rem 0;
            border-bottom: 1px solid #222;
            vertical-align: middle;
        }}
        td:first-child {{ color: var(--text-muted); width: 42%; }}
        code {{
            background: #2a2a2a;
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            font-size: 0.9rem;
        }}
        .btn {{
            display: inline-block;
            padding: 0.65rem 1.25rem;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            margin-top: 0;
            border: none;
            cursor: pointer;
            font: inherit;
            text-align: center;
        }}
        .btn-neutral {{
            background: #333;
            color: #fff;
        }}
        .btn-neutral:hover {{ background: #444; }}
        .btn-caution {{
            background: #ca8a04;
            color: #111;
        }}
        .btn-caution:hover {{ background: #a16207; color: #111; }}
        .btn[disabled] {{ opacity: 0.5; cursor: not-allowed; }}
        .btn-sm {{
            padding: 0.2rem 0.65rem;
            font-size: 0.8rem;
            font-weight: 500;
        }}
        .rl-meta {{
            color: var(--text-muted);
            font-size: 0.85rem;
            margin-left: 0.35rem;
        }}
        .sync-actions {{ margin-top: 1.15rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }}
        .error {{ color: #ef5350; }}
        .ok {{ color: #66bb6a; }}
        .warn {{ color: #ffa726; }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.75rem;
            margin: 0.25rem 0 0.5rem;
        }}
        .metric {{
            text-align: center;
            padding: 0.85rem 0.5rem;
            background: var(--bg-elevated);
            border-radius: 8px;
        }}
        .metric-value {{
            display: block;
            font-size: 1.75rem;
            font-weight: 700;
            color: var(--accent);
        }}
        .metric-label {{
            display: block;
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 0.2rem;
        }}
        .form-status {{
            text-align: center;
            font-size: 0.95rem;
            color: var(--text-muted);
            margin-top: 0.35rem;
        }}
        a {{ color: var(--accent); }}
        a:hover {{ color: var(--accent-hover); }}
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
            background: var(--accent);
            border-radius: 4px;
            transition: width 0.3s ease;
        }}
        progress::-moz-progress-bar {{
            background: var(--accent);
            border-radius: 4px;
        }}
    </style>
</head>
<body>
    {body}
</body>
</html>"""
