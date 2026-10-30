"""Garmin Connect Personal API client.

Endpoints, the token exchange, and the base URL follow Garmin's own Python SDK
(https://github.com/garmin/connect-personal-api-python). The Personal API is
read-only and pull-only. A Personal API key, created by the user in Garmin
Connect's settings, is exchanged for a JWT bearer token valid for 24 hours (or
until the key expires). There are no refresh tokens: a new bearer token is
simply requested with the same key. Garmin documents no rate limits and no
rate-limit headers; in practice it answered HTTP 429 with ``Retry-After: 41``
after about 100 requests in 17 seconds, so requests are spaced out (see
:class:`providers.base.RequestPacer`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import time
import zipfile
from datetime import date

import httpx

from providers.base import RequestPacer, SourceRateLimitExceeded

logger = logging.getLogger(__name__)

SOURCE_NAME = "garmin"

DEFAULT_API_BASE = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

#: Garmin's SDK requests a new bearer token once fewer than this many seconds
#: remain; we do the same.
_TOKEN_REFRESH_WINDOW = 60 * 60

#: Used when a 429 arrives without ``Retry-After``. Garmin documents no limits.
_DEFAULT_RATE_LIMIT_RESET = 900.0

#: Minimum gap between requests: about 90 a minute, below the burst at which
#: Garmin started answering 429.
_MIN_REQUEST_INTERVAL = 0.65

#: A 429 whose ``Retry-After`` is at most this long is waited out and retried
#: once; a longer one pauses the provider until the next sync.
_MAX_INLINE_RETRY_AFTER = 120.0

#: Caps how much of one activity download this client will buffer. Same order
#: of magnitude as ``fitfiles.store.MAX_RAW_DATA_BYTES``.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


class GarminAuthError(ValueError):
    """Raised when Garmin rejects the configured Personal API key."""


class FitDownloadTooLargeError(ValueError):
    """Raised when an activity download exceeds :data:`MAX_DOWNLOAD_BYTES`."""


def jwt_expiration(token: str) -> float:
    """The ``exp`` claim of a JWT, read without verifying its signature."""
    try:
        encoded = token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        return float(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise GarminAuthError(
            "Garmin's token endpoint did not return a JWT with an exp claim"
        ) from exc


def extract_fit(archive: bytes) -> bytes:
    """Return the ``.fit`` member of Garmin's original-activity ZIP.

    Garmin wraps the original file in a ZIP. Activities uploaded from another
    format (GPX/TCX) contain no ``.fit`` member and raise ``ValueError``.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for info in bundle.infolist():
                if info.filename.lower().endswith(".fit"):
                    if info.file_size > MAX_DOWNLOAD_BYTES:
                        raise FitDownloadTooLargeError(
                            f"{info.filename} unpacks to {info.file_size} bytes"
                        )
                    return bundle.read(info)
    except zipfile.BadZipFile as exc:
        raise ValueError("Garmin activity download is not a ZIP archive") from exc
    raise ValueError("Garmin activity download contains no .fit file")


_PACER = RequestPacer(_MIN_REQUEST_INTERVAL)


#: Bearer tokens shared by every client in this process, keyed by
#: :func:`_token_cache_key`. The sync engine builds a fresh client for every
#: run, so without this each sync would exchange the key again. Kept in memory
#: only: a token is cheap to get again after a restart, and keying on the API
#: key means a replaced key never reuses the old key's token.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _token_cache_key(api_base: str, api_key: str) -> str:
    return hashlib.sha256(f"{api_base}\0{api_key}".encode()).hexdigest()


class GarminClient:
    """Async Garmin Personal API client that manages its own bearer token."""

    def __init__(self, api_key: str, *, api_base: str = DEFAULT_API_BASE):
        self._api_key = api_key
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self._cache_key = _token_cache_key(self.api_base, api_key)
        self._http = httpx.AsyncClient(timeout=30.0)
        # Serializes token exchange so concurrent callers don't double-fetch.
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    # ── Auth ────────────────────────────────────────────────────────

    def _forget_token(self) -> None:
        """Drop the cached token, e.g. after Garmin rejected it."""
        _TOKEN_CACHE.pop(self._cache_key, None)

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            cached = _TOKEN_CACHE.get(self._cache_key)
            if cached is not None and cached[1] - time.time() > _TOKEN_REFRESH_WINDOW:
                return cached[0]
            response = await self._http.post(
                f"{self.api_base}{TOKEN_PATH}",
                headers={"Accept": "*/*", "Authorization": f"ApiKey {self._api_key}"},
            )
            if response.status_code in (401, 403):
                raise GarminAuthError(
                    f"Garmin rejected the Personal API key (HTTP {response.status_code}). "
                    "It may have expired; create a new one in Garmin Connect and "
                    "update providers.garmin.api_key."
                )
            response.raise_for_status()
            token = response.text.strip()
            _TOKEN_CACHE[self._cache_key] = (token, jwt_expiration(token))
            logger.info("Garmin bearer token obtained")
            return token

    # ── Requests ────────────────────────────────────────────────────

    def _check_status(self, response: httpx.Response) -> None:
        """Handle the statuses that need more than ``raise_for_status``."""
        if response.status_code == 401:
            # Deleting the key revokes its tokens early; the next call must
            # exchange the key again instead of reusing the cached token.
            self._forget_token()
        if response.status_code == 429:
            raise SourceRateLimitExceeded(
                source=SOURCE_NAME, reset_after=_retry_after(response)
            )

    async def _get(self, path: str, *, params: dict | None = None):
        """GET a path and return decoded JSON, or ``None`` for an empty body.

        A short 429 is waited out and retried once, so a backfill keeps going
        instead of stopping until the next sync.
        """
        for attempt in (1, 2):
            token = await self._ensure_token()
            await _PACER.wait()
            response = await self._http.get(
                f"{self.api_base}{path}",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            if response.status_code == 429 and attempt == 1:
                wait = _retry_after(response)
                if wait <= _MAX_INLINE_RETRY_AFTER:
                    logger.warning("Got 429 from Garmin; retrying in %.0fs", wait)
                    await asyncio.sleep(wait)
                    continue
            break
        self._check_status(response)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    async def search_activities(
        self, *, start: int, limit: int, start_date: date, end_date: date
    ) -> list[dict]:
        """One page of activity summaries for an inclusive local-date range."""
        data = await self._get(
            "/activitylist-service/activities/search/activities",
            params={
                "start": start,
                "limit": limit,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
        )
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError("Unexpected Garmin activity search response")
        return data

    async def get_activity(self, activity_id: str) -> dict:
        data = await self._get(f"/activity-service/activity/{activity_id}")
        if not isinstance(data, dict):
            raise ValueError("Unexpected Garmin activity response")
        return data

    async def get_weight_measurements(self, start_date: date, end_date: date) -> dict:
        """Every weigh-in for an inclusive local-date range."""
        data = await self._get(
            f"/weight-service/weight/range/{start_date.isoformat()}/{end_date.isoformat()}",
            params={"includeAll": "true"},
        )
        return data if isinstance(data, dict) else {}

    # ── Wellness ────────────────────────────────────────────────────
    #
    # Range limits measured against the live API: sleep summaries reject
    # ranges over 31 days; daily summaries and HRV summaries accept a year.
    # The intraday endpoints take a single calendar date.

    async def get_sleep_summaries(self, start_date: date, end_date: date) -> list[dict]:
        """One sleep record per day in an inclusive range (at most 31 days).

        Days without sleep come back as an entry whose fields are all null.
        """
        data = await self._get(
            "/sleep-service/sleep/dailySleepSummariesByDate",
            params={
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
        )
        return data if isinstance(data, list) else []

    async def get_daily_summaries(self, start_date: date, end_date: date) -> list[dict]:
        """Daily wellness summaries for an inclusive range; days without data
        are left out."""
        data = await self._get(
            "/usersummary-service/v2/dailysummaries",
            params={
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
        )
        if not isinstance(data, dict):
            return []
        return data.get("data") or []

    async def get_hrv_summaries(self, start_date: date, end_date: date) -> list[dict]:
        """Overnight HRV summaries for an inclusive range."""
        data = await self._get(
            f"/hrv-service/hrv/daily/{start_date.isoformat()}/{end_date.isoformat()}"
        )
        if not isinstance(data, dict):
            return []
        return data.get("hrvSummaries") or []

    async def get_daily_heart_rate(self, day: date) -> dict:
        """Heart rate every two minutes for one local day."""
        data = await self._get(
            "/wellness-service/wellness/dailyHeartRate",
            params={"date": day.isoformat()},
        )
        return data if isinstance(data, dict) else {}

    async def get_hrv(self, day: date) -> dict:
        """The overnight HRV readings (every five minutes) ending on ``day``."""
        data = await self._get(f"/hrv-service/hrv/{day.isoformat()}")
        return data if isinstance(data, dict) else {}

    async def get_spo2(self, day: date) -> dict:
        """SpO2 for one local day: daily values plus hourly averages.

        Every value is null while Pulse Ox is off. ``spO2SingleValues`` stayed
        null on a CIRQA band even with Pulse Ox on during sleep.
        """
        data = await self._get(
            f"/wellness-service/wellness/daily/spo2acclimation/{day.isoformat()}"
        )
        return data if isinstance(data, dict) else {}

    async def download_activity(self, activity_id: str) -> bytes:
        """The original activity file as ZIP bytes, streamed with a size cap."""
        token = await self._ensure_token()
        url = f"{self.api_base}/download-service/files/activity/{activity_id}"
        headers = {
            "Accept": "application/zip, application/x-zip-compressed, "
            "application/octet-stream",
            "Authorization": f"Bearer {token}",
        }
        chunks: list[bytes] = []
        total = 0
        await _PACER.wait()
        async with self._http.stream("GET", url, headers=headers) as response:
            self._check_status(response)
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise FitDownloadTooLargeError(
                        f"Garmin download for activity {activity_id!r} exceeds "
                        f"the {MAX_DOWNLOAD_BYTES}-byte limit; aborting"
                    )
                chunks.append(chunk)
        return b"".join(chunks)


def _retry_after(response: httpx.Response) -> float:
    """Seconds Garmin asks to wait after a 429."""
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else _DEFAULT_RATE_LIMIT_RESET
    except ValueError:
        return _DEFAULT_RATE_LIMIT_RESET
