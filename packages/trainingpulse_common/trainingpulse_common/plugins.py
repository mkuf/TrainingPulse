"""Plugin protocol for in-process TrainingPulse extensions."""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


@runtime_checkable
class TrainingPulsePlugin(Protocol):
    """Optional data-source plugin loaded into the core FastAPI app.

    Instances are built by the plugin package's ``build_plugin(ctx)`` factory,
    which receives a :class:`~trainingpulse_common.config.PluginContext`.
    Whether a plugin is *enabled* is decided by configuration before it is
    built, so a plugin never has to answer that question itself.
    """

    name: str

    def is_configured(self) -> bool:
        """True when required credentials and database URL are set."""

    def is_mcp_available(self) -> bool:
        """True when the plugin database URL is set (read-only MCP tools).

        Deliberately independent of credentials: the MCP server only reads
        already-synced tables.
        """

    @property
    def engine(self) -> AsyncEngine:
        ...

    @property
    def session_factory(self) -> async_sessionmaker:
        ...

    async def ensure_schema(self) -> None:
        """Create plugin tables on the plugin database."""

    def register_routes(self, app: FastAPI, *, prefix: str) -> None:
        """Mount HTTP routes under the given prefix."""

    def register_scheduler(
        self,
        scheduler: AsyncIOScheduler,
        trigger: Callable[[], Any],
    ) -> None:
        """Register periodic sync job(s)."""

    def register_mcp_tools(self, mcp: FastMCP) -> None:
        """Register MCP tools on the unified server."""
