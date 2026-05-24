"""In-memory sync status for thin plugin ingestors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SimpleSyncState:
    """Tracks background sync progress for optional plugins."""

    running: bool = False
    last_run_at: datetime | None = None
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def mark_running(self) -> None:
        self.running = True
        self.last_error = None

    def mark_finished(
        self,
        *,
        error: str | None = None,
        **counts: Any,
    ) -> None:
        self.running = False
        self.last_run_at = datetime.now(timezone.utc)
        if error is not None:
            self.last_error = error
        self.extra.update(counts)

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            **self.extra,
        }
