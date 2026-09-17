"""Hammerhead API client with OAuth2 token handling and pagination.

Endpoints and scopes confirmed against the live OpenAPI spec published at
https://api.hammerhead.io/v1/docs/openapi.yml. Unlike Strava, Hammerhead's
token response has no ``expires_at`` (only ``expires_in``), its authorization
code exchange requires ``redirect_uri`` in the token request body, and it
documents no rate-limit response headers at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import OAuthToken
from sources.base import SourceRateLimitExceeded

logger = logging.getLogger(__name__)

SOURCE_NAME = "hammerhead"
PROVIDER = "hammerhead"

DEFAULT_API_BASE = "https://api.hammerhead.io/v1/api"

# Endpoints and scopes are properties of the provider, not deployment config.
OAUTH_AUTH_URL = "https://api.hammerhead.io/v1/auth/oauth/authorize"
OAUTH_TOKEN_URL = "https://api.hammerhead.io/v1/auth/oauth/token"
OAUTH_DEAUTHORIZE_URL = "https://api.hammerhead.io/v1/auth/oauth/deauthorize"
SCOPES = "activity:read"

#: No 429 has ever been documented for this API; used only as a defensive
#: default when one shows up without a ``Retry-After`` header.
_DEFAULT_RATE_LIMIT_RESET = 60.0

#: Caps how much of a single ``.fit`` download this client will buffer.
#: Enforced by streaming the response and counting bytes as they arrive,
#: rather than buffering the whole body first, so an oversized or runaway
#: response is aborted instead of fully read into memory. Kept independent of
#: (but the same order of magnitude as) ``fitfiles.store.MAX_RAW_DATA_BYTES``,
#: which enforces its own limit at the storage layer.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class FitDownloadTooLargeError(ValueError):
    """Raised when a ``.fit`` download exceeds :data:`MAX_DOWNLOAD_BYTES`."""


class HammerheadClient:
    """Async Hammerhead API client with auto token refresh."""

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
        # Serializes token refresh so concurrent callers don't double-refresh.
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ── OAuth ───────────────────────────────────────────────────────

    def authorization_url(self, state: str = "hammerhead") -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
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
                "redirect_uri": self.redirect_uri,
            },
        )
        response.raise_for_status()
        return response.json()

    async def store_tokens(self, token_payload: dict) -> None:
        """Persist an authorization-code or refresh response.

        Hammerhead always returns a refresh token, but we fall back to the
        stored one anyway so a malformed response cannot lock the integration
        out.
        """
        refresh = token_payload.get("refresh_token")
        if not refresh:
            existing = await self._get_token()
            if existing is None:
                raise ValueError("Hammerhead OAuth response missing refresh_token")
            refresh = existing.refresh_token

        raw_user_id = token_payload.get("user_id")
        external_user_id = str(raw_user_id) if raw_user_id is not None else None

        # Unlike Strava, Hammerhead's token response has no ``expires_at`` —
        # only ``expires_in`` (seconds from now).
        expires_at = int(time.time()) + int(token_payload.get("expires_in", 3600))

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

    async def get_external_user_id(self) -> str | None:
        """The Hammerhead user id captured at token-exchange time, if any."""
        token = await self._get_token()
        return token.external_user_id if token is not None else None

    async def disconnect(self) -> None:
        token = await self._get_token()
        if token is None:
            return
        try:
            response = await self._http.post(
                OAUTH_DEAUTHORIZE_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "token": token.access_token,
                },
            )
            if response.status_code >= 400:
                logger.warning(
                    "Hammerhead deauthorize returned HTTP %d; removing local "
                    "token anyway",
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - local disconnect must succeed
            logger.warning("Hammerhead deauthorize request failed: %s", exc)
        await self.session.delete(token)
        await self.session.commit()

    async def _ensure_valid_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        async with self._token_lock:
            token = await self._get_token()
            if token is None:
                raise ValueError(
                    "Hammerhead not connected. Authorize from the home page."
                )

            # Refresh if the token expires in less than 5 minutes.
            if token.expires_at < time.time() + 300:
                logger.info("Hammerhead access token expiring; refreshing...")
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
                    logger.error(
                        "Hammerhead token refresh failed: %s", response.text
                    )
                    raise ValueError(
                        "Hammerhead token refresh failed; reconnect Hammerhead "
                        "from the home page."
                    )
                data = response.json()
                token.access_token = data["access_token"]
                if data.get("refresh_token"):
                    token.refresh_token = data["refresh_token"]
                token.expires_at = int(time.time()) + int(
                    data.get("expires_in", 3600)
                )
                await self.session.commit()
                logger.info("Hammerhead token refreshed")

            return token.access_token

    # ── API methods ─────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make an authenticated request to the Hammerhead API."""
        access_token = await self._ensure_valid_token()

        url = f"{self.api_base}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"

        response = await self._http.request(method, url, headers=headers, **kwargs)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            reset = float(retry_after) if retry_after else _DEFAULT_RATE_LIMIT_RESET
            logger.warning("Got 429 from Hammerhead (Retry-After=%s)", retry_after)
            raise SourceRateLimitExceeded(source=SOURCE_NAME, reset_after=reset)

        response.raise_for_status()
        return response

    async def get_activities(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        start_date: date | None = None,
    ) -> dict:
        """Get one page of the athlete's activity summaries.

        Returns the raw pagination envelope
        (``{data, totalItems, totalPages, perPage, currentPage}``) so the
        caller can decide when it has paged through everything.
        """
        params: dict = {"page": page, "perPage": per_page}
        if start_date is not None:
            params["startDate"] = start_date.isoformat()
        response = await self._request("GET", "/activities", params=params)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected activities list response")
        return data

    async def get_activity(self, activity_id: str) -> dict:
        """Get a single activity (detailed representation)."""
        response = await self._request("GET", f"/activities/{activity_id}")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected activity response")
        return data

    async def get_activity_fit_file(self, activity_id: str) -> bytes:
        """Get the raw ``.fit`` file bytes for a single activity.

        Streamed with a running size cap rather than going through
        :meth:`_request` (which fully buffers the response before returning),
        so an oversized or runaway response is aborted before it is ever
        fully held in memory.
        """
        access_token = await self._ensure_valid_token()
        url = f"{self.api_base}/activities/{activity_id}/file"
        headers = {"Authorization": f"Bearer {access_token}"}

        chunks: list[bytes] = []
        total = 0
        async with self._http.stream("GET", url, headers=headers) as response:
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                reset = (
                    float(retry_after) if retry_after else _DEFAULT_RATE_LIMIT_RESET
                )
                logger.warning(
                    "Got 429 from Hammerhead fetching FIT file (Retry-After=%s)",
                    retry_after,
                )
                raise SourceRateLimitExceeded(source=SOURCE_NAME, reset_after=reset)
            response.raise_for_status()

            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise FitDownloadTooLargeError(
                        f"Hammerhead FIT file for activity {activity_id!r} "
                        f"exceeds the {MAX_DOWNLOAD_BYTES}-byte limit; "
                        "aborting download"
                    )
                chunks.append(chunk)

        return b"".join(chunks)
