"""Build activity sources from configuration."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker
from trainingpulse_common.config import Config

from sources.base import ActivitySource
from sources.google_health import GoogleHealthSource
from sources.intervals import IntervalsSource
from sources.strava import StravaSource

logger = logging.getLogger(__name__)


def _build_icu(config: Config, session_factory: async_sessionmaker) -> ActivitySource:
    return IntervalsSource(
        api_key=config.sources.icu.api_key,
        api_base=config.sources.icu.api_base,
        session_factory=session_factory,
    )


def _build_strava(
    config: Config, session_factory: async_sessionmaker
) -> ActivitySource:
    return StravaSource(
        session_factory=session_factory,
        client_id=config.sources.strava.client_id,
        client_secret=config.sources.strava.client_secret,
        app_base_url=config.app.base_url,
    )


def _build_google_health(
    config: Config, session_factory: async_sessionmaker
) -> ActivitySource:
    return GoogleHealthSource(
        session_factory=session_factory,
        client_id=config.sources.google_health.client_id,
        client_secret=config.sources.google_health.client_secret,
        api_base=config.sources.google_health.api_base,
        app_base_url=config.app.base_url,
    )


_BUILDERS = {
    "icu": _build_icu,
    "strava": _build_strava,
    "google_health": _build_google_health,
}


def build_source(
    name: str, config: Config, session_factory: async_sessionmaker
) -> ActivitySource | None:
    """Instantiate one source by name, or None when the name is unknown."""
    builder = _BUILDERS.get(name)
    if builder is None:
        logger.warning("Unknown activity source: %s", name)
        return None
    return builder(config, session_factory)


def build_all_sources(
    config: Config, session_factory: async_sessionmaker
) -> list[ActivitySource]:
    """Every known source, in precedence order, regardless of enabled state.

    Used by the status page, which has to show disabled and unconfigured
    sources too.
    """
    sources = []
    for name in config.sources.precedence:
        source = build_source(name, config, session_factory)
        if source is not None:
            sources.append(source)
    return sources


def build_enabled_sources(
    config: Config, session_factory: async_sessionmaker
) -> list[ActivitySource]:
    """Enabled, configured sources in precedence order."""
    sources = []
    for name in config.sources.enabled_names():
        source = build_source(name, config, session_factory)
        if source is None:
            continue
        if not source.is_configured():
            # The config loader rejects enabled-without-credentials, so this is
            # only reachable if a source adds a stricter check of its own.
            logger.warning("Source %s is enabled but not configured; skipping", name)
            continue
        sources.append(source)
    return sources
