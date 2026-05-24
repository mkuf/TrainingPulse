"""Discover and wire optional TrainingPulse plugins."""

from __future__ import annotations

import logging
import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from trainingpulse_common import TrainingPulsePlugin

logger = logging.getLogger(__name__)

_PLUGIN_BUILDERS: dict[str, Any] = {
    "withings": lambda: __import__(
        "withings_plugin", fromlist=["build_plugin"]
    ).build_plugin(),
    "fddb": lambda: __import__(
        "fddb_plugin", fromlist=["build_plugin"]
    ).build_plugin(),
}


def _parse_enabled() -> set[str]:
    raw = os.environ.get("ENABLED_PLUGINS", "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def load_plugins(*, for_mcp: bool = False) -> list[TrainingPulsePlugin]:
    enabled = _parse_enabled()
    loaded: list[TrainingPulsePlugin] = []
    for name in ("withings", "fddb"):
        if name not in enabled:
            continue
        builder = _PLUGIN_BUILDERS.get(name)
        if builder is None:
            logger.warning("Unknown plugin in ENABLED_PLUGINS: %s", name)
            continue
        plugin = builder()
        if for_mcp:
            if not plugin.is_mcp_available():
                logger.warning(
                    "Plugin %s is enabled but has no database URL — skipping MCP tools",
                    name,
                )
                continue
        elif not plugin.is_configured():
            logger.warning(
                "Plugin %s is enabled but not configured — skipping routes and sync",
                name,
            )
            continue
        loaded.append(plugin)
        logger.info("Loaded plugin: %s", name)
    return loaded


async def setup_plugins(
    app: FastAPI,
    scheduler: AsyncIOScheduler,
    plugins: list[TrainingPulsePlugin],
) -> list[TrainingPulsePlugin]:
    """Initialize schemas, routes, and schedulers for configured plugins."""
    active: list[TrainingPulsePlugin] = []
    for plugin in plugins:
        await plugin.ensure_schema()
        prefix = f"/plugins/{plugin.name}"
        plugin.register_routes(app, prefix=prefix)
        plugin.register_scheduler(scheduler, trigger=lambda: None)
        active.append(plugin)
        logger.info("Registered plugin routes at %s", prefix)

    for plugin in active:
        if plugin.name == "withings" and hasattr(plugin, "bootstrap_scheduler"):
            await plugin.bootstrap_scheduler(scheduler)

    app.state.plugins = active
    return active


def register_plugin_mcp_tools(mcp: FastMCP, plugins: list[TrainingPulsePlugin]) -> None:
    for plugin in plugins:
        plugin.register_mcp_tools(mcp)
        logger.info("Registered MCP tools for plugin: %s", plugin.name)


async def dispose_plugins(plugins: list[TrainingPulsePlugin]) -> None:
    for plugin in plugins:
        if hasattr(plugin, "dispose"):
            await plugin.dispose()
