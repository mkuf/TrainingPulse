"""Withings API client with OAuth2 token handling.

Withings wraps every response in ``{"status": <int>, "body": {...}}`` and
reports most failures with HTTP 200 and a non-zero ``status``: 401 is an
invalid or expired token, 601 is "too many requests". Its refresh tokens
rotate, so every refresh response's ``refresh_token`` must be stored.

Tokens live in ``oauth_tokens`` with ``provider = 'withings'``; the Withings
user id goes in ``external_user_id``.
"""

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

PROVIDER = "withings"

# Endpoints and scopes are properties of the provider, not deployment config.
OAUTH_AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
OAUTH_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
SCOPES = "user.metrics,user.info"

#: ``status`` values in the response envelope.
_STATUS_OK = 0
_STATUS_INVALID_TOKEN = 401
_STATUS_TOO_MANY_REQUESTS = 601

#: Withings allows 120 requests per minute and documents no reset header.
_RATE_LIMIT_RESET = 60.0


class WithingsError(RuntimeError):
    """A non-zero ``status`` from the Withings API."""

    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(f"Withings status {status}: {message or 'request failed'}")


class WithingsClient:
    """Async Withings client with auto token refresh."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
    ):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(timeout=30.0)
        # Serializes token refresh: a rotated refresh token is single-use.
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ── OAuth ───────────────────────────────────────────────────────

    def authorization_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "state": state,
            "scope": SCOPES,
            "redirect_uri": self.redirect_uri,
        }
        return f"{OAUTH_AUTH_URL}?{urlencode(params)}"

    async def _token_call(self, data: dict) -> dict:
        response = await self._http.post(
            OAUTH_TOKEN_URL,
            data={
                "action": "requesttoken",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                **data,
            },
        )
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        if status != _STATUS_OK:
            raise WithingsError(int(status or -1), payload.get("error", ""))
        return payload["body"]

    async def exchange_code(self, code: str) -> dict:
        return await self._token_call(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )

    async def store_tokens(self, body: dict) -> None:
        """Persist an authorization-code or refresh response body."""
        raw_user_id = body.get("userid")
        external_user_id = str(raw_user_id) if raw_user_id is not None else None
        stmt = pg_insert(OAuthToken).values(
            provider=PROVIDER,
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=int(time.time()) + int(body.get("expires_in", 10800)),
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
        token = await self._get_token()
        return token.external_user_id if token is not None else None

    async def disconnect(self) -> None:
        """Forget the local token. Withings offers no revoke endpoint."""
        token = await self._get_token()
        if token is None:
            return
        await self.session.delete(token)
        await self.session.commit()

    async def _refresh(self, token: OAuthToken) -> None:
        logger.info("Withings access token expiring; refreshing...")
        try:
            body = await self._token_call(
                {"grant_type": "refresh_token", "refresh_token": token.refresh_token}
            )
        except (httpx.HTTPError, WithingsError) as exc:
            raise ValueError(
                "Withings token refresh failed; reconnect Withings from the "
                "home page."
            ) from exc
        token.access_token = body["access_token"]
        token.refresh_token = body.get("refresh_token", token.refresh_token)
        token.expires_at = int(time.time()) + int(body.get("expires_in", 10800))
        await self.session.commit()

    async def _ensure_valid_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            token = await self._get_token()
            if token is None:
                raise ValueError("Withings not connected. Authorize from the home page.")
            if force_refresh or token.expires_at < time.time() + 300:
                await self._refresh(token)
            return token.access_token

    # ── API ─────────────────────────────────────────────────────────

    async def _call(self, url: str, data: dict, *, retried: bool = False) -> dict:
        access_token = await self._ensure_valid_token(force_refresh=retried)
        response = await self._http.post(
            url, data=data, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code == 429:
            raise SourceRateLimitExceeded(source=PROVIDER, reset_after=_RATE_LIMIT_RESET)
        response.raise_for_status()
        payload = response.json()
        status = int(payload.get("status", -1))
        if status == _STATUS_OK:
            return payload.get("body") or {}
        if status == _STATUS_INVALID_TOKEN and not retried:
            # Revoked or expired early: refresh once, then give up.
            return await self._call(url, data, retried=True)
        if status == _STATUS_TOO_MANY_REQUESTS:
            raise SourceRateLimitExceeded(source=PROVIDER, reset_after=_RATE_LIMIT_RESET)
        raise WithingsError(status, payload.get("error", ""))

    async def get_measures(
        self,
        *,
        startdate: int,
        enddate: int,
        meastypes: str,
        offset: int | None = None,
    ) -> dict:
        """One page of ``getmeas``: real measurements only (``category=1``).

        Category 2 is the user's own goals, which must never be stored as
        weigh-ins.
        """
        data: dict = {
            "action": "getmeas",
            "meastypes": meastypes,
            "category": 1,
            "startdate": startdate,
            "enddate": enddate,
        }
        if offset is not None:
            data["offset"] = offset
        return await self._call(MEASURE_URL, data)
