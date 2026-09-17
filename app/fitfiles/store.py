"""Persistence for raw FIT files.

Thin, generic helpers with no knowledge of any particular activity source:
one row per activity, keyed by the same local activity id used everywhere
else. Callers (source adapters) are responsible for deciding when to call
:func:`save_fit_bytes` (typically: download once, then reuse forever) and for
which ``source`` label to record.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import ActivityFitFile

#: Defense in depth against an oversized or runaway upstream response: even a
#: multi-day ultra-endurance recording at 1Hz is a few MB, so this is a
#: generous ceiling on what is reasonable to persist, not a real-world limit.
#: Callers that download over the network should also cap the response size
#: themselves rather than relying solely on this check (which only runs after
#: the bytes are already in memory).
MAX_RAW_DATA_BYTES = 50 * 1024 * 1024


class FitFileTooLargeError(ValueError):
    """Raised when raw FIT bytes exceed :data:`MAX_RAW_DATA_BYTES`."""


async def get_fit_bytes(session: AsyncSession, activity_id: str) -> bytes | None:
    """Return the previously stored raw FIT bytes for ``activity_id``, if any."""
    result = await session.execute(
        select(ActivityFitFile.raw_data).where(
            ActivityFitFile.activity_id == activity_id
        )
    )
    row = result.scalar_one_or_none()
    return bytes(row) if row is not None else None


async def save_fit_bytes(
    session: AsyncSession, *, activity_id: str, source: str, raw_data: bytes
) -> None:
    """Permanently store (or refresh) the raw FIT bytes for ``activity_id``."""
    if len(raw_data) > MAX_RAW_DATA_BYTES:
        raise FitFileTooLargeError(
            f"FIT file for activity {activity_id!r} is {len(raw_data)} bytes, "
            f"exceeding the {MAX_RAW_DATA_BYTES}-byte limit"
        )

    stmt = pg_insert(ActivityFitFile).values(
        activity_id=activity_id,
        source=source,
        raw_data=raw_data,
        file_size=len(raw_data),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["activity_id"],
        set_={
            "source": stmt.excluded.source,
            "raw_data": stmt.excluded.raw_data,
            "file_size": stmt.excluded.file_size,
        },
    )
    await session.execute(stmt)
    await session.commit()
