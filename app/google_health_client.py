"""Google Health API client (exercise sessions + HR streams)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import OAuthToken
from sources.base import SourceRateLimitExceeded

logger = logging.getLogger(__name__)

SOURCE_NAME = "google_health"
PROVIDER = "google_health"

DEFAULT_API_BASE = "https://health.googleapis.com/v4"

# Endpoints and scopes are properties of the provider, not deployment config.
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
        "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
        # GPS routes are only available through the TCX export, which needs it.
        "https://www.googleapis.com/auth/googlehealth.location.readonly",
    ]
)


class GoogleHealthClient:
    """Async Google Health client with OAuth refresh for exercise data."""

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
        self._http = httpx.AsyncClient(timeout=60.0)
        self._token_lock = asyncio.Lock()
        # Set once the token turns out to lack the location scope, so a sync
        # does not ask (and warn) again for every activity.
        self._route_access_denied = False

    async def close(self) -> None:
        await self._http.aclose()

    # ── OAuth ───────────────────────────────────────────────────────

    def authorization_url(self, state: str = "google_health") -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
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

    async def store_tokens(
        self, token_payload: dict, external_user_id: str | None = None
    ) -> None:
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
                    OAUTH_TOKEN_URL,
                    data={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": token.refresh_token,
                    },
                )
                if response.status_code != 200:
                    logger.error("Token refresh failed: %s", response.text)
                    raise ValueError(
                        "Google Health token refresh failed; reconnect Google Health "
                        "from the home page."
                    )
                data = response.json()
                token.access_token = data["access_token"]
                if data.get("refresh_token"):
                    token.refresh_token = data["refresh_token"]
                token.expires_at = int(time.time()) + int(data.get("expires_in", 3600))
                await self.session.commit()
                logger.info("Google Health token refreshed")

            return token.access_token

    # ── Requests ────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        access_token = await self._ensure_valid_token()
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {access_token}"
        response = await self._http.request(method, url, headers=headers, **kwargs)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            raise SourceRateLimitExceeded(
                source=SOURCE_NAME, reset_after=float(retry_after)
            )

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
        page_size: int = 25,
    ) -> list[dict]:
        """List exercise session data points in [start, end)."""
        start = _as_utc(start)
        end = _as_utc(end)

        # Sessions only accept a civil (local wall-clock) start-time filter, so
        # widen the window by a day on each side to cover any UTC offset, then
        # trim to [start, end) on the physical start time. The API caps
        # exercise pages at 25.
        civil_from = (start - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        civil_to = (end + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        points = await self._list_data_points(
            "exercise",
            f'exercise.interval.civil_start_time >= "{civil_from}" AND '
            f'exercise.interval.civil_start_time < "{civil_to}"',
            min(page_size, 25),
        )
        out: list[dict] = []
        for point in points:
            interval = (point.get("exercise") or {}).get("interval") or {}
            started = _parse_rfc3339(interval.get("startTime"))
            if started is None or start <= started < end:
                out.append(point)
        return out

    async def get_exercise(self, name: str) -> dict:
        """GET a single exercise data point by resource name."""
        path = (
            name
            if name.startswith("users/")
            else f"users/me/dataTypes/exercise/dataPoints/{name}"
        )
        if not path.startswith("/"):
            path = "/" + path
        data = await self._request("GET", path)
        if not isinstance(data, dict):
            raise ValueError("Unexpected exercise response")
        return data

    async def export_exercise_tcx(self, data_point_id: str) -> str | None:
        """TCX document of one exercise, or None when it has no GPS track."""
        if self._route_access_denied:
            return None
        access_token = await self._ensure_valid_token()
        response = await self._http.get(
            f"{self.api_base}/users/me/dataTypes/exercise/dataPoints/"
            f"{data_point_id}:exportExerciseTcx",
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 429:
            raise SourceRateLimitExceeded(
                source=SOURCE_NAME,
                reset_after=float(response.headers.get("Retry-After", "60")),
            )
        if response.status_code == 403:
            self._route_access_denied = True
            logger.warning(
                "Google Health TCX export denied (%s); reconnect Google Health "
                "to grant location access",
                response.text[:200],
            )
            return None
        if response.status_code in (400, 404):
            return None
        if response.status_code >= 500:
            # Seen for sessions without GPS; the heart-rate stream still works.
            logger.warning(
                "Google Health TCX export for %s failed with %s; "
                "keeping heart rate only",
                data_point_id,
                response.status_code,
            )
            return None
        response.raise_for_status()
        return response.text

    async def list_heart_rate(
        self, start: datetime, end: datetime, *, page_size: int = 10000
    ) -> list[dict]:
        """List heart-rate samples in [start, end)."""
        return await self._list_data_points(
            "heart-rate",
            f'heart_rate.sample_time.physical_time >= "{_rfc3339(start)}" AND '
            f'heart_rate.sample_time.physical_time < "{_rfc3339(end)}"',
            page_size,
        )

    async def _list_data_points(
        self, data_type: str, filter_expr: str, page_size: int
    ) -> list[dict]:
        out: list[dict] = []
        page_token: str | None = None
        while True:
            params: dict = {"filter": filter_expr, "pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            data = await self._request(
                "GET", f"/users/me/dataTypes/{data_type}/dataPoints", params=params
            )
            if not isinstance(data, dict):
                break
            out.extend(data.get("dataPoints") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
