"""Google Health API client (exercise sessions + HR streams)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models import OAuthToken

logger = logging.getLogger(__name__)

PROVIDER = "google_health"


class RateLimitExceeded(Exception):
    def __init__(self, reset_after: float = 60.0):
        self.reset_after = reset_after
        super().__init__(f"Rate limit exceeded. Reset after {reset_after:.0f}s")


class GoogleHealthClient:
    """Async Google Health client with OAuth refresh for Google Health exercise data."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._http = httpx.AsyncClient(timeout=60.0)
        self._token_lock = asyncio.Lock()
        self._rate_limit_usage_15min = 0
        self._rate_limit_limit_15min = 1000
        self._rate_limit_usage_daily = 0
        self._rate_limit_limit_daily = 10000

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def authorization_url(state: str = "google_health") -> str:
        params = {
            "client_id": settings.GOOGLE_HEALTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.google_oauth_callback_url,
            "scope": settings.GOOGLE_HEALTH_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
        }
        return f"{settings.GOOGLE_OAUTH_AUTH_URL}?{urlencode(params)}"

    @staticmethod
    async def exchange_code(code: str) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                settings.GOOGLE_OAUTH_TOKEN_URL,
                data={
                    "client_id": settings.GOOGLE_HEALTH_CLIENT_ID,
                    "client_secret": settings.GOOGLE_HEALTH_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.google_oauth_callback_url,
                },
            )
            response.raise_for_status()
            return response.json()

    async def store_tokens(self, token_payload: dict, external_user_id: str | None = None) -> None:
        expires_in = int(token_payload.get("expires_in", 3600))
        expires_at = int(time.time()) + expires_in
        refresh = token_payload.get("refresh_token")
        if not refresh:
            # Re-consent without a new refresh token: keep existing refresh if any
            existing = await self._get_token()
            if existing is None:
                raise ValueError("Google OAuth response missing refresh_token")
            refresh = existing.refresh_token

        stmt = pg_insert(OAuthToken).values(
            provider=PROVIDER,
            access_token=token_payload["access_token"],
            refresh_token=refresh,
            expires_at=expires_at,
            external_user_id=external_user_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["provider"],
            set_={
                "access_token": stmt.excluded.access_token,
                "refresh_token": stmt.excluded.refresh_token,
                "expires_at": stmt.excluded.expires_at,
                "external_user_id": stmt.excluded.external_user_id,
            },
        )
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

    async def _ensure_valid_token(self) -> str:
        async with self._token_lock:
            token = await self._get_token()
            if token is None:
                raise ValueError(
                    "Google Health not connected. Authorize from the home page."
                )

            if token.expires_at < time.time() + 300:
                logger.info("Google Health access token expiring; refreshing...")
                response = await self._http.post(
                    settings.GOOGLE_OAUTH_TOKEN_URL,
                    data={
                        "client_id": settings.GOOGLE_HEALTH_CLIENT_ID,
                        "client_secret": settings.GOOGLE_HEALTH_CLIENT_SECRET,
                        "grant_type": "refresh_token",
                        "refresh_token": token.refresh_token,
                    },
                )
                if response.status_code != 200:
                    logger.error("Token refresh failed: %s", response.text)
                    raise ValueError(
                        "Google Health token refresh failed; reconnect Google Health from the home page."
                    )
                data = response.json()
                token.access_token = data["access_token"]
                if data.get("refresh_token"):
                    token.refresh_token = data["refresh_token"]
                token.expires_at = int(time.time()) + int(data.get("expires_in", 3600))
                await self.session.commit()
                logger.info("Google Health token refreshed")

            return token.access_token

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> dict | list | None:
        access_token = await self._ensure_valid_token()
        base = settings.GOOGLE_HEALTH_API_BASE.rstrip("/")
        url = path if path.startswith("http") else f"{base}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"
        response = await self._http.request(method, url, headers=headers, **kwargs)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            raise RateLimitExceeded(reset_after=float(retry_after))

        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        # TCX export may be XML
        content_type = response.headers.get("content-type", "")
        if "json" in content_type or response.text[:1] in ("{", "["):
            return response.json()
        return {"_raw": response.text, "_content_type": content_type}

    async def list_exercises(
        self,
        start: datetime,
        end: datetime,
        *,
        page_size: int = 100,
    ) -> list[dict]:
        """List exercise session data points in [start, end)."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        # Google Health list filter uses filter params on the collection
        parent = "users/me/dataTypes/exercise"
        params = {
            "filter": (
                f'time_range.start_time >= "{start.isoformat().replace("+00:00", "Z")}" AND '
                f'time_range.start_time < "{end.isoformat().replace("+00:00", "Z")}"'
            ),
            "pageSize": page_size,
        }
        # Prefer simpler query style used by many clients: startTime/endTime
        # Fall back path if filter is rejected
        out: list[dict] = []
        page_token: str | None = None
        while True:
            req_params = dict(params)
            if page_token:
                req_params["pageToken"] = page_token
            try:
                data = await self._request(
                    "GET", f"/{parent}/dataPoints", params=req_params
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    # Alternate query form used in some Health API examples
                    alt = {
                        "startTime": start.isoformat().replace("+00:00", "Z"),
                        "endTime": end.isoformat().replace("+00:00", "Z"),
                        "pageSize": page_size,
                    }
                    if page_token:
                        alt["pageToken"] = page_token
                    data = await self._request(
                        "GET", f"/{parent}/dataPoints", params=alt
                    )
                else:
                    raise

            if not isinstance(data, dict):
                break
            points = data.get("dataPoints") or data.get("data_points") or []
            out.extend(points)
            page_token = data.get("nextPageToken") or data.get("next_page_token")
            if not page_token:
                break
        return out

    async def get_exercise(self, name: str) -> dict:
        """GET a single exercise data point by resource name."""
        path = name if name.startswith("users/") else f"users/me/dataTypes/exercise/dataPoints/{name}"
        if not path.startswith("/"):
            path = "/" + path
        data = await self._request("GET", path)
        if not isinstance(data, dict):
            raise ValueError("Unexpected exercise response")
        return data

    async def list_heart_rate(
        self, start: datetime, end: datetime, *, page_size: int = 1000
    ) -> list[dict]:
        """List heart-rate samples in [start, end)."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        parent = "users/me/dataTypes/heart-rate"
        out: list[dict] = []
        page_token: str | None = None
        while True:
            params = {
                "startTime": start.isoformat().replace("+00:00", "Z"),
                "endTime": end.isoformat().replace("+00:00", "Z"),
                "pageSize": page_size,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                data = await self._request(
                    "GET", f"/{parent}/dataPoints", params=params
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    params = {
                        "filter": (
                            f'time_range.start_time >= "{start.isoformat().replace("+00:00", "Z")}" AND '
                            f'time_range.start_time < "{end.isoformat().replace("+00:00", "Z")}"'
                        ),
                        "pageSize": page_size,
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    data = await self._request(
                        "GET", f"/{parent}/dataPoints", params=params
                    )
                else:
                    raise

            if not isinstance(data, dict):
                break
            points = data.get("dataPoints") or data.get("data_points") or []
            out.extend(points)
            page_token = data.get("nextPageToken") or data.get("next_page_token")
            if not page_token:
                break
        return out
