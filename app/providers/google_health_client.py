"""Google Health API client: exercises, sleep, daily wellness, and samples."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import OAuthToken
from providers.base import RequestPacer, SourceRateLimitExceeded

logger = logging.getLogger(__name__)

SOURCE_NAME = "google_health"
PROVIDER = "google_health"

DEFAULT_API_BASE = "https://health.googleapis.com/v4"

# Endpoints and scopes are properties of the provider, not deployment config.
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

_SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."
SCOPE_ACTIVITY = _SCOPE_PREFIX + "activity_and_fitness.readonly"
SCOPE_HEALTH_METRICS = _SCOPE_PREFIX + "health_metrics_and_measurements.readonly"
SCOPE_SLEEP = _SCOPE_PREFIX + "sleep.readonly"
# GPS routes are only available through the TCX export, which needs it.
SCOPE_LOCATION = _SCOPE_PREFIX + "location.readonly"

#: What each imported kind needs. Exercises read heart rate for streams;
#: wellness reads steps and calories, which are activity data.
KIND_SCOPES: dict[str, frozenset[str]] = {
    "activities": frozenset({SCOPE_ACTIVITY, SCOPE_HEALTH_METRICS, SCOPE_LOCATION}),
    "sleep": frozenset({SCOPE_SLEEP}),
    "wellness": frozenset({SCOPE_HEALTH_METRICS, SCOPE_ACTIVITY}),
    "intraday": frozenset({SCOPE_HEALTH_METRICS}),
}

#: Granted to every token stored before scopes were recorded.
LEGACY_SCOPES = frozenset({SCOPE_ACTIVITY, SCOPE_HEALTH_METRICS, SCOPE_LOCATION})

#: Max days per ``rollUp`` request for heart rate and per ``dailyRollUp``
#: request for total calories; other daily rollups allow 90.
#: Google allows 300 requests a minute per user; stay at 240 so a long
#: backfill never trips it.
_PACER = RequestPacer(0.25)

#: Seconds to wait before each retry of a 5xx; None ends the attempts.
_SERVER_ERROR_RETRY_DELAYS = (1.0, 4.0, None)
_RETRIED_STATUSES = frozenset({500, 502, 503, 504})

_SHORT_ROLLUP_DAYS = 14
_DAILY_ROLLUP_DAYS = 90


def scopes_for(kinds: Iterable[str]) -> str:
    """Space-separated scopes to request for the imported ``kinds``."""
    needed: set[str] = set()
    for kind in kinds:
        needed |= KIND_SCOPES.get(str(kind), frozenset())
    return " ".join(sorted(needed))


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

    def authorization_url(self, *, scopes: str, state: str = "google_health") -> str:
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": scopes,
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
            scopes=token_payload.get("scope"),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["provider"],
            set_={
                "access_token": stmt.excluded.access_token,
                "refresh_token": stmt.excluded.refresh_token,
                "expires_at": stmt.excluded.expires_at,
                "external_user_id": stmt.excluded.external_user_id,
                "scopes": stmt.excluded.scopes,
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

    async def granted_scopes(self) -> frozenset[str]:
        """Scopes of the stored token; empty when not connected."""
        token = await self._get_token()
        if token is None:
            return frozenset()
        if token.scopes is None:
            return LEGACY_SCOPES
        return frozenset(token.scopes.split())

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
                if data.get("scope"):
                    token.scopes = data["scope"]
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
        # Every call is a read, so a transient server error is safe to retry;
        # Google answered one 503 during a long backfill that a retry fixed.
        for delay in _SERVER_ERROR_RETRY_DELAYS:
            await _PACER.wait()
            response = await self._http.request(method, url, headers=headers, **kwargs)
            if response.status_code not in _RETRIED_STATUSES or delay is None:
                break
            logger.warning(
                "Google Health %s %s returned %d; retrying in %ss",
                method,
                path,
                response.status_code,
                delay,
            )
            await asyncio.sleep(delay)

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

    # ── Sleep and wellness ──────────────────────────────────────────
    #
    # Shapes and limits recorded against a real account (Fitbit Air data):
    # list pages hold at most 25 sleep sessions; rollUp takes 14 days of heart
    # rate per request (8,522 two-minute windows fit one page); dailyRollUp
    # takes 90 days, or 14 for total calories.

    async def list_sleep(self, first: date, last: date) -> list[dict]:
        """Sleep sessions (main sleeps and naps) ending on local days first..last."""
        return await self._list_data_points(
            "sleep",
            f'sleep.interval.civil_end_time >= "{first.isoformat()}" AND '
            f'sleep.interval.civil_end_time < "{(last + timedelta(days=1)).isoformat()}"',
            25,
        )

    async def list_daily(self, data_type: str, first: date, last: date) -> list[dict]:
        """Points of a daily data type (``daily-resting-heart-rate``, ...)."""
        field = data_type.replace("-", "_")
        return await self._list_data_points(
            data_type,
            f'{field}.date >= "{first.isoformat()}" AND '
            f'{field}.date < "{(last + timedelta(days=1)).isoformat()}"',
            1440,
        )

    async def daily_rollup(self, data_type: str, first: date, last: date) -> list[dict]:
        """Google's own per-day totals, de-duplicated across sources."""
        limit = (
            _SHORT_ROLLUP_DAYS if data_type == "total-calories" else _DAILY_ROLLUP_DAYS
        )
        out: list[dict] = []
        current = first
        while current <= last:
            chunk_end = min(current + timedelta(days=limit - 1), last)
            body = {
                "range": {
                    "start": {"date": _civil_date(current), "time": {}},
                    "end": {
                        "date": _civil_date(chunk_end + timedelta(days=1)),
                        "time": {},
                    },
                },
                "windowSizeDays": 1,
            }
            out.extend(
                await self._paged_post(
                    f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", body
                )
            )
            current = chunk_end + timedelta(days=1)
        return out

    async def heart_rate_rollup(
        self, start: datetime, end: datetime, *, window_seconds: int
    ) -> list[dict]:
        """Heart-rate averages over ``window_seconds`` windows in ``[start, end)``."""
        out: list[dict] = []
        current = _as_utc(start)
        end = _as_utc(end)
        while current < end:
            chunk_end = min(current + timedelta(days=_SHORT_ROLLUP_DAYS), end)
            body = {
                "range": {
                    "startTime": _rfc3339(current),
                    "endTime": _rfc3339(chunk_end),
                },
                "windowSize": f"{window_seconds}s",
                "pageSize": 10000,
            }
            out.extend(
                await self._paged_post(
                    "/users/me/dataTypes/heart-rate/dataPoints:rollUp", body
                )
            )
            current = chunk_end
        return out

    async def list_samples(
        self, data_type: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Sample points (``heart-rate-variability``, ``oxygen-saturation``)."""
        field = data_type.replace("-", "_") + ".sample_time.physical_time"
        return await self._list_data_points(
            data_type,
            f'{field} >= "{_rfc3339(start)}" AND {field} < "{_rfc3339(end)}"',
            10000,
        )

    async def _paged_post(self, path: str, body: dict) -> list[dict]:
        out: list[dict] = []
        page_token: str | None = None
        while True:
            request = {**body, **({"pageToken": page_token} if page_token else {})}
            data = await self._request("POST", path, json=request)
            if not isinstance(data, dict):
                break
            out.extend(data.get("rollupDataPoints") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out


def _civil_date(day: date) -> dict:
    return {"year": day.year, "month": day.month, "day": day.day}


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
