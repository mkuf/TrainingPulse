"""FDDB plugin configuration."""

import os


def _enabled_plugins() -> set[str]:
    raw = os.environ.get("ENABLED_PLUGINS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


class Settings:
    FDDB_USER: str = os.environ.get("FDDB_USER", "")
    FDDB_PW: str = os.environ.get("FDDB_PW", "")
    FDDB_COOKIE: str = os.environ.get("FDDB_COOKIE", "")
    FDDB_BASE_URL: str = os.environ.get("FDDB_BASE_URL", "https://fddb.info").rstrip("/")
    FDDB_LANG: str = os.environ.get("FDDB_LANG", "de")

    PLUGIN_PREFIX: str = "/plugins/fddb"

    DATABASE_URL: str = os.environ.get(
        "FDDB_DATABASE_URL",
        "postgresql+asyncpg://trainingpulse:changeme@localhost:5432/fddb_nutrition",
    )

    SYNC_INTERVAL_MINUTES: int = int(
        os.environ.get("FDDB_SYNC_INTERVAL_MINUTES", os.environ.get("SYNC_INTERVAL_MINUTES", "60"))
    )
    SYNC_LOOKBACK_DAYS: int = int(os.environ.get("FDDB_SYNC_LOOKBACK_DAYS", "365"))
    SYNC_REQUEST_DELAY_MS: int = int(os.environ.get("FDDB_SYNC_REQUEST_DELAY_MS", "300"))

    @property
    def credentials_configured(self) -> bool:
        return bool(self.FDDB_USER and self.FDDB_PW and self.FDDB_COOKIE)

    def is_enabled(self) -> bool:
        return "fddb" in _enabled_plugins()

    def is_configured(self) -> bool:
        return self.is_enabled() and self.credentials_configured and bool(self.DATABASE_URL)


settings = Settings()
