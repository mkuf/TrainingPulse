"""Discover and wire optional TrainingPulse plugins."""

from __future__ import annotations

import logging
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from trainingpulse_common import Config, PluginContext, TrainingPulsePlugin
from trainingpulse_common.config import PLUGIN_NAMES, plugin_context

logger = logging.getLogger(__name__)

_PLUGIN_BUILDERS: dict[str, Callable[[PluginContext], TrainingPulsePlugin]] = {
    "withings": lambda ctx: __import__(
        "withings_plugin", fromlist=["build_plugin"]
    ).build_plugin(ctx),
    "fddb": lambda ctx: __import__(
        "fddb_plugin", fromlist=["build_plugin"]
    ).build_plugin(ctx),
}


def enabled_plugin_names(config: Config) -> list[str]:
    """Plugin names enabled in configuration, in declaration order."""
    return config.plugins.enabled_names()


def load_plugins(
    config: Config, *, for_mcp: bool = False
) -> list[TrainingPulsePlugin]:
    """Build every enabled plugin that is usable in this process.

    The core app needs full credentials; the MCP server only reads tables, so
    it accepts a plugin that merely has a database URL.
    """
    loaded: list[TrainingPulsePlugin] = []
    for name in config.plugins.enabled_names():
        builder = _PLUGIN_BUILDERS.get(name)
        if builder is None:
            logger.warning("No builder registered for plugin: %s", name)
            continue

        try:
            plugin = builder(plugin_context(config, name))
        except Exception as exc:  # noqa: BLE001 - one plugin must not stop boot
            logger.error("Plugin %s failed to initialize: %s", name, exc)
            continue

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


__all__ = [
    "PLUGIN_NAMES",
    "dispose_plugins",
    "enabled_plugin_names",
    "load_plugins",
    "register_plugin_mcp_tools",
    "setup_plugins",
]
