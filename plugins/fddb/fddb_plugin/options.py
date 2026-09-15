"""FDDB plugin options, built from the injected plugin context."""

from __future__ import annotations

from dataclasses import dataclass

from trainingpulse_common import PluginContext

PLUGIN_NAME = "fddb"
PLUGIN_PREFIX = "/plugins/fddb"


@dataclass(frozen=True)
class FddbOptions:
    """Resolved configuration for one FDDB plugin instance."""

    user: str
    password: str
    cookie: str
    database_url: str
    base_url: str = "https://fddb.info"
    lang: str = "de"
    sync_interval_minutes: int = 60
    sync_lookback_days: int = 365
    request_delay_ms: int = 300

    @classmethod
    def from_context(cls, ctx: PluginContext) -> "FddbOptions":
        options = ctx.options
        return cls(
            user=options.user,
            password=options.password,
            cookie=options.cookie,
            database_url=ctx.database_url,
            base_url=options.base_url.rstrip("/"),
            lang=options.lang,
            sync_interval_minutes=options.sync_interval_minutes,
            sync_lookback_days=options.sync_lookback_days,
            request_delay_ms=options.request_delay_ms,
        )

    @property
    def prefix(self) -> str:
        return PLUGIN_PREFIX

    @property
    def credentials_configured(self) -> bool:
        return bool(self.user and self.password and self.cookie)

    def is_configured(self) -> bool:
        return self.credentials_configured and bool(self.database_url)
