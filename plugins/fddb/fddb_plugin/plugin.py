"""FDDB TrainingPulse plugin entry point."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text
from trainingpulse_common import make_async_engine, make_session_factory

from fddb_plugin.config import settings
from fddb_plugin.mcp_tools import register_mcp_tools
from fddb_plugin.models import Base
from fddb_plugin.routes import create_router
from fddb_plugin.sync import run_sync

logger = logging.getLogger(__name__)


class FddbPlugin:
    name = "fddb"

    def __init__(self) -> None:
        self._engine = make_async_engine(settings.DATABASE_URL)
        self._session_factory = make_session_factory(self._engine)

    def is_enabled(self) -> bool:
        return settings.is_enabled()

    def is_configured(self) -> bool:
        return settings.is_configured()

    def is_mcp_available(self) -> bool:
        return settings.is_enabled() and bool(settings.DATABASE_URL)

    @property
    def engine(self):
        return self._engine

    @property
    def session_factory(self):
        return self._session_factory

    async def ensure_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS fddb"))
            await conn.run_sync(Base.metadata.create_all)

    def register_routes(self, app: FastAPI, *, prefix: str) -> None:
        router = create_router(self._session_factory)
        app.include_router(router, prefix=prefix)

    async def _trigger_sync(self) -> None:
        async with self._session_factory() as session:
            await run_sync(session)

    def register_scheduler(
        self,
        scheduler: AsyncIOScheduler,
        trigger: Callable[[], Any],
    ) -> None:
        if not settings.credentials_configured:
            logger.info("FDDB scheduler job registered; starts when credentials are set")
        scheduler.add_job(
            self._trigger_sync,
            "interval",
            minutes=settings.SYNC_INTERVAL_MINUTES,
            id="fddb_sync",
            replace_existing=True,
        )

    def register_mcp_tools(self, mcp: FastMCP) -> None:
        register_mcp_tools(mcp, self._session_factory)

    async def dispose(self) -> None:
        await self._engine.dispose()


def build_plugin() -> FddbPlugin:
    return FddbPlugin()
