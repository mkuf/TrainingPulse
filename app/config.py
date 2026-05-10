"""Application configuration loaded from environment variables."""

import os


class Settings:
    """Settings loaded from environment variables with sensible defaults."""

    STRAVA_CLIENT_ID: str = os.environ.get("STRAVA_CLIENT_ID", "")
    STRAVA_CLIENT_SECRET: str = os.environ.get("STRAVA_CLIENT_SECRET", "")

    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://strava:changeme@localhost:5432/strava_fitness",
    )

    APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "http://localhost:8000")

    # Default heart rate values (used if Strava zones are unavailable)
    DEFAULT_MAX_HR: int = int(os.environ.get("DEFAULT_MAX_HR", "190"))
    DEFAULT_REST_HR: int = int(os.environ.get("DEFAULT_REST_HR", "60"))

    # How often to poll Strava for new activities (minutes)
    SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15"))

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
