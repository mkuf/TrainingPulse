"""FDDB plugin HTTP routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from fddb_plugin.config import settings
from fddb_plugin.sync import get_status, run_sync

SETUP_BODY = """
<div class="card">
  <p>Set <code>FDDB_USER</code>, <code>FDDB_PW</code>, and <code>FDDB_COOKIE</code>
    in your <code>.env</code>, enable <code>ENABLED_PLUGINS=fddb</code>, then restart.</p>
  <h3>How to get the session cookie</h3>
  <ol>
    <li>Log in at <a href="https://fddb.info/" target="_blank">fddb.info</a>.</li>
    <li>Open developer tools → Network tab.</li>
    <li>Reload and inspect a request to <code>fddb.info</code>.</li>
    <li>Copy the <code>fddb</code> cookie value (without <code>fddb=</code>).</li>
  </ol>
</div>
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — FDDB</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1.25rem; margin: 1rem 0; }}
    .error {{ color: #c00; }}
    a.btn {{ display: inline-block; margin-top: 1rem; padding: 0.5rem 1rem;
             background: #0b57d0; color: #fff; text-decoration: none; border-radius: 4px; }}
    .fine-print {{ color: #666; font-size: 0.9rem; }}
    button {{ padding: 0.5rem 1rem; cursor: pointer; }}
    ol {{ padding-left: 1.25rem; }}
  </style>
</head>
<body>
  <h1>FDDB <span class="fine-print">(TrainingPulse plugin)</span></h1>
  <p><a href="/">← TrainingPulse home</a></p>
  {body}
</body>
</html>"""


def create_router(session_factory: async_sessionmaker) -> APIRouter:
    router = APIRouter()

    async def trigger_sync_task() -> None:
        async with session_factory() as session:
            await run_sync(session)

    @router.get("/", response_class=HTMLResponse)
    async def home():
        async with session_factory() as session:
            status = await get_status(session)

        if not status["configured"]:
            return HTMLResponse(_page("Setup", SETUP_BODY))

        sync = status["sync"]
        err = (
            f"<p class='error'>Last sync error: {sync.get('last_error')}</p>"
            if sync.get("last_error")
            else ""
        )
        body = f"""
        <div class="card">
          <p><strong>Days stored:</strong> {status['day_count']}</p>
          <p><strong>Date range:</strong>
            {status['earliest_date'] or '—'} → {status['latest_date'] or '—'}</p>
          <p><strong>Latest day:</strong> {status['latest_date'] or '—'}
            ({status['latest_kcal'] or '—'} kcal)</p>
          <p><strong>Sync running:</strong> {sync.get('running')}</p>
          {err}
          <form method="post" action="{settings.PLUGIN_PREFIX}/sync/trigger">
            <button type="submit">Sync now</button>
          </form>
        </div>
        """
        return HTMLResponse(_page("Status", body))

    @router.post("/sync/trigger")
    async def sync_trigger():
        asyncio.create_task(trigger_sync_task())
        return RedirectResponse(f"{settings.PLUGIN_PREFIX}/", status_code=303)

    @router.get("/api/health")
    async def api_health():
        return {"status": "ok", "plugin": "fddb"}

    return router
