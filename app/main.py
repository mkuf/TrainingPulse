"""TrainingPulse — FastAPI application.

Provides:
- One connection flow per activity source (API key or OAuth)
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
from trainingpulse_common.config import redacted_dump

from config import config
from database import async_session, engine
from models import Activity, AthleteSettings, DailyMetrics
from plugins.registry import dispose_plugins, enabled_plugin_names, load_plugins, setup_plugins
from schema_migrate import ensure_schema
from sources.registry import build_all_sources
from sync import refresh_rate_limits, run_sync, sync_state

_PLUGIN_LINKS: dict[str, tuple[str, str]] = {
    "withings": ("Withings (body weight)", "/plugins/withings/"),
    "fddb": ("FDDB (nutrition)", "/plugins/fddb/"),
}

SOURCE_LABELS: dict[str, str] = {
    "icu": "intervals.icu",
    "strava": "Strava",
    "google_health": "Google Health",
    "hammerhead": "Hammerhead",
}

APP_DISPLAY_NAME = "TrainingPulse"

logging.basicConfig(
    level=getattr(logging, config.app.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

#: Built once at import so OAuth routes can be registered before startup.
#: Includes disabled sources so the status page can explain why they are off.
SOURCES = build_all_sources(config, async_session)


async def trigger_sync_task(force_resync: bool = False):
    """Background task: sync activities from enabled providers."""
    async with async_session() as session:
        await run_sync(session, force_resync=force_resync)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: migrate schema, start scheduler, load plugins."""
    logger.info("Effective configuration:\n%s", redacted_dump(config))

    async with engine.begin() as conn:
        await ensure_schema(conn)
    logger.info("Database tables created/verified")

    app.state.scheduler = scheduler
    plugins = load_plugins(config)
    app.state.plugins = await setup_plugins(app, scheduler, plugins)

    scheduler.add_job(
        trigger_sync_task,
        "interval",
        minutes=config.sync.interval_minutes,
        id="trainingpulse_sync",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    logger.info(
        "Scheduler started: syncing every %d minutes", config.sync.interval_minutes
    )

    yield

    scheduler.shutdown(wait=False)
    await dispose_plugins(app.state.plugins)
    for source in SOURCES:
        await source.close()
    await engine.dispose()


app = FastAPI(title=APP_DISPLAY_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Each source mounts its own authorization routes. API-key sources are no-ops.
# Paths are owned by the source so already-registered OAuth redirect URIs (in
# particular Google's /auth/google/callback) keep working unchanged.
for _source in SOURCES:
    _source.register_auth_routes(app)


# ── Status helpers ───────────────────────────────────────────────────


async def _progress_counts(session, athlete_id: int | None) -> dict:
    """Counts for the status page, over visible (non-superseded) activities."""
    empty = {
        "total": 0,
        "details_done": 0,
        "details_pending": 0,
        "streams_done": 0,
        "streams_pending": 0,
        "superseded": 0,
        "by_source": {},
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
            ).where(
                Activity.athlete_id == athlete_id,
                Activity.superseded_by == None,  # noqa: E711
            )
        )
    ).one()
    total, details_pending, streams_pending = row

    superseded = (
        await session.execute(
            select(func.count(Activity.id)).where(
                Activity.athlete_id == athlete_id,
                Activity.superseded_by != None,  # noqa: E711
            )
        )
    ).scalar_one()

    by_source: dict[str, int] = {}
    source_rows = (
        await session.execute(
            select(Activity.source, func.count(Activity.id))
            .where(
                Activity.athlete_id == athlete_id,
                Activity.superseded_by == None,  # noqa: E711
            )
            .group_by(Activity.source)
        )
    ).all()
    for source, count in source_rows:
        # Old rows may still carry the short-lived "fitbit" label.
        key = "google_health" if source == "fitbit" else (source or "unknown")
        by_source[key] = by_source.get(key, 0) + count

    return {
        "total": total,
        "details_done": max(0, total - details_pending),
        "details_pending": details_pending,
        "streams_done": max(0, total - streams_pending),
        "streams_pending": streams_pending,
        "superseded": int(superseded or 0),
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
    enabled = enabled_plugin_names(config)
    if not enabled:
        return ""
    items: list[str] = []
    for name in enabled:
        label, path = _PLUGIN_LINKS[name]
        if name in active_names:
            items.append(f'<li><a href="{path}">{label}</a></li>')
        else:
            items.append(
                f'<li>{label} <span class="fine-print">(not configured — see '
                "config.yaml)</span></li>"
            )
    return f"""
    <div class="card">
        <h3>Plugins</h3>
        <ul style="margin: 0.35rem 0 0 1.15rem; padding: 0;">
            {"".join(items)}
        </ul>
    </div>
    """


async def _source_states(session) -> list[dict]:
    """Enabled/configured/connected state for every known source."""
    states: list[dict] = []
    for source in SOURCES:
        section = config.sources.section(source.name)
        connected = False
        if section.enabled and source.is_configured():
            try:
                connected = await source.is_connected()
            except Exception as exc:  # noqa: BLE001 - status page must render
                logger.warning("%s connection check failed: %s", source.name, exc)
        states.append(
            {
                "name": source.name,
                "label": SOURCE_LABELS.get(source.name, source.name),
                "enabled": section.enabled,
                "configured": source.is_configured(),
                "connected": connected,
                "requires_authorization": source.requires_authorization,
                "connect_path": source.connect_path,
                "disconnect_path": source.disconnect_path,
            }
        )
    return states


def _sources_card_html(states: list[dict]) -> str:
    """One row per source, in precedence order."""
    rows: list[str] = []
    for state in states:
        action = ""
        if not state["enabled"]:
            status = '<span class="fine-print">Disabled</span>'
        elif not state["configured"]:
            status = '<span class="error">Missing credentials</span>'
        elif not state["requires_authorization"]:
            status = '<span class="ok">Configured</span>'
        elif state["connected"]:
            status = '<span class="ok">Connected</span>'
            action = f"""
        <form action="{state['disconnect_path']}" method="post" class="inline-form">
            <button type="submit" class="btn btn-neutral btn-sm">Disconnect</button>
        </form>
        """
        else:
            status = '<span class="warn">Not connected</span>'
            action = (
                f'<a href="{state["connect_path"]}" '
                'class="btn btn-neutral btn-sm">Connect</a>'
            )
        rows.append(
            f"""
        <div class="source-row">
            <span class="source-name">{state['label']}</span>
            <span class="source-meta">{status}{action}</span>
        </div>
        """
        )
    return f"""
    <div class="card sources-card">
        <h3>Sources</h3>
        <p class="fine-print">Listed highest precedence first. When two sources
        report the same workout, the higher one wins.</p>
        {"".join(rows)}
    </div>
    """


def _by_source_label(by_source: dict[str, int], superseded: int = 0) -> str:
    """Render per-source counts, e.g. "icu 812 · strava 40 · 37 hidden"."""
    if not by_source:
        return ""
    parts = [
        f"{SOURCE_LABELS.get(name, name)} {count}"
        for name, count in sorted(by_source.items(), key=lambda item: -item[1])
    ]
    if superseded:
        parts.append(f"{superseded} hidden duplicate{'s' if superseded != 1 else ''}")
    return f"({' · '.join(parts)})"


def _rate_limit_rows_html(states: list[dict]) -> str:
    """One quota row per usable source that reports a budget."""
    rows: list[str] = []
    for state in states:
        snapshot = sync_state.rate_limits.get(state["name"])
        label = state["label"]
        if snapshot is None or not snapshot.known:
            rows.append(
                f"""
            <tr>
                <td>{label} quota</td>
                <td class="fine-print" id="sync-rl-{state['name']}">not reported yet</td>
            </tr>
            """
            )
            continue
        rows.append(
            f"""
            <tr>
                <td>{label} (15-min)</td>
                <td id="sync-rl-{state['name']}-15min">{snapshot.used_15min}/{snapshot.limit_15min}</td>
            </tr>
            <tr>
                <td>{label} (daily)</td>
                <td id="sync-rl-{state['name']}-daily">{snapshot.used_daily}/{snapshot.limit_daily}</td>
            </tr>
            """
        )
    if not rows:
        return ""
    rows.append(
        f"""
            <tr>
                <td>Quota checked</td>
                <td>
                    <span id="sync-rl-checked">Never</span>
                    <button type="button" id="sync-rl-refresh" class="btn btn-neutral btn-sm" {"disabled" if sync_state.is_running else ""}>Refresh</button>
                </td>
            </tr>
        """
    )
    return "".join(rows)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Status page showing sync state and current metrics."""
    active_plugins = {p.name for p in getattr(request.app.state, "plugins", [])}
    async with async_session() as session:
        states = await _source_states(session)
        # A source is usable when it is enabled, configured, and (for OAuth)
        # authorized. API-key sources need no authorization step.
        usable = [
            state
            for state in states
            if state["enabled"]
            and state["configured"]
            and (state["connected"] or not state["requires_authorization"])
        ]
        athlete_id = await _primary_athlete_id(session)

        if not usable:
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
                    {_sources_card_html(states)}
                    """,
                )
            )

        counts = await _progress_counts(session, athlete_id)
        activity_count = counts["total"]

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
        provider_label = (
            f"({sync_state.active_source})" if sync_state.active_source else ""
        )
    else:
        status_label = "Idle"
        provider_label = ""

    details_row_style = "" if details_pending_initial else "display:none;"
    streams_row_style = "" if streams_pending_initial else "display:none;"

    by_source_initial = _by_source_label(counts["by_source"], counts["superseded"])
    rate_limit_rows = _rate_limit_rows_html(usable)

    sync_info = f"""
    <div class="card">
        <h3>Sync</h3>
        <table>
            <tr><td>Status</td><td><strong id="sync-phase">{status_label}</strong>
                <span class="rl-meta" id="sync-provider">{provider_label}</span></td></tr>
            <tr><td>Activities</td><td id="sync-db-count">
                {activity_count} total
                <span class="rl-meta" id="sync-by-source">{by_source_initial}</span>
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
                    provEl.textContent = (s.is_running && s.active_source) ? "(" + s.active_source + ")" : "";
                }}
                const lastEl = document.getElementById("sync-last");
                if (lastEl) lastEl.textContent = "Last sync " + fmtLast(s.last_sync);
                const by = s.activities_by_source || {{}};
                const dbEl = document.getElementById("sync-db-count");
                if (dbEl) {{
                    let total = 0;
                    const parts = [];
                    Object.keys(by).sort(function(a, b) {{ return by[b] - by[a]; }}).forEach(function(name) {{
                        total += by[name];
                        parts.push((s.source_labels && s.source_labels[name] ? s.source_labels[name] : name) + " " + by[name]);
                    }});
                    if (s.superseded) {{
                        parts.push(s.superseded + " hidden duplicate" + (s.superseded === 1 ? "" : "s"));
                    }}
                    const suffix = parts.length ? ' <span class="rl-meta" id="sync-by-source">(' + parts.join(" · ") + ")</span>" : "";
                    dbEl.innerHTML = total + " total" + suffix;
                }}
                setPendingRow("sync-details-row", "sync-details-text", "sync-details-bar",
                    s.details.fetched, s.details.pending, s.details.total);
                setPendingRow("sync-streams-row", "sync-streams-text", "sync-streams-bar",
                    s.streams.fetched, s.streams.pending, s.streams.total);
                let rlChecked = null;
                Object.keys(s.rate_limits || {{}}).forEach(function(name) {{
                    const rl = s.rate_limits[name];
                    if (!rl || !rl.known) return;
                    const el15 = document.getElementById("sync-rl-" + name + "-15min");
                    if (el15 && rl.fifteen_min) el15.textContent = rl.fifteen_min.used + "/" + rl.fifteen_min.limit;
                    const elD = document.getElementById("sync-rl-" + name + "-daily");
                    if (elD && rl.daily) elD.textContent = rl.daily.used + "/" + rl.daily.limit;
                    if (rl.last_checked_at && (!rlChecked || rl.last_checked_at > rlChecked)) {{
                        rlChecked = rl.last_checked_at;
                    }}
                }});
                const rlCheckedEl = document.getElementById("sync-rl-checked");
                if (rlCheckedEl) rlCheckedEl.textContent = fmtAgo(rlChecked);
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
            {_sources_card_html(states)}
            {sync_info}
            {_plugin_links_html(active_plugins)}
            """,
        )
    )


async def _build_status_payload() -> dict:
    async with async_session() as session:
        athlete_id = await _primary_athlete_id(session)
        counts = await _progress_counts(session, athlete_id)

    return {
        "is_running": sync_state.is_running,
        "phase": sync_state.phase,
        "active_source": sync_state.active_source,
        "activities_by_source": counts["by_source"],
        "source_labels": SOURCE_LABELS,
        "superseded": counts["superseded"],
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
        "rate_limits": sync_state.rate_limit_payload(),
        "last_error": sync_state.last_error,
        "last_sync": sync_state.last_sync.isoformat() if sync_state.last_sync else None,
    }


@app.get("/sync/status")
async def sync_status():
    return await _build_status_payload()


@app.post("/sync/refresh-rate-limit")
async def refresh_rate_limit():
    """Refresh each connected source's published quota headers."""
    if sync_state.is_running:
        return JSONResponse(
            status_code=409,
            content={"error": "A sync is already running; rate limits will update from that."},
        )
    if not config.sources.enabled_names():
        return JSONResponse(
            status_code=400,
            content={"error": "No activity source is enabled."},
        )
    try:
        await refresh_rate_limits()
    except Exception as e:  # noqa: BLE001 - surfaced to the status page
        logger.warning("Rate-limit refresh failed: %s", e)
        return JSONResponse(status_code=502, content={"error": str(e)})
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
