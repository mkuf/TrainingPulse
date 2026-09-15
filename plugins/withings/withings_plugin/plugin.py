"""Withings TrainingPulse plugin entry point."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from trainingpulse_common import PluginContext, make_async_engine, make_session_factory

from withings_plugin.mcp_tools import register_mcp_tools
from withings_plugin.models import WithingsToken
from withings_plugin.options import PLUGIN_NAME, WithingsOptions
from withings_plugin.routes import create_router
from withings_plugin.schema import ensure_schema
from withings_plugin.sync import run_sync

logger = logging.getLogger(__name__)


class WithingsPlugin:
    name = PLUGIN_NAME

    def __init__(self, options: WithingsOptions) -> None:
        self.options = options
        self._engine = make_async_engine(options.database_url)
        self._session_factory = make_session_factory(self._engine)
        self._scheduler_started = False

    def is_configured(self) -> bool:
        return self.options.is_configured()

    def is_mcp_available(self) -> bool:
        """MCP only reads the plugin's own tables, so credentials are optional."""
        return bool(self.options.database_url)

    @property
    def engine(self):
        return self._engine

    @property
    def session_factory(self):
        return self._session_factory

    async def ensure_schema(self) -> None:
        async with self._engine.begin() as conn:
            await ensure_schema(conn)

    def register_routes(self, app: FastAPI, *, prefix: str) -> None:
        def on_connected() -> None:
            self._ensure_scheduler_job(app.state.scheduler)

        router = create_router(
            self._session_factory,
            self.options,
            on_connected=on_connected,
        )
        app.include_router(router, prefix=prefix)

    def _ensure_scheduler_job(self, scheduler: AsyncIOScheduler) -> None:
        if scheduler.get_job("withings_sync") is None:
            scheduler.add_job(
                self._trigger_sync,
                "interval",
                minutes=self.options.sync_interval_minutes,
                id="withings_sync",
                replace_existing=True,
            )
        self._scheduler_started = True

    async def _trigger_sync(self) -> None:
        async with self._session_factory() as session:
            await run_sync(session, self.options)

    def register_scheduler(
        self,
        scheduler: AsyncIOScheduler,
        trigger: Callable[[], Any],
    ) -> None:
        scheduler.add_job(
            self._trigger_sync,
            "interval",
            minutes=self.options.sync_interval_minutes,
            id="withings_sync",
            replace_existing=True,
        )

    async def bootstrap_scheduler(self, scheduler: AsyncIOScheduler) -> None:
        async with self._session_factory() as session:
            token = (
                await session.execute(select(WithingsToken).limit(1))
            ).scalar_one_or_none()
        if token is not None:
            self._ensure_scheduler_job(scheduler)

    def register_mcp_tools(self, mcp: FastMCP) -> None:
        register_mcp_tools(mcp, self._session_factory)

    async def dispose(self) -> None:
        await self._engine.dispose()


def build_plugin(ctx: PluginContext) -> WithingsPlugin:
    return WithingsPlugin(WithingsOptions.from_context(ctx))
