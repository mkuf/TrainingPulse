"""Withings TrainingPulse plugin entry point."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from trainingpulse_common import make_async_engine, make_session_factory

from withings_plugin.config import settings
from withings_plugin.mcp_tools import register_mcp_tools
from withings_plugin.models import WithingsToken
from withings_plugin.routes import create_router
from withings_plugin.schema import ensure_schema
from withings_plugin.sync import run_sync

logger = logging.getLogger(__name__)


class WithingsPlugin:
    name = "withings"

    def __init__(self) -> None:
        self._engine = make_async_engine(settings.DATABASE_URL)
        self._session_factory = make_session_factory(self._engine)
        self._scheduler_started = False

    def is_enabled(self) -> bool:
        return settings.is_enabled()

    def is_configured(self) -> bool:
        return settings.is_configured()

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
            on_connected=on_connected,
        )
        app.include_router(router, prefix=prefix)

    def _ensure_scheduler_job(self, scheduler: AsyncIOScheduler) -> None:
        if scheduler.get_job("withings_sync") is None:
            scheduler.add_job(
                self._trigger_sync,
                "interval",
                minutes=settings.SYNC_INTERVAL_MINUTES,
                id="withings_sync",
                replace_existing=True,
            )
        self._scheduler_started = True

    async def _trigger_sync(self) -> None:
        async with self._session_factory() as session:
            await run_sync(session)

    def register_scheduler(
        self,
        scheduler: AsyncIOScheduler,
        trigger: Callable[[], Any],
    ) -> None:
        scheduler.add_job(
            self._trigger_sync,
            "interval",
            minutes=settings.SYNC_INTERVAL_MINUTES,
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


def build_plugin() -> WithingsPlugin:
    return WithingsPlugin()
