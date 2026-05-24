"""Application configuration loaded from environment variables."""

import os


class Settings:
    """Settings loaded from environment variables with sensible defaults."""

    STRAVA_CLIENT_ID: str = os.environ.get("STRAVA_CLIENT_ID", "")
    STRAVA_CLIENT_SECRET: str = os.environ.get("STRAVA_CLIENT_SECRET", "")

    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://trainingpulse:changeme@localhost:5432/trainingpulse",
    )

    # Comma-separated plugin names: withings, fddb
    ENABLED_PLUGINS: str = os.environ.get("ENABLED_PLUGINS", "")

    APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "http://localhost:8000")

    # Heart rate and power settings
    # These will overwrite Strava values if set in the environment.
    # We use None as default to detect if they were NOT set in the environment.
    MAX_HR: int | None = (
        int(os.environ["MAX_HR"]) if "MAX_HR" in os.environ else
        int(os.environ["DEFAULT_MAX_HR"]) if "DEFAULT_MAX_HR" in os.environ else None
    )
    REST_HR: int | None = (
        int(os.environ["REST_HR"]) if "REST_HR" in os.environ else
        int(os.environ["DEFAULT_REST_HR"]) if "DEFAULT_REST_HR" in os.environ else None
    )
    FTP: int | None = int(os.environ["FTP"]) if "FTP" in os.environ else None

    # Fallbacks if everything else fails (Strava API unavailable and not set in env)
    FALLBACK_MAX_HR: int = 190
    FALLBACK_REST_HR: int = 60
    FALLBACK_FTP: int = 200

    # How often to poll Strava for new activities (minutes)
    SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15"))

    # Concurrency for per-activity API calls during sync. Strava's 15-min quota
    # is the real ceiling; small values (3-8) just keep workers fed without
    # bursting too hard. Setting either to 1 restores fully sequential behavior.
    SYNC_DETAIL_CONCURRENCY: int = max(
        1, int(os.environ.get("SYNC_DETAIL_CONCURRENCY", "5"))
    )
    SYNC_STREAMS_CONCURRENCY: int = max(
        1, int(os.environ.get("SYNC_STREAMS_CONCURRENCY", "5"))
    )

    # Strava OAuth URLs
    STRAVA_AUTH_URL: str = "https://www.strava.com/oauth/authorize"
    STRAVA_TOKEN_URL: str = "https://www.strava.com/oauth/token"
    STRAVA_API_BASE: str = "https://www.strava.com/api/v3"

    # OAuth scopes we need
    STRAVA_SCOPES: str = "activity:read_all,profile:read_all"

    @property
    def oauth_callback_url(self) -> str:
        return f"{self.APP_BASE_URL}/auth/callback"


settings = Settings()
