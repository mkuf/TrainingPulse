"""Application configuration loaded from environment variables."""

import os


def _env_int(*names: str) -> int | None:
    """Return the first non-empty env var as int, or None.

    Compose often injects ``MAX_HR=`` (empty) when the host .env omits the
    key; treat blank as unset so startup does not crash on ``int("")``.
    """
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        raw = raw.strip()
        if not raw:
            continue
        return int(raw)
    return None


class Settings:
    """Settings loaded from environment variables with sensible defaults."""

    # intervals.icu (API key from Settings → Developer Settings)
    INTERVALS_ICU_API_KEY: str = os.environ.get("INTERVALS_ICU_API_KEY", "")
    INTERVALS_ICU_API_BASE: str = os.environ.get(
        "INTERVALS_ICU_API_BASE", "https://intervals.icu/api/v1"
    )

    # Google Health API — OAuth client from Google Cloud
    GOOGLE_HEALTH_CLIENT_ID: str = os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "")
    GOOGLE_HEALTH_CLIENT_SECRET: str = os.environ.get(
        "GOOGLE_HEALTH_CLIENT_SECRET", ""
    )
    GOOGLE_HEALTH_API_BASE: str = os.environ.get(
        "GOOGLE_HEALTH_API_BASE", "https://health.googleapis.com/v4"
    )
    GOOGLE_OAUTH_AUTH_URL: str = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_OAUTH_TOKEN_URL: str = "https://oauth2.googleapis.com/token"
    GOOGLE_HEALTH_SCOPES: str = " ".join(
        [
            "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
            "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
        ]
    )

    # Comma-separated activity sources: icu, google_health (default: both when configured)
    ENABLED_ACTIVITY_SOURCES: str = os.environ.get(
        "ENABLED_ACTIVITY_SOURCES", "icu,google_health"
    )

    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://trainingpulse:changeme@localhost:5432/trainingpulse",
    )

    # Comma-separated plugin names: withings, fddb
    ENABLED_PLUGINS: str = os.environ.get("ENABLED_PLUGINS", "")

    APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "http://localhost:8000")

    # Heart rate and power settings
    # These will overwrite provider values if set in the environment.
    MAX_HR: int | None = _env_int("MAX_HR", "DEFAULT_MAX_HR")
    REST_HR: int | None = _env_int("REST_HR", "DEFAULT_REST_HR")
    FTP: int | None = _env_int("FTP")

    FALLBACK_MAX_HR: int = 190
    FALLBACK_REST_HR: int = 60
    FALLBACK_FTP: int = 200

    # How often to poll providers for new activities (minutes)
    SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15") or "15")

    # Re-list activities this many days back on each sync to detect edits.
    SYNC_RECONCILE_DAYS: int = int(os.environ.get("SYNC_RECONCILE_DAYS", "90") or "90")

    # Years of history to pull on first intervals.icu backfill
    SYNC_BACKFILL_YEARS: int = int(os.environ.get("SYNC_BACKFILL_YEARS", "10") or "10")

    # Date-window size (days) when listing intervals.icu activities
    SYNC_ICU_WINDOW_DAYS: int = int(os.environ.get("SYNC_ICU_WINDOW_DAYS", "90") or "90")

    SYNC_DETAIL_CONCURRENCY: int = max(
        1, int(os.environ.get("SYNC_DETAIL_CONCURRENCY", "5") or "5")
    )
    SYNC_STREAMS_CONCURRENCY: int = max(
        1, int(os.environ.get("SYNC_STREAMS_CONCURRENCY", "5") or "5")
    )

    @property
    def google_oauth_callback_url(self) -> str:
        return f"{self.APP_BASE_URL}/auth/google/callback"

    def enabled_sources(self) -> set[str]:
        raw = (self.ENABLED_ACTIVITY_SOURCES or "").strip()
        if not raw:
            return set()
        return {p.strip().lower() for p in raw.split(",") if p.strip()}

    def icu_enabled(self) -> bool:
        return "icu" in self.enabled_sources() and bool(self.INTERVALS_ICU_API_KEY)

    def google_health_configured(self) -> bool:
        return bool(
            self.GOOGLE_HEALTH_CLIENT_ID and self.GOOGLE_HEALTH_CLIENT_SECRET
        )

    def google_health_enabled(self) -> bool:
        sources = self.enabled_sources()
        # Accept legacy "fitbit" alias from older .env files
        wanted = "google_health" in sources or "fitbit" in sources
        return wanted and self.google_health_configured()


settings = Settings()
