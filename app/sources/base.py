"""Contracts shared by every activity source.

A source is a thin adapter over one provider's HTTP API. It knows how to
authenticate, how to list a date window, how to fetch one activity's detail and
streams, and how to map a provider payload onto ``activities`` columns. It knows
nothing about backfill, incremental sync, reconciliation, queue draining,
concurrency, or supersession — :mod:`sources.engine` owns all of that for every
source alike.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from fastapi import FastAPI

from catalog import AuthMethod, DataKind
from models import Activity

#: Canonical stream keys. Sources normalize provider-specific names onto these
#: so metrics code never has to care where a stream came from.
STREAM_TIME = "time"
STREAM_HEARTRATE = "heartrate"
STREAM_WATTS = "watts"
STREAM_CADENCE = "cadence"
STREAM_DISTANCE = "distance"
STREAM_ALTITUDE = "altitude"
STREAM_LATLNG = "latlng"
STREAM_VELOCITY = "velocity_smooth"
STREAM_TEMP = "temp"
STREAM_GRADE = "grade_smooth"


class SourceRateLimitExceeded(Exception):
    """Raised when a provider's rate budget is exhausted.

    The engine treats this as "pause this phase, resume on the next scheduled
    sync" rather than as a failure, and never lets it abort a different source.
    """

    def __init__(self, *, source: str, reset_after: float = 900.0):
        self.source = source
        self.reset_after = reset_after
        super().__init__(
            f"{source}: rate limit exceeded, resets in {reset_after:.0f}s"
        )


@dataclass
class RateLimitSnapshot:
    """A source's last-seen quota usage.

    ``known`` is False for providers that publish no usable quota headers, so
    the UI can say "not reported" instead of showing a misleading 0/0.
    """

    used_15min: int = 0
    limit_15min: int = 0
    used_daily: int = 0
    limit_daily: int = 0
    checked_at: datetime | None = None
    known: bool = True

    def as_payload(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "fifteen_min": {"used": self.used_15min, "limit": self.limit_15min},
            "daily": {"used": self.used_daily, "limit": self.limit_daily},
            "last_checked_at": (
                self.checked_at.isoformat() if self.checked_at else None
            ),
        }


def prefixed_id(prefix: str, raw: object) -> str:
    """Idempotently apply a source's activity-id prefix.

    Accepts values that are already prefixed, use a ``:`` separator, or are a
    bare provider id, and tolerates resource-name style paths.
    """
    value = str(raw).strip()
    if value.startswith(prefix):
        return value
    colon = prefix.rstrip("_") + ":"
    if value.startswith(colon):
        value = value[len(colon) :]
    if "/" in value:
        value = value.rstrip("/").split("/")[-1]
    return f"{prefix}{value}"


def external_id(prefix: str, activity_id: str) -> str:
    """Strip a source's prefix to recover the provider's own id."""
    value = str(activity_id).strip()
    if value.startswith(prefix):
        return value[len(prefix) :]
    colon = prefix.rstrip("_") + ":"
    if value.startswith(colon):
        return value[len(colon) :]
    return value


class OAuthStateGuard:
    """CSRF ``state`` handling for a single-tenant OAuth connect/callback pair.

    There is no user session to bind a CSRF token to, so the token is held in
    memory between the redirect to the provider and the callback. It is
    single-use (cleared on the first verification attempt, success or not) and
    expires quickly, keeping the window an attacker could exploit small: they
    would need to get their own authorization code into this server's
    callback before the legitimate flow that issued the token completes.
    """

    def __init__(self, *, ttl_seconds: float = 600.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._pending: tuple[str, float] | None = None

    def issue(self) -> str:
        """Generate and remember a fresh token for the next callback."""
        token = secrets.token_urlsafe(24)
        self._pending = (token, time.monotonic() + self._ttl_seconds)
        return token

    def verify(self, state: str | None) -> bool:
        """Check ``state`` against the last issued token, then forget it."""
        pending = self._pending
        self._pending = None
        if pending is None or not state:
            return False
        token, expires_at = pending
        if time.monotonic() > expires_at:
            return False
        return hmac.compare_digest(token, state)


@runtime_checkable
class ActivitySource(Protocol):
    """An activity provider that is its own activities feed.

    Satisfies both :class:`providers.base.Provider` and
    :class:`providers.feeds.ActivityFeed`; the engine works against this
    combined view because it needs the feed and the provider's rate limits.
    """

    #: Value stored in ``activities.source``.
    name: str
    auth: AuthMethod
    kind: DataKind
    #: Prefix applied to ``activities.id``, e.g. ``"strava_"``.
    id_prefix: str
    #: True when the provider has a per-activity detail endpoint worth calling.
    #: False means the list payload is already complete.
    supports_detail: bool
    #: True when the provider exposes per-activity time series.
    supports_streams: bool
    #: True when re-listing a recent window is cheap enough to detect edits.
    supports_reconcile: bool
    #: Route that starts authorization, or "" for API-key sources.
    connect_path: str
    #: Route that revokes stored credentials, or "" for API-key sources.
    disconnect_path: str

    def is_configured(self) -> bool:
        """True when credentials are present in the configuration."""

    async def is_connected(self) -> bool:
        """True when the source can make authenticated calls right now.

        API-key sources answer from configuration; OAuth sources answer from the
        stored token.
        """

    async def verify_credentials(self) -> int | None:
        """Check credentials and return the provider's athlete id, if any.

        Must raise on an authentication failure so the sync can stop with a
        clear message instead of silently syncing nothing. Never used to read
        heart-rate zones or FTP — those come from configuration only.
        """

    def register_auth_routes(self, app: FastAPI) -> None:
        """Mount any OAuth routes this source needs. No-op for API keys."""

    async def list_activities(self, start: datetime, end: datetime) -> list[dict]:
        """Return provider payloads for activities in ``[start, end)``.

        The source owns its own pagination dialect; the engine only ever asks
        for a date window.
        """

    async def fetch_detail(self, external_id: str) -> dict | None:
        """Return the detailed payload for one activity.

        ``None`` means "permanently unavailable" (e.g. deleted upstream) and the
        engine will stop asking. Raise for transient failures so it retries.
        """

    async def fetch_streams(self, activity: Activity) -> dict[str, list]:
        """Return canonical streams for one stored activity."""

    def map_row(self, payload: dict, athlete_id: int) -> dict | None:
        """Map a provider payload onto ``activities`` column values.

        Pure and synchronous, so it can be tested against recorded fixtures.
        Returns ``None`` to skip a payload that is not a usable activity.
        """

    def rate_limits(self) -> RateLimitSnapshot:
        """Last-seen quota usage for this source."""

    async def close(self) -> None:
        """Release HTTP resources."""
