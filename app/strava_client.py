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
        # Strava publishes TWO rate-limit budgets per response:
        #   X-RateLimit-*: overall (read + write combined). Default 200/2000.
        #   X-ReadRateLimit-*: reads only. Default 100/1000.
        # Almost everything we do is a read, so the read budget is what
        # actually triggers 429s. We track both and gate on whichever is
        # closer to its limit. Defaults match Strava's current "default" tier.
        self._rate_limit_usage_15min = 0
        self._rate_limit_limit_15min = 200
        self._rate_limit_usage_daily = 0
        self._rate_limit_limit_daily = 2000
        self._read_rate_limit_usage_15min = 0
        self._read_rate_limit_limit_15min = 100
        self._read_rate_limit_usage_daily = 0
        self._read_rate_limit_limit_daily = 1000
        self._gear_cache: dict[str, str | None] = {}
        # Serializes the rate-limit check so concurrent workers don't all read
        # "remaining=4" simultaneously and overshoot the 15-min window.
        self._rate_limit_lock = asyncio.Lock()
        # Serializes token refresh so concurrent callers don't double-refresh.
        self._token_lock = asyncio.Lock()

    async def close(self):
        await self._http.aclose()

    # ── Token management ────────────────────────────────────────────

    async def _get_token(self) -> StravaToken | None:
        """Get the stored token from the database."""
        result = await self.session.execute(select(StravaToken).limit(1))
        return result.scalar_one_or_none()

    async def _ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if needed.

        Serialized via ``_token_lock`` so concurrent workers don't all read the
        shared DB session in parallel and don't double-refresh near expiry.
        """
        async with self._token_lock:
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

    @staticmethod
    def _parse_pair(header_value: str) -> tuple[int, int] | None:
        """Parse a "short,daily" comma-separated header into a tuple of ints."""
        if not header_value:
            return None
        try:
            short_str, daily_str = header_value.split(",", 1)
            return int(short_str.strip()), int(daily_str.strip())
        except (ValueError, IndexError):
            return None

    def _update_rate_limits(self, response: httpx.Response):
        """Parse Strava rate limit headers and update internal state.

        Strava sends both an overall budget (``X-RateLimit-*``) and a
        read-specific budget (``X-ReadRateLimit-*``). We update whichever
        ones are present; missing headers leave the previous value intact.
        """
        overall_usage = self._parse_pair(response.headers.get("X-RateLimit-Usage", ""))
        overall_limit = self._parse_pair(response.headers.get("X-RateLimit-Limit", ""))
        if overall_usage is not None:
            self._rate_limit_usage_15min, self._rate_limit_usage_daily = overall_usage
        if overall_limit is not None:
            self._rate_limit_limit_15min, self._rate_limit_limit_daily = overall_limit

        read_usage = self._parse_pair(response.headers.get("X-ReadRateLimit-Usage", ""))
        read_limit = self._parse_pair(response.headers.get("X-ReadRateLimit-Limit", ""))
        if read_usage is not None:
            self._read_rate_limit_usage_15min, self._read_rate_limit_usage_daily = read_usage
        if read_limit is not None:
            self._read_rate_limit_limit_15min, self._read_rate_limit_limit_daily = read_limit

    @property
    def rate_limit_remaining_15min(self) -> int:
        """Smaller of overall- and read-budget remaining for the 15-min window."""
        return min(
            self._rate_limit_limit_15min - self._rate_limit_usage_15min,
            self._read_rate_limit_limit_15min - self._read_rate_limit_usage_15min,
        )

    @property
    def rate_limit_remaining_daily(self) -> int:
        """Smaller of overall- and read-budget remaining for the daily window."""
        return min(
            self._rate_limit_limit_daily - self._rate_limit_usage_daily,
            self._read_rate_limit_limit_daily - self._read_rate_limit_usage_daily,
        )

    async def _wait_for_rate_limit(self):
        """If we're close to either rate limit, sleep until the window resets."""
        async with self._rate_limit_lock:
            if self.rate_limit_remaining_15min <= 5:
                # 15-minute windows reset at :00, :15, :30, :45
                now = time.time()
                current_minute = int(now // 60) % 15
                seconds_until_reset = (15 - current_minute) * 60 - (now % 60)
                wait_time = max(seconds_until_reset + 5, 10)  # 5s buffer
                logger.warning(
                    "Approaching 15-min rate limit "
                    "(overall %d/%d, reads %d/%d). Sleeping %.0fs...",
                    self._rate_limit_usage_15min,
                    self._rate_limit_limit_15min,
                    self._read_rate_limit_usage_15min,
                    self._read_rate_limit_limit_15min,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                # Reset both counters after sleep; Strava's response will
                # rewrite them on the next call.
                self._rate_limit_usage_15min = 0
                self._read_rate_limit_usage_15min = 0

            if self.rate_limit_remaining_daily <= 20:
                logger.error(
                    "Approaching daily rate limit "
                    "(overall %d/%d, reads %d/%d). Pausing sync.",
                    self._rate_limit_usage_daily,
                    self._rate_limit_limit_daily,
                    self._read_rate_limit_usage_daily,
                    self._read_rate_limit_limit_daily,
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

    async def get_activity(
        self, activity_id: int, *, include_all_efforts: bool = False
    ) -> dict:
        """Get a single activity (detailed representation)."""
        if include_all_efforts:
            return await self._request(
                "GET",
                f"/activities/{activity_id}",
                params={"include_all_efforts": "true"},
            )
        return await self._request("GET", f"/activities/{activity_id}")

    async def get_gear_display_name(self, gear_id: str) -> str | None:
        """Resolve bike/shoe label from GET /gear/{id}; cached per client lifetime."""
        gid = (gear_id or "").strip()
        if not gid or gid.lower() == "none":
            return None
        if gid in self._gear_cache:
            return self._gear_cache[gid]
        try:
            data = await self._request("GET", f"/gear/{gid}")
        except RateLimitExceeded:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._gear_cache[gid] = None
                return None
            raise
        except Exception as e:
            logger.warning("Failed to fetch gear %s: %s", gid, e)
            self._gear_cache[gid] = None
            return None
        if not isinstance(data, dict):
            self._gear_cache[gid] = None
            return None
        label = (data.get("nickname") or data.get("name") or "").strip() or None
        self._gear_cache[gid] = label
        return label

    async def get_activity_streams(
        self,
        activity_id: int,
        keys: str = "time,latlng,distance,altitude,heartrate,cadence,watts,temp,velocity_smooth,grade_smooth",
    ) -> dict:
        """Get stream data for an activity."""
        params = {"keys": keys, "key_by_type": "true"}
        return await self._request(
            "GET", f"/activities/{activity_id}/streams", params=params
        )
