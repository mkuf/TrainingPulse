"""Live sync progress, shared by the engine and the status UI."""

from __future__ import annotations

from datetime import datetime

from sources.base import ActivitySource, RateLimitSnapshot


class SyncState:
    """Mutable, in-memory view of the current sync run."""

    def __init__(self) -> None:
        self.is_running: bool = False
        self.phase: str = "idle"
        #: Name of the source currently being worked on, or "".
        self.active_source: str = ""
        self.total_activities: int = 0
        self.synced_activities: int = 0
        self.details_fetched: int = 0
        self.details_pending: int = 0
        self.streams_fetched: int = 0
        self.streams_pending: int = 0
        #: Per-source quota usage, keyed by source name.
        self.rate_limits: dict[str, RateLimitSnapshot] = {}
        self.last_error: str | None = None
        self.last_sync: datetime | None = None

    def begin(self, phase: str, source: str = "") -> None:
        self.phase = phase
        self.active_source = source

    def snapshot_rate_limits(self, source: ActivitySource) -> None:
        """Record a source's current quota usage."""
        self.rate_limits[source.name] = source.rate_limits()

    def rate_limit_payload(self) -> dict[str, dict]:
        return {name: snap.as_payload() for name, snap in self.rate_limits.items()}


sync_state = SyncState()
