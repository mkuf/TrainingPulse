"""Strava API client with automatic token refresh and rate limiting."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import OAuthToken
from sources.base import SourceRateLimitExceeded

logger = logging.getLogger(__name__)

SOURCE_NAME = "strava"
PROVIDER = "strava"

DEFAULT_API_BASE = "https://www.strava.com/api/v3"

# Endpoints and scopes are properties of the provider, not deployment config.
OAUTH_AUTH_URL = "https://www.strava.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
SCOPES = "activity:read_all,profile:read_all"

DEFAULT_STREAM_KEYS = (
    "time,latlng,distance,altitude,heartrate,cadence,watts,temp,"
    "velocity_smooth,grade_smooth"
)


class StravaClient:
    """Async Strava API client with auto token refresh and rate limit handling."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        client_id: str = "",
        client_secret: str = "",
        api_base: str = DEFAULT_API_BASE,
        redirect_uri: str = "",
    ):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(timeout=30.0)
        # Strava publishes TWO rate-limit budgets per response:
        #   X-RateLimit-*: overall (read + write combined). Default 200/2000.
        #   X-ReadRateLimit-*: reads only. Default 100/1000.
        # Almost everything we do is a read, so the read budget is what
        # actually triggers 429s. We track both and gate on whichever is
        # closer to its limit.
        self._rate_limit_usage_15min = 0
        self._rate_limit_limit_15min = 200
        self._rate_limit_usage_daily = 0
        self._rate_limit_limit_daily = 2000
        self._read_rate_limit_usage_15min = 0
        self._read_rate_limit_limit_15min = 100
        self._read_rate_limit_usage_daily = 0
        self._read_rate_limit_limit_daily = 1000
        self._rate_limit_seen = False
        self._gear_cache: dict[str, str | None] = {}
        # Serializes the rate-limit check so concurrent workers don't all read
        # "remaining=4" simultaneously and overshoot the 15-min window.
        self._rate_limit_lock = asyncio.Lock()
        # Serializes token refresh so concurrent callers don't double-refresh.
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ── OAuth ───────────────────────────────────────────────────────

    def authorization_url(self, state: str = "strava") -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "approval_prompt": "auto",
            "scope": SCOPES,
            "state": state,
        }
        return f"{OAUTH_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        response = await self._http.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()

    async def store_tokens(self, token_payload: dict) -> None:
        """Persist an authorization-code or refresh response.

        Strava always returns a refresh token, but we fall back to the stored
        one anyway so a malformed response cannot lock the integration out.
        """
        refresh = token_payload.get("refresh_token")
        if not refresh:
            existing = await self._get_token()
            if existing is None:
                raise ValueError("Strava OAuth response missing refresh_token")
            refresh = existing.refresh_token

        athlete = token_payload.get("athlete")
        external_user_id = None
        if isinstance(athlete, dict) and athlete.get("id") is not None:
            external_user_id = str(athlete["id"])

        expires_at = token_payload.get("expires_at")
        if expires_at is None:
            expires_at = int(time.time()) + int(token_payload.get("expires_in", 21600))

        stmt = pg_insert(OAuthToken).values(
            provider=PROVIDER,
            access_token=token_payload["access_token"],
            refresh_token=refresh,
            expires_at=int(expires_at),
            external_user_id=external_user_id,
        )
        set_ = {
            "access_token": stmt.excluded.access_token,
            "refresh_token": stmt.excluded.refresh_token,
            "expires_at": stmt.excluded.expires_at,
        }
        if external_user_id is not None:
            set_["external_user_id"] = stmt.excluded.external_user_id
        stmt = stmt.on_conflict_do_update(index_elements=["provider"], set_=set_)
        await self.session.execute(stmt)
        await self.session.commit()

    async def _get_token(self) -> OAuthToken | None:
        result = await self.session.execute(
            select(OAuthToken).where(OAuthToken.provider == PROVIDER).limit(1)
        )
        return result.scalar_one_or_none()

    async def is_connected(self) -> bool:
        return (await self._get_token()) is not None

    async def disconnect(self) -> None:
        token = await self._get_token()
        if token is not None:
            await self.session.delete(token)
            await self.session.commit()

    async def stored_athlete_id(self) -> int | None:
        token = await self._get_token()
        if token is None or not token.external_user_id:
            return None
        try:
            return int(token.external_user_id)
        except ValueError:
            return None

    async def _ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        async with self._token_lock:
            token = await self._get_token()
            if token is None:
                raise ValueError(
                    "Strava not connected. Authorize from the home page."
                )

            # Refresh if the token expires in less than 5 minutes
            if token.expires_at < time.time() + 300:
                logger.info("Strava access token expiring; refreshing...")
                response = await self._http.post(
                    OAUTH_TOKEN_URL,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": token.refresh_token,
                    },
                )
                if response.status_code != 200:
                    logger.error("Strava token refresh failed: %s", response.text)
                    raise ValueError(
                        "Strava token refresh failed; reconnect Strava from the "
                        "home page."
                    )
                data = response.json()
                token.access_token = data["access_token"]
                if data.get("refresh_token"):
                    token.refresh_token = data["refresh_token"]
                token.expires_at = int(
                    data.get("expires_at")
                    or int(time.time()) + int(data.get("expires_in", 21600))
                )
                await self.session.commit()
                logger.info("Strava token refreshed")

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

    def _update_rate_limits(self, response: httpx.Response) -> None:
        """Parse Strava rate limit headers and update internal state.

        Missing headers leave the previous value intact.
        """
        overall_usage = self._parse_pair(response.headers.get("X-RateLimit-Usage", ""))
        overall_limit = self._parse_pair(response.headers.get("X-RateLimit-Limit", ""))
        if overall_usage is not None:
            self._rate_limit_usage_15min, self._rate_limit_usage_daily = overall_usage
            self._rate_limit_seen = True
        if overall_limit is not None:
            self._rate_limit_limit_15min, self._rate_limit_limit_daily = overall_limit

        read_usage = self._parse_pair(
            response.headers.get("X-ReadRateLimit-Usage", "")
        )
        read_limit = self._parse_pair(
            response.headers.get("X-ReadRateLimit-Limit", "")
        )
        if read_usage is not None:
            (
                self._read_rate_limit_usage_15min,
                self._read_rate_limit_usage_daily,
            ) = read_usage
            self._rate_limit_seen = True
        if read_limit is not None:
            (
                self._read_rate_limit_limit_15min,
                self._read_rate_limit_limit_daily,
            ) = read_limit

    @property
    def rate_limit_seen(self) -> bool:
        return self._rate_limit_seen

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

    @property
    def tightest_15min(self) -> tuple[int, int]:
        """(used, limit) for whichever 15-minute budget is closer to its cap."""
        overall_remaining = (
            self._rate_limit_limit_15min - self._rate_limit_usage_15min
        )
        read_remaining = (
            self._read_rate_limit_limit_15min - self._read_rate_limit_usage_15min
        )
        if read_remaining <= overall_remaining:
            return self._read_rate_limit_usage_15min, self._read_rate_limit_limit_15min
        return self._rate_limit_usage_15min, self._rate_limit_limit_15min

    @property
    def tightest_daily(self) -> tuple[int, int]:
        """(used, limit) for whichever daily budget is closer to its cap."""
        overall_remaining = (
            self._rate_limit_limit_daily - self._rate_limit_usage_daily
        )
        read_remaining = (
            self._read_rate_limit_limit_daily - self._read_rate_limit_usage_daily
        )
        if read_remaining <= overall_remaining:
            return self._read_rate_limit_usage_daily, self._read_rate_limit_limit_daily
        return self._rate_limit_usage_daily, self._rate_limit_limit_daily

    async def _wait_for_rate_limit(self) -> None:
        """If we're close to either rate limit, sleep until the window resets."""
        async with self._rate_limit_lock:
            if self.rate_limit_remaining_15min <= 5:
                # 15-minute windows reset at :00, :15, :30, :45
                now = time.time()
                current_minute = int(now // 60) % 15
                seconds_until_reset = (15 - current_minute) * 60 - (now % 60)
                wait_time = max(seconds_until_reset + 5, 10)  # 5s buffer
                logger.warning(
                    "Approaching Strava 15-min rate limit "
                    "(overall %d/%d, reads %d/%d). Sleeping %.0fs...",
                    self._rate_limit_usage_15min,
                    self._rate_limit_limit_15min,
                    self._read_rate_limit_usage_15min,
                    self._read_rate_limit_limit_15min,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                # Strava's next response rewrites these.
                self._rate_limit_usage_15min = 0
                self._read_rate_limit_usage_15min = 0

            if self.rate_limit_remaining_daily <= 20:
                logger.error(
                    "Approaching Strava daily rate limit "
                    "(overall %d/%d, reads %d/%d). Pausing sync.",
                    self._rate_limit_usage_daily,
                    self._rate_limit_limit_daily,
                    self._read_rate_limit_usage_daily,
                    self._read_rate_limit_limit_daily,
                )
                raise SourceRateLimitExceeded(source=SOURCE_NAME, reset_after=3600)

    # ── API methods ─────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        """Make an authenticated request to the Strava API."""
        await self._wait_for_rate_limit()
        access_token = await self._ensure_valid_token()

        url = f"{self.api_base}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"

        response = await self._http.request(method, url, headers=headers, **kwargs)
        self._update_rate_limits(response)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            reset = float(retry_after) if retry_after else 900.0
            logger.warning("Got 429 from Strava (Retry-After=%s)", retry_after)
            raise SourceRateLimitExceeded(source=SOURCE_NAME, reset_after=reset)

        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get_athlete(self) -> dict:
        """GET /athlete — used only to verify the token and read the athlete id."""
        data = await self._request("GET", "/athlete")
        if not isinstance(data, dict):
            raise ValueError("Unexpected athlete response")
        return data

    async def get_activities(
        self,
        *,
        page: int = 1,
        per_page: int = 200,
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict]:
        """Get a page of the athlete's activities."""
        params: dict = {"page": page, "per_page": per_page}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        data = await self._request("GET", "/athlete/activities", params=params)
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError("Unexpected activities list response")
        return data

    async def get_activity(self, activity_id: str) -> dict:
        """Get a single activity (detailed representation)."""
        data = await self._request("GET", f"/activities/{activity_id}")
        if not isinstance(data, dict):
            raise ValueError("Unexpected activity response")
        return data

    async def get_gear_display_name(self, gear_id: str) -> str | None:
        """Resolve bike/shoe label from GET /gear/{id}; cached per client lifetime."""
        gid = (gear_id or "").strip()
        if not gid or gid.lower() == "none":
            return None
        if gid in self._gear_cache:
            return self._gear_cache[gid]
        try:
            data = await self._request("GET", f"/gear/{gid}")
        except SourceRateLimitExceeded:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._gear_cache[gid] = None
                return None
            raise
        except Exception as e:
            logger.warning("Failed to fetch Strava gear %s: %s", gid, e)
            self._gear_cache[gid] = None
            return None
        if not isinstance(data, dict):
            self._gear_cache[gid] = None
            return None
        label = (data.get("nickname") or data.get("name") or "").strip() or None
        self._gear_cache[gid] = label
        return label

    async def get_activity_streams(
        self, activity_id: str, keys: str = DEFAULT_STREAM_KEYS
    ) -> dict:
        """Get stream data for an activity, keyed by stream type."""
        params = {"keys": keys, "key_by_type": "true"}
        data = await self._request(
            "GET", f"/activities/{activity_id}/streams", params=params
        )
        if isinstance(data, dict):
            return data
        return {}
