"""Withings plugin HTTP routes (mounted under /plugins/withings)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from withings_plugin.client import WithingsClient
from withings_plugin.config import settings
from withings_plugin.sync import get_status, run_sync, sync_state

logger = logging.getLogger(__name__)


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — Withings</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }}
    .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1.25rem; margin: 1rem 0; }}
    .error {{ color: #c00; }}
    .success {{ border-color: #6a6; }}
    a.btn {{ display: inline-block; margin-top: 1rem; padding: 0.5rem 1rem;
             background: #0b57d0; color: #fff; text-decoration: none; border-radius: 4px; }}
    .fine-print {{ color: #666; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>Withings <span class="fine-print">(TrainingPulse plugin)</span></h1>
  <p><a href="/">← TrainingPulse home</a></p>
  {body}
</body>
</html>"""


def _authorize_url(redirect_uri: str | None = None) -> str:
    uri = redirect_uri or settings.oauth_callback_url
    params = {
        "response_type": "code",
        "client_id": settings.WITHINGS_CLIENT_ID,
        "state": secrets.token_urlsafe(16),
        "scope": settings.WITHINGS_SCOPES,
        "redirect_uri": uri,
    }
    return f"{settings.WITHINGS_AUTH_URL}?{urlencode(params)}"


def create_router(
    session_factory: async_sessionmaker,
    *,
    on_connected: Callable[[], None] | None = None,
) -> APIRouter:
    router = APIRouter()

    async def trigger_sync_task() -> None:
        async with session_factory() as session:
            await run_sync(session)

    @router.get("/auth/setup")
    async def auth_setup():
        client_id = settings.WITHINGS_CLIENT_ID
        return JSONResponse(
            {
                "redirect_uri": settings.oauth_callback_url,
                "redirect_uri_legacy_path": settings.oauth_callback_url_legacy,
                "app_base_url": settings.APP_BASE_URL,
                "client_id_prefix": client_id[:12] + "…" if len(client_id) > 12 else client_id,
                "authorize_url_preview": _authorize_url(),
            }
        )

    @router.get("/auth/withings")
    async def auth_withings(redirect_uri: str | None = Query(default=None)):
        allowed = {settings.oauth_callback_url, settings.oauth_callback_url_legacy}
        uri = redirect_uri if redirect_uri in allowed else settings.oauth_callback_url
        return RedirectResponse(_authorize_url(uri))

    @router.api_route("/auth/callback", methods=["GET", "HEAD"])
    @router.api_route("/get_token", methods=["GET", "HEAD"])
    async def auth_callback(
        request: Request,
        code: str | None = None,
        error: str | None = None,
    ):
        if request.method == "HEAD":
            return Response(status_code=200)
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

        async with session_factory() as session:
            try:
                client = WithingsClient(session)
                body = await client.exchange_code(code)
                await client.store_tokens(body)
            except (httpx.HTTPError, RuntimeError) as exc:
                return HTMLResponse(
                    _page(
                        "Authorization Failed",
                        f"<p class='error'>Token exchange failed: {exc}</p>",
                    ),
                    status_code=400,
                )

        if on_connected:
            on_connected()
        asyncio.create_task(trigger_sync_task())
        return HTMLResponse(
            _page(
                "Connected!",
                """
                <div class="card success">
                  <h2>Withings account linked</h2>
                  <p>Weight data is syncing in the background.</p>
                </div>
                """,
            )
        )

    @router.get("/", response_class=HTMLResponse)
    async def home():
        async with session_factory() as session:
            status = await get_status(session)

        if not status["connected"]:
            return HTMLResponse(
                _page(
                    "Setup",
                    f"""
                    <div class="card">
                      <p>Connect Withings to sync body weight into TrainingPulse.</p>
                      <p>Callback URL for the Partner dashboard:</p>
                      <p><code>{settings.oauth_callback_url}</code></p>
                      <a href="{settings.PLUGIN_PREFIX}/auth/withings" class="btn">Connect Withings</a>
                    </div>
                    """,
                )
            )

        sync = status["sync"]
        err = (
            f"<p class='error'>Last sync error: {sync.get('last_error')}</p>"
            if sync.get("last_error")
            else ""
        )
        return HTMLResponse(
            _page(
                "Status",
                f"""
                <div class="card">
                  <p><strong>Measurements stored:</strong> {status['measurement_count']}</p>
                  <p><strong>Latest weight:</strong> {status['latest_weight_kg'] or '—'} kg</p>
                  <p><strong>Sync running:</strong> {sync.get('running')}</p>
                  {err}
                  <form method="post" action="{settings.PLUGIN_PREFIX}/sync/trigger">
                    <button type="submit">Sync now</button>
                  </form>
                </div>
                """,
            )
        )

    @router.post("/sync/trigger")
    async def sync_trigger():
        asyncio.create_task(trigger_sync_task())
        return RedirectResponse(f"{settings.PLUGIN_PREFIX}/", status_code=303)

    @router.get("/api/health")
    async def api_health():
        return {"status": "ok", "plugin": "withings"}

    return router
