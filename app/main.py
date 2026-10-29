"""TrainingPulse — FastAPI application.

Provides:
- One connection flow per provider (API key, session, or OAuth)
- Status page showing accounts, sync progress, and where each data kind comes from
- Background scheduler for periodic syncing
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select, true

from catalog import KINDS, PROVIDERS, AuthMethod, DataKind
from config import config
from config_loader import redacted_dump
from database import async_session, engine
from ingest.records import RECORD_KINDS, STORES
from models import ACTIVITY_VISIBLE, Activity, AthleteSettings
from providers.registry import build_all_providers
from resting_hr import resting_hr_on
from schema_migrate import ensure_schema
from sync import run_sync, sync_state

PROVIDER_LABELS: dict[str, str] = {name: spec.label for name, spec in PROVIDERS.items()}

APP_DISPLAY_NAME = "TrainingPulse"

logging.basicConfig(
    level=getattr(logging, config.app.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
SYNC_JOB_ID = "trainingpulse_sync"

#: Built once at import so OAuth routes can be registered before startup.
#: Includes disabled providers so the status page can explain why they are off.
PROVIDER_INSTANCES = build_all_providers(config, async_session)


async def trigger_sync_task(force_resync: bool = False):
    """Background task: sync every enabled provider."""
    async with async_session() as session:
        await run_sync(session, force_resync=force_resync)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: migrate schema, start scheduler."""
    logger.info("Effective configuration:\n%s", redacted_dump(config))

    async with engine.begin() as conn:
        await ensure_schema(conn, timezone=config.athlete.timezone)
    logger.info("Database tables created/verified")

    scheduler.add_job(
        trigger_sync_task,
        "interval",
        minutes=config.sync.interval_minutes,
        id=SYNC_JOB_ID,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    logger.info(
        "Scheduler started: syncing every %d minutes", config.sync.interval_minutes
    )

    yield

    scheduler.shutdown(wait=False)
    for provider in PROVIDER_INSTANCES:
        await provider.close()
    await engine.dispose()


app = FastAPI(title=APP_DISPLAY_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters["num"] = lambda value: f"{value:,}"
#: Appended to static asset URLs so browsers pick up new CSS, JS, and logos
#: after an upgrade instead of serving cached copies.
templates.env.globals["asset_version"] = str(int(datetime.now(timezone.utc).timestamp()))

# Each provider mounts its own authorization routes; non-OAuth ones are no-ops.
# Paths are owned by the provider so already-registered OAuth redirect URIs (in
# particular Google's /auth/google/callback and Withings'
# /plugins/withings/get_token) keep working unchanged.
for _provider in PROVIDER_INSTANCES:
    _provider.register_auth_routes(app)




# ── Status helpers ───────────────────────────────────────────────────

#: How a count of each kind reads, singular and plural.
KIND_UNITS: dict[DataKind, tuple[str, str]] = {
    DataKind.ACTIVITIES: ("activity", "activities"),
    DataKind.BODY: ("measurement", "measurements"),
    DataKind.NUTRITION: ("day", "days"),
    DataKind.SLEEP: ("night", "nights"),
    DataKind.WELLNESS: ("day", "days"),
    # Sample counts mean nothing to a reader; days with samples do.
    DataKind.INTRADAY: ("day", "days"),
}

SHOWN_KINDS: tuple[DataKind, ...] = (DataKind.ACTIVITIES, *RECORD_KINDS)


def _grafana_url() -> str:
    """Configured Grafana URL, or base_url's host on Grafana's default port."""
    if config.app.grafana_url:
        return config.app.grafana_url
    parts = urlsplit(config.app.base_url)
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme or 'http'}://{host}:3000"


def _count_label(kind: DataKind, count: int) -> str:
    singular, plural = KIND_UNITS[kind]
    return f"{count:,} {singular if count == 1 else plural}"


def _ago(when: datetime | None) -> str:
    if when is None:
        return "never"
    secs = max(0, int((datetime.now(timezone.utc) - when).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    days = secs // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def _until(when: datetime | None) -> str:
    if when is None:
        return ""
    secs = int((when - datetime.now(timezone.utc)).total_seconds())
    if secs < 60:
        return "in under a minute"
    if secs < 3600:
        return f"in {secs // 60} min"
    return f"in {secs // 3600} h {secs % 3600 // 60} min"


def _local_time(when: datetime | None) -> str:
    if when is None:
        return ""
    local = when.astimezone(ZoneInfo(config.athlete.timezone))
    return local.strftime("%a %d %b, %H:%M")


async def _primary_athlete_id(session) -> int | None:
    result = await session.execute(select(AthleteSettings.athlete_id).limit(1))
    aid = result.scalar_one_or_none()
    if aid is not None:
        return aid
    result = await session.execute(select(Activity.athlete_id).limit(1))
    return result.scalar_one_or_none()


async def _kind_counts(session, athlete_id: int | None) -> dict[DataKind, dict[str, dict]]:
    """Visible and hidden-duplicate rows per provider, for every shown kind."""
    tables = [
        (DataKind.ACTIVITIES, Activity, Activity.source, Activity.athlete_id == athlete_id),
        *(
            (kind, STORES[kind].model, STORES[kind].model.provider, true())
            for kind in RECORD_KINDS
        ),
    ]
    counts: dict[DataKind, dict[str, dict]] = {}
    for kind, model, provider_column, scope in tables:
        by_provider: dict[str, dict] = {}
        if kind is DataKind.ACTIVITIES and athlete_id is None:
            counts[kind] = by_provider
            continue
        # Intraday rows are thousands of samples a day; count days instead.
        unit = distinct(model.day) if kind is DataKind.INTRADAY else model.id
        rows = (
            await session.execute(
                select(
                    provider_column,
                    func.count(unit).filter(model.superseded_by == None),  # noqa: E711
                    func.count(unit).filter(model.superseded_by != None),  # noqa: E711
                )
                .where(scope)
                .group_by(provider_column)
            )
        ).all()
        for provider, visible, hidden in rows:
            # Old rows may still carry the short-lived "fitbit" label.
            key = "google_health" if provider == "fitbit" else (provider or "unknown")
            entry = by_provider.setdefault(key, {"visible": 0, "hidden": 0})
            entry["visible"] += int(visible)
            entry["hidden"] += int(hidden)
        counts[kind] = by_provider
    return counts


async def _kind_since(session, athlete_id: int | None) -> dict[DataKind, date]:
    """The earliest visible entry of each shown kind, as a local day."""
    since: dict[DataKind, date] = {}
    if athlete_id is not None:
        first = await session.scalar(
            select(func.min(Activity.start_date)).where(
                ACTIVITY_VISIBLE, Activity.athlete_id == athlete_id
            )
        )
        if first is not None:
            since[DataKind.ACTIVITIES] = first.astimezone(
                ZoneInfo(config.athlete.timezone)
            ).date()
    for kind in RECORD_KINDS:
        model = STORES[kind].model
        first = await session.scalar(
            select(func.min(model.day)).where(model.superseded_by == None)  # noqa: E711
        )
        if first is not None:
            since[kind] = first
    return since


async def _pending_counts(session, athlete_id: int | None) -> dict:
    """Visible activities still waiting for their detail or stream fetch."""
    if athlete_id is None:
        return {"total": 0, "details_pending": 0, "streams_pending": 0}
    total, details_pending, streams_pending = (
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
                ACTIVITY_VISIBLE,
            )
        )
    ).one()
    return {
        "total": total,
        "details_pending": details_pending,
        "streams_pending": streams_pending,
    }


async def _ungranted_imports(provider, name: str) -> list[DataKind]:
    """Imported kinds the stored credentials cannot read (missing scopes)."""
    try:
        granted = await provider.granted_kinds()
    except Exception as exc:  # noqa: BLE001 - status page must render
        logger.warning("%s granted-kinds check failed: %s", name, exc)
        return []
    return [kind for kind in config.providers.imports_of(name) if kind not in granted]


async def _provider_views(counts: dict[DataKind, dict[str, dict]]) -> list[dict]:
    """One card per known provider: its state, what it supplied, its quota."""
    views: list[dict] = []
    for provider in PROVIDER_INSTANCES:
        name = provider.name
        label = PROVIDER_LABELS.get(name, name)
        section = config.providers.section(name)
        configured = provider.is_configured()
        oauth = provider.auth is AuthMethod.OAUTH

        connected = False
        if section.enabled and configured and oauth:
            try:
                connected = await provider.is_connected()
            except Exception as exc:  # noqa: BLE001 - status page must render
                logger.warning("%s connection check failed: %s", name, exc)

        if not section.enabled:
            status, status_label, tone = "off", "Off", "muted"
            hint = f"Turned off. Set providers.{name}.enabled to true in config.yaml."
        elif not configured:
            status, status_label, tone = "missing", "Needs setup", "danger"
            hint = f"Add its credentials under providers.{name} in config.yaml."
        elif not oauth:
            status, status_label, tone = "ready", "Ready", "ok"
            hint = (
                "Uses the API key from config.yaml."
                if provider.auth is AuthMethod.API_KEY
                else "Signs in with the login from config.yaml."
            )
        elif connected:
            missing = await _ungranted_imports(provider, name)
            if missing:
                status, status_label, tone = "connected", "Reconnect needed", "warn"
                hint = (
                    f"Disconnect and connect {label} again to allow "
                    f"{', '.join(KINDS[kind].label.lower() for kind in missing)}."
                )
            else:
                status, status_label, tone = "connected", "Connected", "ok"
                hint = f"You gave TrainingPulse access to your {label} account."
        else:
            status, status_label, tone = "disconnected", "Not connected", "warn"
            hint = f"Connect your {label} account to start syncing."

        # Amounts, including hidden duplicates, live in "Where your data
        # comes from"; the card is about the connection only.
        usable = status in ("ready", "connected")

        quota = None
        snapshot = sync_state.rate_limits.get(name)
        if usable and snapshot is not None and snapshot.known and snapshot.limit_daily:
            pct = min(100, round(100 * snapshot.used_daily / snapshot.limit_daily))
            quota = {
                "daily_used": snapshot.used_daily,
                "daily_limit": snapshot.limit_daily,
                "fifteen_used": snapshot.used_15min,
                "fifteen_limit": snapshot.limit_15min,
                "pct": pct,
                "high": pct >= 80,
                "checked": _ago(snapshot.checked_at),
            }

        views.append(
            {
                "name": name,
                "label": label,
                "status": status,
                "status_label": status_label,
                "tone": tone,
                "hint": hint,
                "quota": quota,
                "usable": usable,
                "connect_path": provider.connect_path,
                "disconnect_path": provider.disconnect_path,
            }
        )
    # Things that need your attention first, switched-off providers last.
    order = {"missing": 0, "disconnected": 0, "connected": 1, "ready": 1, "off": 2}
    views.sort(key=lambda view: order[view["status"]])
    return views


def _kind_views(
    counts: dict[DataKind, dict[str, dict]], since: dict[DataKind, date]
) -> list[dict]:
    """Each kind's providers in precedence order, with what each one shows."""
    views: list[dict] = []
    for kind in SHOWN_KINDS:
        by_provider = counts.get(kind, {})
        active = config.active_providers(kind)
        # Rows from a provider that is off now still count until replaced.
        leftover = [
            name for name, entry in by_provider.items()
            if entry["visible"] and name not in active
        ]
        if not active and not leftover:
            continue
        ranking = config.data.kind(kind).precedence
        leftover.sort(key=lambda name: ranking.index(name) if name in ranking else len(ranking))
        chain = [
            {
                "label": PROVIDER_LABELS.get(name, name),
                "count": by_provider.get(name, {}).get("visible", 0),
                "hidden": by_provider.get(name, {}).get("hidden", 0),
                "off": name not in active,
            }
            for name in active + leftover
        ]
        total = sum(entry["visible"] for entry in by_provider.values())
        total_label = _count_label(kind, total) if total else "No data yet"
        if total and kind in since:
            total_label += f" · since {since[kind].strftime('%b %Y')}"
        views.append(
            {
                "label": KINDS[kind].label,
                "total_label": total_label,
                "chain": chain,
            }
        )
    return views


def _phase_label(phase: str, source: str) -> str:
    """What the running sync is doing, in words."""
    who = PROVIDER_LABELS.get(source, source)
    verb, _, what = phase.partition(" ")
    try:
        what = KINDS[DataKind(what)].label.lower()
    except ValueError:
        pass
    labels = {
        "starting": "Starting sync",
        "backfilling": f"Downloading your {who} {what or 'activity'} history",
        "incremental": f"Checking {who} for new activities",
        "reconciling": f"Checking {who} for edited activities",
        "detailing": f"Fetching activity details from {who}",
        "streaming": f"Fetching heart rate and power data from {who}",
        "syncing": f"Syncing {what} from {who}",
        "deduplicating": "Matching duplicates across accounts",
        "finalizing": "Updating fitness and training load",
    }
    return labels.get(verb, phase.capitalize())


def _sync_view(pending: dict) -> dict:
    """Headline, detail line, and catch-up progress for the status bar."""
    job = scheduler.get_job(SYNC_JOB_ID)
    next_run = job.next_run_time if job is not None else None
    catching_up = bool(pending["details_pending"] or pending["streams_pending"])
    error = sync_state.last_error
    rate_limited = bool(error) and "rate limit" in error.lower()

    details: list[str] = []
    if sync_state.last_sync:
        details.append(f"Last synced {_ago(sync_state.last_sync)}")
    if next_run and not sync_state.is_running:
        details.append(f"{'next' if details else 'Next'} sync {_until(next_run)}")

    if sync_state.is_running:
        state = "running"
        headline = _phase_label(sync_state.phase, sync_state.active_source)
    elif error and not rate_limited:
        state, headline = "error", "The last sync ran into a problem"
    elif sync_state.last_sync is None:
        state, headline = "waiting", "Waiting for the first sync"
    elif catching_up or rate_limited:
        state, headline = "catching-up", "Catching up"
    else:
        state, headline = "ok", "Up to date"

    bars = []
    total = pending["total"]
    for key, label in (
        ("details_pending", "Activity details"),
        ("streams_pending", "Heart rate and power data"),
    ):
        if pending[key]:
            done = total - pending[key]
            bars.append(
                {
                    "label": label,
                    "done": done,
                    "total": total,
                    "pct": round(100 * done / max(total, 1)),
                }
            )
    if sync_state.is_running:
        note = "This updates live while the sync runs."
    else:
        note = (
            f"The rest is fetched over the next syncs, starting {_until(next_run)}."
            if next_run
            else "The rest is fetched over the next syncs."
        )

    return {
        "running": sync_state.is_running,
        "state": state,
        "headline": headline,
        "detail": " · ".join(details),
        "last_sync_local": _local_time(sync_state.last_sync),
        "error": error,
        "rate_limited": rate_limited,
        "bars": bars,
        "note": note,
    }


async def _live_context() -> dict:
    """Everything the auto-refreshing part of the status page shows."""
    async with async_session() as session:
        athlete_id = await _primary_athlete_id(session)
        counts = await _kind_counts(session, athlete_id)
        since = await _kind_since(session, athlete_id)
        pending = await _pending_counts(session, athlete_id)
    providers = await _provider_views(counts)
    return {
        "setup": not any(view["usable"] for view in providers),
        "sync": _sync_view(pending),
        "providers": providers,
        "kinds": _kind_views(counts, since),
    }


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Status page: accounts, sync progress, and where data comes from."""
    context = await _live_context()
    async with async_session() as session:
        resting_hr = await resting_hr_on(
            session,
            datetime.now(ZoneInfo(config.athlete.timezone)).date(),
            fallback=config.athlete.rest_hr,
            max_hr=config.athlete.max_hr,
        )
    context.update(
        app_name=APP_DISPLAY_NAME,
        grafana_url=_grafana_url(),
        athlete=config.athlete,
        resting_hr=resting_hr,
        resting_hr_source=(
            f"{PROVIDER_LABELS.get(resting_hr.provider, resting_hr.provider)} · "
            f"{resting_hr.days}-day median"
            if resting_hr.source == "imported"
            else "From config.yaml"
        ),
    )
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/partials/live", response_class=HTMLResponse)
async def live_partial(request: Request):
    """The status page's live region, polled by static/js/status.js."""
    return templates.TemplateResponse(request, "_live.html", await _live_context())


@app.get("/sync/status")
async def sync_status():
    async with async_session() as session:
        athlete_id = await _primary_athlete_id(session)
        counts = await _kind_counts(session, athlete_id)
        pending = await _pending_counts(session, athlete_id)
    activities = counts.get(DataKind.ACTIVITIES, {})
    total = pending["total"]
    return {
        "is_running": sync_state.is_running,
        "phase": sync_state.phase,
        "active_source": sync_state.active_source,
        "activities_by_source": {
            name: entry["visible"] for name, entry in activities.items() if entry["visible"]
        },
        "superseded": sum(entry["hidden"] for entry in activities.values()),
        "details": {
            "fetched": total - pending["details_pending"],
            "pending": pending["details_pending"],
            "total": total,
        },
        "streams": {
            "fetched": total - pending["streams_pending"],
            "pending": pending["streams_pending"],
            "total": total,
        },
        "rate_limits": sync_state.rate_limit_payload(),
        "last_error": sync_state.last_error,
        "last_sync": sync_state.last_sync.isoformat() if sync_state.last_sync else None,
    }


@app.post("/sync/trigger")
async def trigger_sync():
    if not sync_state.is_running:
        asyncio.create_task(trigger_sync_task())
    return RedirectResponse("/", status_code=303)


@app.post("/sync/full")
async def full_resync():
    if not sync_state.is_running:
        asyncio.create_task(trigger_sync_task(force_resync=True))
    return RedirectResponse("/", status_code=303)
