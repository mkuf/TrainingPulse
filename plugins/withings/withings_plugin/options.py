"""Withings plugin options, built from the injected plugin context."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from trainingpulse_common import PluginContext

PLUGIN_NAME = "withings"
PLUGIN_PREFIX = "/plugins/withings"

# Provider endpoints are properties of Withings, not deployment config.
AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
API_BASE = "https://wbsapi.withings.net"
SCOPES = "user.metrics,user.info"


@dataclass(frozen=True)
class WithingsOptions:
    """Resolved configuration for one Withings plugin instance."""

    client_id: str
    client_secret: str
    app_base_url: str
    database_url: str
    redirect_uri: str = ""
    sync_interval_minutes: int = 60
    sync_lookback_days: int = 3650

    @classmethod
    def from_context(cls, ctx: PluginContext) -> "WithingsOptions":
        options = ctx.options
        return cls(
            client_id=options.client_id.strip(),
            client_secret=options.client_secret.strip(),
            app_base_url=ctx.app_base_url.rstrip("/"),
            database_url=ctx.database_url,
            redirect_uri=options.redirect_uri.strip(),
            sync_interval_minutes=options.sync_interval_minutes,
            sync_lookback_days=options.sync_lookback_days,
        )

    @property
    def prefix(self) -> str:
        return PLUGIN_PREFIX

    @property
    def oauth_callback_url(self) -> str:
        if self.redirect_uri:
            return self.redirect_uri
        return urljoin(f"{self.app_base_url}{PLUGIN_PREFIX}/", "get_token")

    @property
    def oauth_callback_url_legacy(self) -> str:
        return urljoin(f"{self.app_base_url}{PLUGIN_PREFIX}/", "auth/callback")

    def is_configured(self) -> bool:
        return bool(
            self.client_id.strip() and self.client_secret.strip() and self.database_url
        )
