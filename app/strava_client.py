"""Strava API client with automatic token refresh and rate limiting."""

import asyncio
import logging
import time

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import StravaToken

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when we need to wait for the rate limit window to reset."""

    def __init__(self, reset_after: float):
        self.reset_after = reset_after
        super().__init__(f"Rate limit exceeded. Reset after {reset_after:.0f}s")


class StravaClient:
    """Async Strava API client with auto token refresh and rate limit handling."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._http = httpx.AsyncClient(timeout=30.0)
        self._rate_limit_usage_15min = 0
        self._rate_limit_limit_15min = 100
        self._rate_limit_usage_daily = 0
        self._rate_limit_limit_daily = 1000

    async def close(self):
        await self._http.aclose()

    # ── Token management ────────────────────────────────────────────

    async def _get_token(self) -> StravaToken | None:
        """Get the stored token from the database."""
        result = await self.session.execute(select(StravaToken).limit(1))
        return result.scalar_one_or_none()

    async def _ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        token = await self._get_token()
        if token is None:
            raise ValueError("No Strava token found. Please authorize the app first.")

        # Refresh if token expires in less than 5 minutes
        if token.expires_at < time.time() + 300:
            logger.info("Access token expired or expiring soon, refreshing...")
            response = await self._http.post(
                settings.STRAVA_TOKEN_URL,
                data={
                    "client_id": settings.STRAVA_CLIENT_ID,
                    "client_secret": settings.STRAVA_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                },
            )
            response.raise_for_status()
            data = response.json()

            token.access_token = data["access_token"]
            token.refresh_token = data["refresh_token"]
            token.expires_at = data["expires_at"]
            await self.session.commit()
            logger.info("Token refreshed successfully, new expiry: %s", data["expires_at"])

        return token.access_token

    # ── Rate limiting ───────────────────────────────────────────────

    def _update_rate_limits(self, response: httpx.Response):
        """Parse Strava rate limit headers and update internal state."""
        usage = response.headers.get("X-RateLimit-Usage", "")
        limit = response.headers.get("X-RateLimit-Limit", "")

        if usage and limit:
            try:
                usage_parts = usage.split(",")
                limit_parts = limit.split(",")
                self._rate_limit_usage_15min = int(usage_parts[0].strip())
                self._rate_limit_usage_daily = int(usage_parts[1].strip())
                self._rate_limit_limit_15min = int(limit_parts[0].strip())
                self._rate_limit_limit_daily = int(limit_parts[1].strip())
            except (ValueError, IndexError):
                pass

    @property
    def rate_limit_remaining_15min(self) -> int:
        return self._rate_limit_limit_15min - self._rate_limit_usage_15min

    @property
    def rate_limit_remaining_daily(self) -> int:
        return self._rate_limit_limit_daily - self._rate_limit_usage_daily

    async def _wait_for_rate_limit(self):
        """If we're close to the rate limit, sleep until the window resets."""
        if self.rate_limit_remaining_15min <= 5:
            # 15-minute windows reset at :00, :15, :30, :45
            now = time.time()
            current_minute = int(now // 60) % 15
            seconds_until_reset = (15 - current_minute) * 60 - (now % 60)
            wait_time = max(seconds_until_reset + 5, 10)  # 5s buffer
            logger.warning(
                "Approaching 15-min rate limit (%d/%d used). Sleeping %.0fs...",
                self._rate_limit_usage_15min,
                self._rate_limit_limit_15min,
                wait_time,
            )
            await asyncio.sleep(wait_time)
            # Reset counter after sleep
            self._rate_limit_usage_15min = 0

        if self.rate_limit_remaining_daily <= 20:
            logger.error(
                "Approaching daily rate limit (%d/%d used). Pausing sync.",
                self._rate_limit_usage_daily,
                self._rate_limit_limit_daily,
            )
            raise RateLimitExceeded(reset_after=3600)

    # ── API methods ─────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        """Make an authenticated request to the Strava API."""
        await self._wait_for_rate_limit()
        access_token = await self._ensure_valid_token()

        url = f"{settings.STRAVA_API_BASE}{path}"
        headers = {"Authorization": f"Bearer {access_token}"}

        response = await self._http.request(method, url, headers=headers, **kwargs)
        self._update_rate_limits(response)

        if response.status_code == 429:
            logger.warning("Got 429 Too Many Requests from Strava")
            raise RateLimitExceeded(reset_after=900)

        response.raise_for_status()
        return response.json()

    async def get_athlete(self) -> dict:
        """Get the authenticated athlete's profile."""
        return await self._request("GET", "/athlete")

    async def get_athlete_zones(self) -> dict:
        """Get the athlete's heart rate and power zones."""
        return await self._request("GET", "/athlete/zones")

    async def get_activities(
        self, page: int = 1, per_page: int = 200, after: int | None = None
    ) -> list[dict]:
        """Get a page of the athlete's activities."""
        params = {"page": page, "per_page": per_page}
        if after is not None:
            params["after"] = after
        return await self._request("GET", "/athlete/activities", params=params)

    async def get_activity_streams(
        self, activity_id: int, keys: str = "time,heartrate"
    ) -> dict:
        """Get stream data for an activity."""
        params = {"keys": keys, "key_by_type": "true"}
        return await self._request(
            "GET", f"/activities/{activity_id}/streams", params=params
        )
