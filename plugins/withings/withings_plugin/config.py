"""Withings plugin configuration."""

import os
from urllib.parse import urljoin


def _enabled_plugins() -> set[str]:
    raw = os.environ.get("ENABLED_PLUGINS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


class Settings:
    WITHINGS_CLIENT_ID: str = os.environ.get("WITHINGS_CLIENT_ID", "")
    WITHINGS_CLIENT_SECRET: str = os.environ.get("WITHINGS_CLIENT_SECRET", "")

    APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    PLUGIN_PREFIX: str = "/plugins/withings"
    WITHINGS_REDIRECT_URI: str = os.environ.get("WITHINGS_REDIRECT_URI", "").strip()

    DATABASE_URL: str = os.environ["DATABASE_URL"]

    SYNC_INTERVAL_MINUTES: int = int(
        os.environ.get("WITHINGS_SYNC_INTERVAL_MINUTES", os.environ.get("SYNC_INTERVAL_MINUTES", "60"))
    )
    SYNC_LOOKBACK_DAYS: int = int(os.environ.get("WITHINGS_SYNC_LOOKBACK_DAYS", "3650"))

    WITHINGS_AUTH_URL: str = "https://account.withings.com/oauth2_user/authorize2"
    WITHINGS_TOKEN_URL: str = "https://wbsapi.withings.net/v2/oauth2"
    WITHINGS_API_BASE: str = "https://wbsapi.withings.net"
    WITHINGS_SCOPES: str = "user.metrics,user.info"

    @property
    def oauth_callback_url(self) -> str:
        if self.WITHINGS_REDIRECT_URI:
            return self.WITHINGS_REDIRECT_URI
        return urljoin(f"{self.APP_BASE_URL}{self.PLUGIN_PREFIX}/", "get_token")

    @property
    def oauth_callback_url_legacy(self) -> str:
        return urljoin(f"{self.APP_BASE_URL}{self.PLUGIN_PREFIX}/", "auth/callback")

    def is_enabled(self) -> bool:
        return "withings" in _enabled_plugins()

    def is_configured(self) -> bool:
        return bool(
            self.is_enabled()
            and self.WITHINGS_CLIENT_ID
            and self.WITHINGS_CLIENT_SECRET
            and self.DATABASE_URL
        )


settings = Settings()
