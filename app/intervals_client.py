"""intervals.icu API client with API-key auth and rate limiting."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from config import settings

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; TrainingPulse/1.0; +https://github.com/trainingpulse)"
)


class RateLimitExceeded(Exception):
    """Raised when we need to wait for the rate limit window to reset."""

    def __init__(self, reset_after: float):
        self.reset_after = reset_after
        super().__init__(f"Rate limit exceeded. Reset after {reset_after:.0f}s")


class IntervalsClient:
    """Async intervals.icu client using personal API key (Basic auth)."""

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or settings.INTERVALS_ICU_API_KEY).strip()
        if not self.api_key:
            raise ValueError("INTERVALS_ICU_API_KEY is not set")
        self._http = httpx.AsyncClient(
            timeout=60.0,
            headers={"User-Agent": USER_AGENT},
            auth=("API_KEY", self.api_key),
        )
        # Defaults match intervals.icu API-key budgets
        self._rate_limit_usage_15min = 0
        self._rate_limit_limit_15min = 2500
        self._rate_limit_usage_daily = 0
        self._rate_limit_limit_daily = 5000
        self._rate_limit_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _parse_pair(header_value: str) -> tuple[int, int] | None:
        if not header_value:
            return None
        try:
            short_str, daily_str = header_value.split(",", 1)
            return int(short_str.strip()), int(daily_str.strip())
        except (ValueError, IndexError):
            return None

    def _update_rate_limits(self, response: httpx.Response) -> None:
        # intervals.icu: X-RateLimit-Limit / Remaining as "15m,daily"
        limit = self._parse_pair(response.headers.get("X-RateLimit-Limit", ""))
        remaining = self._parse_pair(response.headers.get("X-RateLimit-Remaining", ""))
        if limit is not None:
            self._rate_limit_limit_15min, self._rate_limit_limit_daily = limit
        if remaining is not None and limit is not None:
            rem_15, rem_daily = remaining
            self._rate_limit_usage_15min = max(0, limit[0] - rem_15)
            self._rate_limit_usage_daily = max(0, limit[1] - rem_daily)

    @property
    def rate_limit_remaining_15min(self) -> int:
        return self._rate_limit_limit_15min - self._rate_limit_usage_15min

    @property
    def rate_limit_remaining_daily(self) -> int:
        return self._rate_limit_limit_daily - self._rate_limit_usage_daily

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_limit_lock:
            if self.rate_limit_remaining_15min <= 5:
                wait_time = 60.0
                logger.warning(
                    "Approaching intervals.icu 15-min rate limit (%d/%d). Sleeping %.0fs...",
                    self._rate_limit_usage_15min,
                    self._rate_limit_limit_15min,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                self._rate_limit_usage_15min = 0

            if self.rate_limit_remaining_daily <= 20:
                logger.error(
                    "Approaching intervals.icu daily rate limit (%d/%d). Pausing sync.",
                    self._rate_limit_usage_daily,
                    self._rate_limit_limit_daily,
                )
                raise RateLimitExceeded(reset_after=3600)

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> dict | list | None:
        await self._wait_for_rate_limit()
        base = settings.INTERVALS_ICU_API_BASE.rstrip("/")
        url = f"{base}{path}"
        response = await self._http.request(method, url, **kwargs)
        self._update_rate_limits(response)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            reset = float(retry_after) if retry_after else 900.0
            logger.warning("Got 429 from intervals.icu (Retry-After=%s)", reset)
            raise RateLimitExceeded(reset_after=reset)

        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get_athlete(self) -> dict:
        """GET /athlete/0 — authenticated athlete profile."""
        data = await self._request("GET", "/athlete/0")
        if not isinstance(data, dict):
            raise ValueError("Unexpected athlete response")
        return data

    async def get_sport_settings(self) -> list | dict:
        """GET /athlete/0/sport-settings — FTP, HR zones per sport."""
        data = await self._request("GET", "/athlete/0/sport-settings")
        return data if data is not None else []

    async def list_activities(
        self,
        oldest: str,
        newest: str,
        *,
        limit: int | None = None,
    ) -> list[dict]:
        """List activities in [oldest, newest] (YYYY-MM-DD)."""
        params: dict = {"oldest": oldest, "newest": newest}
        if limit is not None:
            params["limit"] = limit
        data = await self._request(
            "GET", "/athlete/0/activities", params=params
        )
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError("Unexpected activities list response")
        return data

    async def get_activity(self, activity_id: str, *, intervals: bool = False) -> dict:
        """GET /activity/{id}."""
        params = {"intervals": "true"} if intervals else None
        # Strip provider prefix if present
        raw_id = activity_id.removeprefix("icu_").removeprefix("icu:")
        data = await self._request(
            "GET", f"/activity/{raw_id}", params=params
        )
        if not isinstance(data, dict):
            raise ValueError("Unexpected activity response")
        return data

    async def get_activity_streams(self, activity_id: str) -> dict:
        """GET /activity/{id}/streams.json — keyed stream arrays."""
        raw_id = activity_id.removeprefix("icu_").removeprefix("icu:")
        data = await self._request("GET", f"/activity/{raw_id}/streams.json")
        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            # Some responses are a list of {type, data} objects
            out: dict = {}
            for item in data:
                if isinstance(item, dict) and "type" in item:
                    out[item["type"]] = item
            return out
        return {}
