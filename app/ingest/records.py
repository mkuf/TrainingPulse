"""Generic sync for every non-activity data kind.

A :class:`~providers.feeds.RecordFeed` only lists a window and maps payloads;
this module does the rest identically for every provider: choosing the window,
upserting into the kind's core table, and deciding which provider's rows are
visible for each day.

Every record kind is per-day: body, nutrition, sleep (keyed by the day the
night ends), daily wellness, and intraday samples (keyed by their local day).
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from catalog import DataKind
from ingest.activities import date_windows
from ingest.state import SyncState
from models import (
    BodyMeasurement,
    DailyNutrition,
    DailyWellness,
    RecordSyncCursor,
    SleepSession,
    WellnessSample,
)
from providers.feeds import (
    BodyRecord,
    NutritionRecord,
    RecordFeed,
    SampleRecord,
    SleepRecord,
    WellnessRecord,
)

logger = logging.getLogger(__name__)

#: Rows per INSERT. Keeps a multi-year backfill well under Postgres' bind
#: parameter limit.
_UPSERT_BATCH = 1000


# ── Per-kind storage ────────────────────────────────────────────────


def _body_values(record: BodyRecord, provider: str, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "provider": provider,
        "external_id": record.external_id,
        "measured_at": record.measured_at,
        "day": record.measured_at.astimezone(tz).date(),
        "weight_kg": record.weight_kg,
        "fat_pct": record.fat_pct,
        "muscle_mass_kg": record.muscle_mass_kg,
        "device": record.device,
    }


def _nutrition_values(
    record: NutritionRecord, provider: str, tz: ZoneInfo
) -> dict[str, Any]:
    return {
        "provider": provider,
        "day": record.day,
        "kcal": record.kcal,
        "protein_g": record.protein_g,
        "carbs_g": record.carbs_g,
        "sugar_g": record.sugar_g,
        "fat_g": record.fat_g,
        "fiber_g": record.fiber_g,
    }


def _sleep_values(record: SleepRecord, provider: str, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "provider": provider,
        "external_id": record.external_id,
        "day": record.day,
        "start_at": record.start,
        "end_at": record.end,
        "asleep_seconds": record.asleep_seconds,
        "deep_seconds": record.deep_seconds,
        "light_seconds": record.light_seconds,
        "rem_seconds": record.rem_seconds,
        "awake_seconds": record.awake_seconds,
        "nap_seconds": record.nap_seconds,
        "stages": (
            [
                {
                    "start": stage.start.isoformat(),
                    "end": stage.end.isoformat(),
                    "stage": stage.stage,
                }
                for stage in record.stages
            ]
            if record.stages
            else None
        ),
    }


def _wellness_values(
    record: WellnessRecord, provider: str, tz: ZoneInfo
) -> dict[str, Any]:
    values = dataclasses.asdict(record)
    values["provider"] = provider
    return values


def _sample_values(record: SampleRecord, provider: str, tz: ZoneInfo) -> dict[str, Any]:
    return {
        "provider": provider,
        "metric": record.metric,
        "measured_at": record.measured_at,
        "day": record.measured_at.astimezone(tz).date(),
        "value": record.value,
    }


@dataclass(frozen=True)
class RecordStore:
    """Where one kind's records live and how a record becomes a row."""

    kind: DataKind
    model: type
    #: Unique key a re-listed record updates in place.
    conflict: tuple[str, ...]
    to_values: Callable[[Any, str, ZoneInfo], dict[str, Any]]


STORES: dict[DataKind, RecordStore] = {
    DataKind.BODY: RecordStore(
        DataKind.BODY, BodyMeasurement, ("provider", "external_id"), _body_values
    ),
    DataKind.NUTRITION: RecordStore(
        DataKind.NUTRITION, DailyNutrition, ("provider", "day"), _nutrition_values
    ),
    DataKind.SLEEP: RecordStore(
        DataKind.SLEEP, SleepSession, ("provider", "day"), _sleep_values
    ),
    DataKind.WELLNESS: RecordStore(
        DataKind.WELLNESS, DailyWellness, ("provider", "day"), _wellness_values
    ),
    DataKind.INTRADAY: RecordStore(
        DataKind.INTRADAY,
        WellnessSample,
        ("provider", "metric", "measured_at"),
        _sample_values,
    ),
}

#: Record kinds synced each run, in order.
RECORD_KINDS: tuple[DataKind, ...] = tuple(STORES)


# ── Windows ─────────────────────────────────────────────────────────


def local_day_window(
    oldest: date, newest: date, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Half-open UTC range covering whole local days ``oldest..newest``."""
    start = datetime.combine(oldest, time.min, tzinfo=tz)
    end = datetime.combine(newest + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def first_day_to_list(
    *,
    today: date,
    latest_stored: date | None,
    history_days: int,
    refresh_days: int,
    synced_through: date | None = None,
    force: bool = False,
) -> date:
    """Oldest local day the next sync should list for one provider and kind.

    A first sync (or a forced one) walks the full history. Later syncs re-list
    the last ``refresh_days`` to pick up edits, and never start after the day
    the provider was last caught up to, so a provider that was unreachable
    for a while catches up instead of leaving a gap. "Caught up" is the later
    of its newest stored day and ``synced_through`` (the last fully listed
    day), so days that simply had no data are not re-listed on every run.
    """
    if force or (latest_stored is None and synced_through is None):
        return today - timedelta(days=history_days)
    caught_up = max(day for day in (latest_stored, synced_through) if day is not None)
    return min(caught_up, today - timedelta(days=refresh_days))


# ── Upserting ───────────────────────────────────────────────────────


def map_records(feed: RecordFeed, provider: str, payloads: Iterable[dict]) -> list:
    """Map payloads to records, skipping (and logging) the unusable ones."""
    records = []
    for payload in payloads:
        try:
            record = feed.map_record(payload)
        except Exception as exc:  # noqa: BLE001 - one bad payload must not stop a sync
            logger.warning("Skipping %s %s payload: %s", provider, feed.kind, exc)
            continue
        if record is not None:
            records.append(record)
    return records


async def upsert_records(
    session: AsyncSession,
    store: RecordStore,
    provider: str,
    records: Sequence,
    tz: ZoneInfo,
) -> int:
    """Insert or refresh rows; leaves ``superseded_by`` to the dedup pass."""
    if not records:
        return 0

    # A provider may list the same record twice in one window (FDDB days at a
    # window edge, Withings pages that shift while paging). One INSERT cannot
    # update the same row twice, so keep the last occurrence.
    by_key: dict[tuple, dict[str, Any]] = {}
    for record in records:
        values = store.to_values(record, provider, tz)
        by_key[record_key(store, values)] = values
    rows = list(by_key.values())

    updatable = [name for name in rows[0] if name not in store.conflict]
    for offset in range(0, len(rows), _UPSERT_BATCH):
        stmt = pg_insert(store.model).values(rows[offset : offset + _UPSERT_BATCH])
        stmt = stmt.on_conflict_do_update(
            index_elements=list(store.conflict),
            set_={
                **{name: getattr(stmt.excluded, name) for name in updatable},
                "synced_at": func.now(),
            },
        )
        await session.execute(stmt)
    await session.commit()
    return len(rows)


def record_key(store: RecordStore, values: dict[str, Any]) -> tuple:
    """The conflict key a row is upserted on."""
    return tuple(values[name] for name in store.conflict)


def stale_ids(stored: Iterable[tuple[int, tuple]], listed: set[tuple]) -> list[int]:
    """Ids of stored ``(id, key)`` rows whose key was not listed again. Pure."""
    return [row_id for row_id, key in stored if key not in listed]


async def delete_unlisted(
    session: AsyncSession,
    store: RecordStore,
    provider: str,
    first: date,
    last: date,
    listed: set[tuple],
) -> int:
    """Delete this provider's rows for days ``first..last`` that were not listed.

    Records can be listed again, so unlike activities they are deleted rather
    than hidden.
    """
    model = store.model
    result = await session.execute(
        select(model.id, *(getattr(model, name) for name in store.conflict)).where(
            model.provider == provider, model.day >= first, model.day <= last
        )
    )
    stale = stale_ids(((row[0], tuple(row[1:])) for row in result.all()), listed)
    for offset in range(0, len(stale), _UPSERT_BATCH):
        await session.execute(
            delete(model).where(model.id.in_(stale[offset : offset + _UPSERT_BATCH]))
        )
    if stale:
        await session.commit()
    return len(stale)


async def _synced_through(
    session: AsyncSession, provider: str, kind: DataKind
) -> date | None:
    result = await session.execute(
        select(RecordSyncCursor.synced_through).where(
            RecordSyncCursor.provider == provider, RecordSyncCursor.kind == str(kind)
        )
    )
    return result.scalar_one_or_none()


async def _store_synced_through(
    session: AsyncSession, provider: str, kind: DataKind, day: date
) -> None:
    stmt = pg_insert(RecordSyncCursor).values(
        provider=provider, kind=str(kind), synced_through=day
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "kind"],
        set_={"synced_through": stmt.excluded.synced_through, "updated_at": func.now()},
    )
    await session.execute(stmt)
    await session.commit()


async def _latest_day(session: AsyncSession, store: RecordStore, provider: str) -> date | None:
    result = await session.execute(
        select(func.max(store.model.day)).where(store.model.provider == provider)
    )
    return result.scalar_one_or_none()


async def sync_record_feed(
    provider: str,
    feed: RecordFeed,
    session: AsyncSession,
    *,
    history_days: int,
    refresh_days: int,
    window_days: int,
    tz: ZoneInfo,
    state: SyncState,
    force: bool = False,
) -> int:
    """List, map, and upsert one provider's records for one kind."""
    store = STORES[feed.kind]
    today = datetime.now(tz).date()
    latest = await _latest_day(session, store, provider)
    synced_through = await _synced_through(session, provider, feed.kind)
    oldest = first_day_to_list(
        today=today,
        latest_stored=latest,
        history_days=history_days,
        refresh_days=refresh_days,
        synced_through=synced_through,
        force=force,
    )
    state.begin(
        f"backfilling {feed.kind}" if latest is None or force else f"syncing {feed.kind}",
        provider,
    )

    # Rows in the refresh window that are no longer listed were deleted
    # upstream. The oldest re-listed day is left alone: providers disagree on
    # which window an edge record belongs to (a night that ends on it may be
    # listed with the day before).
    sweep_from = today - timedelta(days=refresh_days - 1)
    listed: set[tuple] = set()

    stored = 0
    for window_oldest, window_newest in date_windows(oldest, today, window_days):
        start, end = local_day_window(window_oldest, window_newest, tz)
        payloads = await feed.list_records(start, end)
        records = map_records(feed, provider, payloads)
        for record in records:
            values = store.to_values(record, provider, tz)
            if values["day"] >= sweep_from:
                listed.add(record_key(store, values))
        stored += await upsert_records(session, store, provider, records, tz)
    # Only reached when every window listed, so a failure mid-way retries the
    # whole range next time and deletes nothing.
    deleted = 0
    if refresh_days > 0:
        deleted = await delete_unlisted(
            session, store, provider, sweep_from, today, listed
        )
    await _store_synced_through(session, provider, feed.kind, today)
    logger.info(
        "%s %s: %d records stored, %d gone upstream (%s → %s)",
        provider,
        feed.kind,
        stored,
        deleted,
        oldest,
        today,
    )
    return stored


# ── Deduplication ───────────────────────────────────────────────────


def choose_daily_winners(
    rows: Iterable[tuple[date, str]], precedence: Sequence[str]
) -> dict[date, str]:
    """For each day, the highest-precedence provider that has data that day.

    Pure. Providers absent from ``precedence`` rank last, alphabetically, so
    the result never depends on row order.
    """
    rank = {name: index for index, name in enumerate(precedence)}
    unranked = len(rank)

    def key(provider: str) -> tuple[int, str]:
        return rank.get(provider, unranked), provider

    winners: dict[date, str] = {}
    for day, provider in rows:
        current = winners.get(day)
        if current is None or key(provider) < key(current):
            winners[day] = provider
    return winners


async def recompute_daily_supersessions(
    session: AsyncSession, kind: DataKind, precedence: Sequence[str]
) -> int:
    """Recompute ``superseded_by`` for one per-day kind. Writes only changes.

    Returns how many (day, provider) groups changed visibility.
    """
    model = STORES[kind].model
    result = await session.execute(
        select(model.day, model.provider, model.superseded_by).distinct()
    )
    rows = result.all()
    if not rows:
        return 0

    winners = choose_daily_winners(((row.day, row.provider) for row in rows), precedence)

    # Group by (new superseded_by, provider) so a precedence flip over years of
    # history is a handful of UPDATEs rather than one per day.
    changes: dict[tuple[str | None, str], set[date]] = {}
    for row in rows:
        winner = winners[row.day]
        target = None if row.provider == winner else winner
        if row.superseded_by != target:
            changes.setdefault((target, row.provider), set()).add(row.day)

    for (target, provider), days in changes.items():
        await session.execute(
            update(model)
            .where(model.provider == provider, model.day.in_(days))
            .values(superseded_by=target)
        )
    if changes:
        await session.commit()
        logger.info(
            "%s: visibility changed for %d provider-days",
            kind,
            sum(len(days) for days in changes.values()),
        )
    return sum(len(days) for days in changes.values())
