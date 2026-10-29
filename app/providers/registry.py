"""Build providers from configuration."""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from catalog import PROVIDER_NAMES
from config_loader import Config
from providers.base import Provider
from providers.fddb import FddbSource
from providers.garmin import GarminSource
from providers.google_health import GoogleHealthSource
from providers.hammerhead import HammerheadSource
from providers.intervals import IntervalsSource
from providers.strava import StravaSource
from providers.withings import WithingsSource

logger = logging.getLogger(__name__)


def _build_icu(config: Config, session_factory: async_sessionmaker) -> Provider:
    section = config.providers.icu
    return IntervalsSource(
        api_key=section.api_key,
        api_base=section.api_base,
        session_factory=session_factory,
    )


def _build_strava(config: Config, session_factory: async_sessionmaker) -> Provider:
    section = config.providers.strava
    return StravaSource(
        session_factory=session_factory,
        client_id=section.client_id,
        client_secret=section.client_secret,
        app_base_url=config.app.base_url,
    )


def _build_google_health(
    config: Config, session_factory: async_sessionmaker
) -> Provider:
    section = config.providers.google_health
    return GoogleHealthSource(
        session_factory=session_factory,
        client_id=section.client_id,
        client_secret=section.client_secret,
        api_base=section.api_base,
        app_base_url=config.app.base_url,
    )


def _build_hammerhead(
    config: Config, session_factory: async_sessionmaker
) -> Provider:
    section = config.providers.hammerhead
    return HammerheadSource(
        session_factory=session_factory,
        client_id=section.client_id,
        client_secret=section.client_secret,
        api_base=section.api_base,
        app_base_url=config.app.base_url,
    )


def _build_garmin(config: Config, session_factory: async_sessionmaker) -> Provider:
    section = config.providers.garmin
    return GarminSource(
        api_key=section.api_key,
        api_base=section.api_base,
        session_factory=session_factory,
        timezone=config.athlete.timezone,
    )


def _build_withings(config: Config, session_factory: async_sessionmaker) -> Provider:
    section = config.providers.withings
    return WithingsSource(
        session_factory=session_factory,
        client_id=section.client_id,
        client_secret=section.client_secret,
        app_base_url=config.app.base_url,
        redirect_uri=section.redirect_uri,
    )


def _build_fddb(config: Config, session_factory: async_sessionmaker) -> Provider:
    section = config.providers.fddb
    return FddbSource(
        user=section.user,
        password=section.password,
        cookie=section.cookie,
        base_url=section.base_url,
        lang=section.lang,
        request_delay_ms=section.request_delay_ms,
        timezone=config.athlete.timezone,
    )


_BUILDERS: dict[str, Callable[[Config, async_sessionmaker], Provider]] = {
    "icu": _build_icu,
    "strava": _build_strava,
    "google_health": _build_google_health,
    "hammerhead": _build_hammerhead,
    "garmin": _build_garmin,
    "withings": _build_withings,
    "fddb": _build_fddb,
}


def build_provider(
    name: str, config: Config, session_factory: async_sessionmaker
) -> Provider | None:
    """Instantiate one provider by name, or None when the name is unknown."""
    builder = _BUILDERS.get(name)
    if builder is None:
        logger.warning("Unknown provider: %s", name)
        return None
    return builder(config, session_factory)


def build_all_providers(
    config: Config, session_factory: async_sessionmaker
) -> list[Provider]:
    """Every known provider, in declaration order, regardless of enabled state.

    Used by the status page, which has to show disabled and unconfigured
    providers too, and to mount every provider's auth routes.
    """
    providers = []
    for name in PROVIDER_NAMES:
        provider = build_provider(name, config, session_factory)
        if provider is not None:
            providers.append(provider)
    return providers


def build_enabled_providers(
    config: Config, session_factory: async_sessionmaker
) -> list[Provider]:
    """Enabled, configured providers, in declaration order."""
    providers = []
    for name in config.providers.enabled_names():
        provider = build_provider(name, config, session_factory)
        if provider is None:
            continue
        if not provider.is_configured():
            # The config loader rejects enabled-without-credentials, so this is
            # only reachable if a provider adds a stricter check of its own.
            logger.warning("Provider %s is enabled but not configured; skipping", name)
            continue
        providers.append(provider)
    return providers
