"""Generic sync engine.

Every source goes through the same phases here — backfill or incremental,
reconcile, detail drain, stream drain — so a new provider only has to implement
the :class:`~providers.base.ActivitySource` protocol.

The run is deliberately two-pass (see :func:`sync_sources` callers):

1. every source lists its window and stores summary rows, and hides the
   activities its refresh window no longer lists;
2. supersession is recomputed across all stored rows;
3. only the winners get their detail and stream calls.

That ordering is what lets a tight-budget provider like Strava avoid spending
requests on activities that are hidden behind a higher-precedence source.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Awaitable, Callable, Iterable, Sequence, TypeVar

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from calories import (
    ActivityEnergyInputs,
    AthleteEnergyInputs,
    compute_energy,
    kilojoules_from_stream,
)
from ingest.state import SyncState
from resting_hr import resting_hr_on
from metrics import (
    calculate_best_interval,
    calculate_normalized_power,
    calculate_power_curve,
    calculate_power_zones,
    calculate_trimp_from_streams,
    estimate_trimp_without_hr,
)
from models import ACTIVITY_VISIBLE, Activity, ActivityStream, BodyMeasurement
from providers.base import ActivitySource, SourceRateLimitExceeded, external_id
from providers.mapping import steps_per_minute

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Two activities can only supersede one another when their start times are
#: within this many seconds *and* their time ranges genuinely overlap.
START_TOLERANCE_SECONDS = 900

#: Columns refreshed from a list payload on every upsert. Deliberately excludes
#: detail-only and derived columns so re-listing never destroys enriched data.
LIST_UPSERT_FIELDS = (
    "name",
    "source",
    "athlete_id",
    "sport_type",
    "start_date",
    "elapsed_time",
    "moving_time",
    "distance",
    "average_heartrate",
    "max_heartrate",
    "has_heartrate",
    "average_watts",
    "max_watts",
    "average_speed",
    "total_elevation_gain",
    "total_descent",
    "kilojoules",
    "device_watts",
    "gear_id",
    "provider_updated_at",
)

#: Summary columns the stream phase derives when a source omits them.
_STREAM_DERIVED_FIELDS = (
    "average_heartrate",
    "max_heartrate",
    "average_speed",
    "max_speed",
    "average_cadence",
    "max_cadence",
)

#: Columns compared to decide whether a re-listed activity was edited upstream.
_DIFF_FIELDS = (
    "name",
    "sport_type",
    "start_date",
    "elapsed_time",
    "moving_time",
    "distance",
    "average_heartrate",
    "max_heartrate",
    "has_heartrate",
    "average_watts",
    "max_watts",
    "gear_id",
    "kilojoules",
    "device_watts",
)

_FLOAT_FIELDS = {
    "distance",
    "average_heartrate",
    "max_heartrate",
    "average_watts",
    "max_watts",
    "kilojoules",
    "average_speed",
    "total_elevation_gain",
}

#: Rows per INSERT. An activity row has ~30 columns, so this keeps a busy
#: window well under Postgres' bind parameter limit.
_UPSERT_BATCH = 500

#: Detail-only columns merged from a detail payload.
_DETAIL_ONLY_FIELDS = (
    "description",
    "private_note",
    "calories",
    "device_name",
    "gear_name",
)


@dataclass
class EngineContext:
    """Everything the engine needs that is not source-specific."""

    session_factory: async_sessionmaker
    athlete_id: int
    state: SyncState
    #: ``data.activities.history_days``: how far back a backfill lists.
    history_days: int
    window_days: int
    #: ``data.activities.refresh_days``: how far back reconcile re-lists.
    refresh_days: int
    detail_concurrency: int
    streams_concurrency: int
    hr_zone_boundaries: list[tuple[float, float]] = field(default_factory=list)
    power_zone_boundaries: list[tuple[float, float]] = field(default_factory=list)
    #: Athlete values for computed calories; ``weight_kg`` and ``rest_hr`` are
    #: the configured fallbacks, replaced per activity by the nearest weigh-in
    #: and the imported resting heart rate (see :mod:`resting_hr`).
    energy_athlete: AthleteEnergyInputs | None = None


# ── Concurrency ─────────────────────────────────────────────────────


async def run_with_concurrency(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[None]],
    concurrency: int,
) -> bool:
    """Run ``worker`` over ``items``; return False if a rate limit stopped us.

    A rate limit is a pause, not a failure: remaining items are abandoned so the
    next scheduled sync can resume them.
    """
    items = list(items)
    if not items:
        return True

    semaphore = asyncio.Semaphore(max(1, concurrency))
    rate_limited = asyncio.Event()

    async def _runner(item: T) -> None:
        if rate_limited.is_set():
            return
        async with semaphore:
            if rate_limited.is_set():
                return
            try:
                await worker(item)
            except SourceRateLimitExceeded:
                rate_limited.set()
                raise

    tasks = [asyncio.create_task(_runner(item)) for item in items]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    for result in results:
        if isinstance(result, SourceRateLimitExceeded):
            return False
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError
        ):
            raise result
    return not rate_limited.is_set()


# ── Windows ─────────────────────────────────────────────────────────


def date_windows(
    oldest: date, newest: date, window_days: int
) -> list[tuple[date, date]]:
    """Split an inclusive date range into chunks of at most ``window_days``."""
    windows: list[tuple[date, date]] = []
    span = max(1, window_days)
    cursor = oldest
    while cursor <= newest:
        end = min(cursor + timedelta(days=span - 1), newest)
        windows.append((cursor, end))
        cursor = end + timedelta(days=1)
    return windows


def _as_window(oldest: date, newest: date) -> tuple[datetime, datetime]:
    """Convert an inclusive date range to a half-open UTC datetime range."""
    start = datetime.combine(oldest, time.min, tzinfo=timezone.utc)
    end = datetime.combine(newest, time.min, tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


# ── Upserting ───────────────────────────────────────────────────────


def map_rows(
    source: ActivitySource, payloads: Sequence[dict], athlete_id: int
) -> list[dict]:
    """Map provider payloads to column dicts, skipping the unusable ones."""
    rows: list[dict] = []
    for payload in payloads:
        try:
            row = source.map_row(payload, athlete_id)
        except Exception as exc:  # noqa: BLE001 - one bad payload must not stop a sync
            logger.warning("Skipping %s payload: %s", source.name, exc)
            continue
        if row is not None:
            rows.append(row)
    return rows


async def upsert_rows(
    session: AsyncSession, source: ActivitySource, rows: list[dict]
) -> int:
    """Insert or refresh summary rows for one source.

    For a source with a detail endpoint, rows whose detail is already merged
    are left alone: the list payload is a subset of the detail payload, so
    overwriting would null out enriched columns. Reconcile and a forced
    resync clear ``detail_synced`` first when a row really needs refreshing.

    A row marked gone upstream that is listed again is restored: the mark is
    cleared and, for a detail source, its detail is re-queued.
    """
    if not rows:
        return 0

    # One INSERT cannot update the same row twice, so keep the last
    # occurrence of an id listed twice in one window.
    values = list({row["id"]: dict(row) for row in rows}.values())
    fields = list(LIST_UPSERT_FIELDS)
    restore: dict = {"deleted_upstream_at": None}
    where = None
    if source.supports_detail:
        existing = Activity.__table__.c
        where = or_(
            existing.detail_synced.is_(False),
            existing.deleted_upstream_at.is_not(None),
        )
        restore["detail_synced"] = False
    else:
        # The list payload is all there is, and there is no later detail-merge
        # step (drain_detail_queue never runs for these sources) to ever
        # refresh detail-only columns afterwards. Refresh them from the list
        # payload here too, or they freeze at their first-seen value forever.
        for row in values:
            row["detail_synced"] = True
        fields.append("detail_synced")
        fields.extend(_DETAIL_ONLY_FIELDS)

    for offset in range(0, len(values), _UPSERT_BATCH):
        stmt = pg_insert(Activity).values(values[offset : offset + _UPSERT_BATCH])
        existing = Activity.__table__.c
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                **{name: getattr(stmt.excluded, name) for name in fields},
                # The stream phase fills these in when the list payload has
                # none; a later re-list without them must not wipe them.
                **{
                    name: func.coalesce(
                        getattr(stmt.excluded, name), getattr(existing, name)
                    )
                    for name in _STREAM_DERIVED_FIELDS
                },
                # distance is NOT NULL, so "none" arrives as 0.
                "distance": case(
                    (stmt.excluded.distance > 0, stmt.excluded.distance),
                    else_=existing.distance,
                ),
                "has_heartrate": or_(
                    stmt.excluded.has_heartrate, existing.has_heartrate
                ),
                **restore,
            },
            where=where,
        )
        await session.execute(stmt)
    await session.commit()
    return len(values)


def _values_differ(existing: Activity, row: dict) -> bool:
    for name in _DIFF_FIELDS:
        current = getattr(existing, name, None)
        incoming = row.get(name)
        if name in _FLOAT_FIELDS:
            if current is None and incoming is None:
                continue
            if current is None or incoming is None:
                return True
            if float(current) != float(incoming):
                return True
            continue
        if current != incoming:
            return True
    return False


def needs_full_refresh(existing: Activity, row: dict) -> bool:
    """True when an upstream edit invalidates derived metrics, not just detail."""
    incoming = row.get("provider_updated_at")
    if incoming is not None and existing.provider_updated_at is not None:
        if incoming > existing.provider_updated_at:
            return True
    return _values_differ(existing, row)


async def _mark_for_detail_refresh(session: AsyncSession, ids: Sequence[str]) -> None:
    if not ids:
        return
    await session.execute(
        update(Activity).where(Activity.id.in_(ids)).values(detail_synced=False)
    )
    await session.commit()


async def _mark_for_full_refresh(session: AsyncSession, ids: Sequence[str]) -> None:
    if not ids:
        return
    await session.execute(
        update(Activity)
        .where(Activity.id.in_(ids))
        .values(
            detail_synced=False,
            synced_streams=False,
            trimp=None,
            hr_zone_seconds=None,
            power_zone_seconds=None,
            best_20min_power=None,
            power_curve=None,
            weighted_average_watts=None,
        )
    )
    await session.commit()


# ── Phase 1: listing ────────────────────────────────────────────────


async def _stored_count(
    session: AsyncSession, source: ActivitySource, athlete_id: int
) -> int:
    result = await session.execute(
        select(func.count(Activity.id)).where(
            Activity.athlete_id == athlete_id, Activity.source == source.name
        )
    )
    return int(result.scalar_one() or 0)


async def _latest_start(
    session: AsyncSession, source: ActivitySource, athlete_id: int
) -> datetime | None:
    result = await session.execute(
        select(func.max(Activity.start_date)).where(
            Activity.athlete_id == athlete_id, Activity.source == source.name
        )
    )
    return result.scalar_one_or_none()


async def backfill(
    source: ActivitySource, session: AsyncSession, ctx: EngineContext
) -> int:
    """Walk the full configured history in ``window_days`` chunks."""
    ctx.state.begin("backfilling", source.name)
    newest = date.today()
    oldest = newest - timedelta(days=ctx.history_days)
    windows = date_windows(oldest, newest, ctx.window_days)
    logger.info(
        "%s backfill: %s → %s (%d windows)", source.name, oldest, newest, len(windows)
    )

    stored = 0
    for window_oldest, window_newest in windows:
        start, end = _as_window(window_oldest, window_newest)
        payloads = await source.list_activities(start, end)
        ctx.state.snapshot_rate_limits(source)
        stored += await upsert_rows(
            session, source, map_rows(source, payloads, ctx.athlete_id)
        )
        ctx.state.synced_activities = stored
        logger.info(
            "%s backfill %s..%s: %d listed (total stored %d)",
            source.name,
            window_oldest,
            window_newest,
            len(payloads),
            stored,
        )
    logger.info("%s backfill complete: %d activities", source.name, stored)
    return stored


async def incremental(
    source: ActivitySource, session: AsyncSession, ctx: EngineContext
) -> int:
    """List everything since the newest stored activity, minus a safety margin."""
    ctx.state.begin("incremental", source.name)
    latest = await _latest_start(session, source, ctx.athlete_id)
    newest = date.today() + timedelta(days=1)
    if latest is not None:
        oldest = (latest - timedelta(days=1)).date()
    else:
        oldest = newest - timedelta(days=ctx.refresh_days or 90)

    start, end = _as_window(oldest, newest)
    payloads = await source.list_activities(start, end)
    ctx.state.snapshot_rate_limits(source)
    stored = await upsert_rows(
        session, source, map_rows(source, payloads, ctx.athlete_id)
    )
    logger.info("%s incremental: %d activities", source.name, stored)
    return stored


def sweep_window(oldest: date, newest: date) -> tuple[datetime, datetime]:
    """Start dates a re-list of ``oldest..newest`` may declare gone.

    The oldest day is left out: providers disagree on which window an activity
    at the edge belongs to (intervals.icu lists by date, others by start time),
    so missing from that day's listing does not mean deleted.
    """
    return _as_window(oldest + timedelta(days=1), newest)


async def _mark_gone_upstream(
    session: AsyncSession,
    source: ActivitySource,
    athlete_id: int,
    listed_ids: Sequence[str],
    start: datetime,
    end: datetime,
) -> int:
    """Hide this source's activities in ``[start, end)`` that were not listed.

    Only hidden, never deleted, for the same reasons as a 404 on detail (see
    :func:`_apply_detail`); a later listing that includes one restores it.
    """
    query = select(Activity.id).where(
        Activity.athlete_id == athlete_id,
        Activity.source == source.name,
        Activity.deleted_upstream_at.is_(None),
        Activity.start_date >= start,
        Activity.start_date < end,
    )
    if listed_ids:
        query = query.where(Activity.id.not_in(listed_ids))
    gone = [row[0] for row in (await session.execute(query)).all()]
    if not gone:
        return 0
    for activity_id in gone:
        logger.info(
            "%s activity %s is gone upstream; hiding it", source.name, activity_id
        )
    await session.execute(
        update(Activity)
        .where(Activity.id.in_(gone))
        .values(deleted_upstream_at=func.now())
    )
    await session.commit()
    return len(gone)


async def reconcile(
    source: ActivitySource, session: AsyncSession, ctx: EngineContext
) -> int:
    """Re-list the refresh window, hide what is gone, re-queue what was edited.

    The deletion sweep runs for every source. Edit detection only runs where
    ``supports_reconcile`` is set, since for the others a re-listed summary
    cannot tell an edit from a list payload that is simply thinner.
    """
    if ctx.refresh_days <= 0:
        return 0

    ctx.state.begin("reconciling", source.name)
    newest = date.today() + timedelta(days=1)
    oldest = newest - timedelta(days=ctx.refresh_days)
    start, end = _as_window(oldest, newest)
    payloads = await source.list_activities(start, end)
    ctx.state.snapshot_rate_limits(source)

    rows = map_rows(source, payloads, ctx.athlete_id)
    ids = [row["id"] for row in rows]
    gone = await _mark_gone_upstream(
        session, source, ctx.athlete_id, ids, *sweep_window(oldest, newest)
    )
    if not ids:
        if gone:
            logger.info("%s reconcile: %d gone upstream", source.name, gone)
        return 0

    full_refresh: list[str] = []
    detail_refresh: list[str] = []
    if source.supports_reconcile:
        result = await session.execute(
            select(Activity).where(
                Activity.athlete_id == ctx.athlete_id, Activity.id.in_(ids)
            )
        )
        existing_by_id = {row.id: row for row in result.scalars().all()}
        for row in rows:
            existing = existing_by_id.get(row["id"])
            if existing is None:
                continue
            if needs_full_refresh(existing, row):
                full_refresh.append(row["id"])
            elif source.supports_detail and not existing.detail_synced:
                detail_refresh.append(row["id"])

    await _mark_for_full_refresh(session, full_refresh)
    await _mark_for_detail_refresh(session, detail_refresh)
    stored = await upsert_rows(session, source, rows)
    logger.info(
        "%s reconcile: %d listed, %d gone upstream, %d full refresh, "
        "%d detail refresh",
        source.name,
        len(rows),
        gone,
        len(full_refresh),
        len(detail_refresh),
    )
    return stored


async def run_list_phase(
    source: ActivitySource,
    session: AsyncSession,
    ctx: EngineContext,
    *,
    force_resync: bool = False,
) -> int:
    """Backfill or incrementally list, then reconcile."""
    stored_before = await _stored_count(session, source, ctx.athlete_id)
    if force_resync or stored_before == 0:
        stored = await backfill(source, session, ctx)
    else:
        stored = await incremental(source, session, ctx)
        stored += await reconcile(source, session, ctx)
    return stored


# ── Phase 2: supersession ───────────────────────────────────────────


@dataclass(frozen=True)
class ActivityKey:
    """The minimum needed to decide which of two activities wins."""

    id: str
    source: str
    start: datetime
    elapsed: int = 0

    @property
    def end(self) -> datetime:
        # A minimum of one second so two zero-duration rows that start at the
        # same instant still register as the same workout.
        return self.start + timedelta(seconds=max(self.elapsed or 0, 1))


def _overlaps(left: ActivityKey, right: ActivityKey) -> bool:
    if abs((left.start - right.start).total_seconds()) > START_TOLERANCE_SECONDS:
        return False
    return left.start < right.end and left.end > right.start


def choose_winners(
    rows: Sequence[ActivityKey], precedence: Sequence[str]
) -> dict[str, str | None]:
    """Decide, for each activity, which activity supersedes it.

    Returns ``{activity_id: winner_id or None}``. ``None`` means the activity is
    visible. Pure and total: every input id appears in the result.

    Comparison is pairwise rather than transitive, so a chain of near-misses
    cannot collapse two activities that do not actually overlap. Chains of
    winners are then resolved to their root so ``superseded_by`` always points at
    a visible activity.

    Sources absent from ``precedence`` rank last. Precedence is positional and
    ignores whether a source is currently enabled: a disabled source's rows must
    keep the standing they had, or reordering the list would silently rewrite
    history.
    """
    rank = {name: index for index, name in enumerate(precedence)}
    unranked = len(rank)

    def sort_key(row: ActivityKey) -> tuple:
        # Best first: higher precedence, then longer, then earlier, then stable.
        return (rank.get(row.source, unranked), -(row.elapsed or 0), row.start, row.id)

    ordered = sorted(rows, key=lambda row: (row.start, row.id))
    by_id = {row.id: row for row in rows}
    direct: dict[str, str | None] = {}

    for index, row in enumerate(ordered):
        best = row
        # Only rows whose start is within the tolerance can compete, and the
        # list is sorted by start, so a short scan either side is enough.
        for neighbour_index in range(index - 1, -1, -1):
            neighbour = ordered[neighbour_index]
            if (row.start - neighbour.start).total_seconds() > START_TOLERANCE_SECONDS:
                break
            if _overlaps(row, neighbour) and sort_key(neighbour) < sort_key(best):
                best = neighbour
        for neighbour in ordered[index + 1 :]:
            if (neighbour.start - row.start).total_seconds() > START_TOLERANCE_SECONDS:
                break
            if _overlaps(row, neighbour) and sort_key(neighbour) < sort_key(best):
                best = neighbour
        direct[row.id] = None if best.id == row.id else best.id

    resolved: dict[str, str | None] = {}
    for activity_id in direct:
        origin = by_id[activity_id]
        winner = direct[activity_id]
        seen = {activity_id}
        while winner is not None and direct.get(winner) is not None:
            if winner in seen:
                # Defensive: a cycle would mean two rows each outrank the other.
                break
            candidate = direct[winner]
            if candidate is not None and not _overlaps(origin, by_id[candidate]):
                # The chain's next hop no longer overlaps the original
                # activity. ``winner`` itself is not a root (it lost to
                # ``candidate``), so it cannot be used either without
                # violating "always points at a visible activity". With no
                # valid visible activity that actually overlaps ``origin``,
                # leave it visible rather than collapsing it into an
                # unrelated activity it never overlapped.
                winner = None
                break
            seen.add(winner)
            winner = candidate
        resolved[activity_id] = winner
    return resolved


async def recompute_supersessions(
    session: AsyncSession, athlete_id: int, precedence: Sequence[str]
) -> int:
    """Recompute ``superseded_by`` for every stored activity.

    Idempotent and complete, so it copes with a precedence change, a newly
    connected source, or an activity disappearing upstream (see
    ``deleted_upstream_at``) without any special cases. Only differences are
    written.
    """
    result = await session.execute(
        select(
            Activity.id,
            Activity.source,
            Activity.start_date,
            Activity.elapsed_time,
            Activity.superseded_by,
            Activity.deleted_upstream_at,
        ).where(Activity.athlete_id == athlete_id)
    )
    records = result.all()
    if not records:
        return 0

    keys = [
        ActivityKey(
            id=record.id,
            source=record.source or "",
            start=record.start_date,
            elapsed=record.elapsed_time or 0,
        )
        for record in records
        # An activity gone upstream neither competes nor stays hidden behind
        # another: it is out of the picture entirely, so it cannot keep
        # suppressing the copies other sources hold.
        if record.start_date is not None and record.deleted_upstream_at is None
    ]
    current = {record.id: record.superseded_by for record in records}
    winners = choose_winners(keys, precedence)
    for record in records:
        if record.deleted_upstream_at is not None:
            winners[record.id] = None

    changed = 0
    changes_by_winner: dict[str | None, list[str]] = {}
    for activity_id, winner in winners.items():
        if current.get(activity_id) == winner:
            continue
        changes_by_winner.setdefault(winner, []).append(activity_id)
        changed += 1

    # Grouped by target winner rather than one UPDATE per activity: a
    # precedence change or initial multi-source backfill can affect a large
    # share of an athlete's history, and most changed rows share the same new
    # winner (or None), so this collapses what could be thousands of
    # round-trips into one per distinct winner.
    for winner, activity_ids in changes_by_winner.items():
        await session.execute(
            update(Activity)
            .where(Activity.id.in_(activity_ids))
            .values(superseded_by=winner)
        )
        logger.info(
            "%d activities are now %s",
            len(activity_ids),
            f"superseded by {winner}" if winner else "visible",
        )

    if changed:
        await session.commit()
    return changed


# ── Phase 3: enrichment ─────────────────────────────────────────────


def activity_needs_streams(activity: Activity) -> bool:
    """Streams are only worth fetching when they can produce HR or power."""
    return bool(activity.has_heartrate or activity.device_watts)


async def refresh_progress(
    session: AsyncSession, athlete_id: int, state: SyncState
) -> None:
    """Recompute the global detail/stream progress counters for the UI."""
    row = (
        await session.execute(
            select(
                func.count(Activity.id),
                func.count(Activity.id).filter(
                    Activity.detail_synced == False  # noqa: E712
                ),
                func.count(Activity.id).filter(
                    Activity.synced_streams == False  # noqa: E712
                ),
            ).where(Activity.athlete_id == athlete_id, ACTIVITY_VISIBLE)
        )
    ).one()
    total, details_pending, streams_pending = (int(value or 0) for value in row)
    state.total_activities = total
    state.details_pending = details_pending
    state.details_fetched = max(0, total - details_pending)
    state.streams_pending = streams_pending
    state.streams_fetched = max(0, total - streams_pending)


async def _apply_detail(
    session: AsyncSession,
    source: ActivitySource,
    activity_id: str,
    athlete_id: int,
) -> bool:
    """Fetch and merge one activity's detail. Returns True when it progressed."""
    try:
        payload = await source.fetch_detail(external_id(source.id_prefix, activity_id))
    except SourceRateLimitExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - transient; retried next sync
        logger.warning("%s detail for %s failed: %s", source.name, activity_id, exc)
        return False

    if payload is None:
        # Only hidden, never deleted: "gone" may also mean a retention limit or
        # a lost scope, and the stored streams and FIT file are irreplaceable.
        # Stop asking; a later listing that includes it again restores it.
        logger.info("%s activity %s is gone upstream; hiding it", source.name, activity_id)
        result = await session.execute(
            update(Activity)
            .where(Activity.id == activity_id)
            .values(detail_synced=True, deleted_upstream_at=func.now())
        )
        await session.commit()
        return result.rowcount > 0

    try:
        row = source.map_row(payload, athlete_id)
    except Exception as exc:  # noqa: BLE001 - one bad payload must not stop a sync
        logger.warning(
            "%s detail for %s is unusable, keeping list data: %s",
            source.name,
            activity_id,
            exc,
        )
        row = None
    if row is None:
        result = await session.execute(
            update(Activity)
            .where(Activity.id == activity_id)
            .values(detail_synced=True)
        )
        await session.commit()
        return result.rowcount > 0

    # Never let a detail payload rewrite identity or supersession.
    row.pop("id", None)
    row.pop("source", None)
    row.pop("athlete_id", None)
    row["detail_synced"] = True
    result = await session.execute(
        update(Activity).where(Activity.id == activity_id).values(**row)
    )
    await session.commit()
    return result.rowcount > 0


async def drain_detail_queue(
    source: ActivitySource, session: AsyncSession, ctx: EngineContext
) -> None:
    """Fetch detail for this source's activities that are not superseded.

    Superseded activities are skipped on purpose: their data is never shown, so
    spending a request on them wastes budget that a visible activity needs.

    Streams for an activity are fetched right after its detail, while anything
    the source derived for both (Hammerhead's parsed FIT file) is still at hand.
    Each activity is attempted at most once per drain, so one that keeps
    failing costs one request per sync rather than one per batch.
    """
    if not source.supports_detail:
        return

    ctx.state.begin("detailing", source.name)
    await refresh_progress(session, ctx.athlete_id, ctx.state)

    concurrency = ctx.detail_concurrency
    counter_lock = asyncio.Lock()
    attempted: set[str] = set()

    async def _worker(activity_id: str) -> None:
        async with ctx.session_factory() as worker_session:
            progressed = await _apply_detail(
                worker_session, source, activity_id, ctx.athlete_id
            )
        if not progressed:
            return
        async with counter_lock:
            ctx.state.details_fetched += 1
            if ctx.state.details_pending > 0:
                ctx.state.details_pending -= 1
        if source.supports_streams:
            await _stream_one(source, activity_id, ctx, counter_lock)

    while True:
        query = (
            select(Activity.id)
            .where(
                Activity.athlete_id == ctx.athlete_id,
                Activity.source == source.name,
                Activity.detail_synced == False,  # noqa: E712
                ACTIVITY_VISIBLE,
            )
            .order_by(Activity.start_date.desc())
            .limit(max(100, concurrency * 20))
        )
        if attempted:
            query = query.where(Activity.id.not_in(attempted))
        ids = [row[0] for row in (await session.execute(query)).all()]
        if not ids:
            break
        attempted.update(ids)

        completed = await run_with_concurrency(ids, _worker, concurrency)
        ctx.state.snapshot_rate_limits(source)
        if not completed:
            ctx.state.last_error = (
                f"{source.name} rate limit while merging details; "
                "will resume next sync."
            )
            break


def _energy_inputs(activity: Activity) -> ActivityEnergyInputs:
    return ActivityEnergyInputs(
        sport_type=activity.sport_type,
        moving_time=activity.moving_time,
        distance=activity.distance,
        elevation_gain=activity.total_elevation_gain,
        average_watts=activity.average_watts,
        device_watts=activity.device_watts,
        average_heartrate=activity.average_heartrate,
        imported_calories=activity.calories,
    )


async def _computed_energy(
    session: AsyncSession,
    ctx: EngineContext,
    inputs: ActivityEnergyInputs,
    day: date,
    stream_kilojoules: float | None = None,
) -> dict:
    """``computed_*`` column values for one activity, see ``calories.py``."""
    athlete = ctx.energy_athlete
    if athlete is None:
        return {}
    weight = await session.scalar(
        select(BodyMeasurement.weight_kg)
        .where(BodyMeasurement.superseded_by.is_(None))
        .order_by(func.abs(BodyMeasurement.day - day))
        .limit(1)
    )
    if weight is not None:
        athlete = dataclasses.replace(athlete, weight_kg=weight)
    rest = await resting_hr_on(
        session, day, fallback=athlete.rest_hr, max_hr=athlete.max_hr
    )
    if rest.bpm != athlete.rest_hr:
        athlete = dataclasses.replace(athlete, rest_hr=rest.bpm)
    energy = compute_energy(inputs, athlete, stream_kilojoules=stream_kilojoules)
    return {
        "computed_calories": energy.kcal,
        "computed_kilojoules": energy.kilojoules,
        "computed_calories_method": energy.method,
    }


def _missing_summary(activity: Activity, streams: dict[str, list]) -> dict:
    """Summary values derived from the streams for fields the source left empty.

    Some sources (Google Health) report no max and often no average heart rate,
    and no distance for sessions whose route only exists as a track. Stream
    cadence counts one leg on foot, like FIT.
    """
    derived: dict = {}
    samples = [value for value in streams.get("heartrate") or [] if value and value > 0]
    if samples:
        if activity.average_heartrate is None:
            derived["average_heartrate"] = sum(samples) / len(samples)
        if activity.max_heartrate is None:
            derived["max_heartrate"] = float(max(samples))
        if derived and not activity.has_heartrate:
            derived["has_heartrate"] = True

    distances = [value for value in streams.get("distance") or [] if value is not None]
    distance = activity.distance or 0.0
    if distance <= 0 and distances and max(distances) > 0:
        distance = float(max(distances))
        derived["distance"] = distance
    if activity.average_speed is None and distance > 0 and activity.moving_time:
        derived["average_speed"] = distance / activity.moving_time

    speeds = [value for value in streams.get("velocity_smooth") or [] if value]
    if activity.max_speed is None and speeds:
        derived["max_speed"] = float(max(speeds))

    # Zero cadence is coasting or standing; devices leave it out of the average.
    cadences = [value for value in streams.get("cadence") or [] if value and value > 0]
    if cadences:
        if activity.average_cadence is None:
            derived["average_cadence"] = steps_per_minute(
                sum(cadences) / len(cadences), activity.sport_type
            )
        if activity.max_cadence is None:
            derived["max_cadence"] = steps_per_minute(
                max(cadences), activity.sport_type
            )
    return derived


async def _apply_streams(
    session: AsyncSession,
    source: ActivitySource,
    activity: Activity,
    ctx: EngineContext,
) -> None:
    """Fetch streams for one activity and derive its training metrics."""
    wants_hr_power = activity_needs_streams(activity)
    # Snapshot what the fallback needs: a rollback expires ORM attributes and
    # reloading them lazily is not allowed under asyncio.
    activity_id = activity.id
    fallback_trimp = estimate_trimp_without_hr(
        activity.sport_type, activity.moving_time
    )
    energy_inputs = _energy_inputs(activity)
    activity_day = activity.start_date.date()
    try:
        streams = await source.fetch_streams(activity)
        stream_types = list(streams.keys())
        sample_count = max((len(value) for value in streams.values()), default=0)

        if streams:
            stmt = pg_insert(ActivityStream).values(
                activity_id=activity.id,
                data=streams,
                stream_types=",".join(stream_types),
                sample_count=sample_count,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["activity_id"],
                set_={
                    "data": stmt.excluded.data,
                    "stream_types": stmt.excluded.stream_types,
                    "sample_count": stmt.excluded.sample_count,
                },
            )
            await session.execute(stmt)

        derived_summary = _missing_summary(activity, streams)
        if derived_summary:
            for field, value in derived_summary.items():
                setattr(activity, field, value)
            wants_hr_power = activity_needs_streams(activity)
            energy_inputs = _energy_inputs(activity)

        if not wants_hr_power:
            await session.execute(
                update(Activity)
                .where(Activity.id == activity.id)
                .values(
                    **derived_summary,
                    trimp=estimate_trimp_without_hr(
                        activity.sport_type, activity.moving_time
                    ),
                    hr_zone_seconds=None,
                    power_zone_seconds=None,
                    best_20min_power=None,
                    power_curve=None,
                    weighted_average_watts=None,
                    synced_streams=True,
                    **await _computed_energy(
                        session, ctx, energy_inputs, activity_day
                    ),
                )
            )
            await session.commit()
            return

        time_stream = streams.get("time") or []
        # FIT-derived streams carry None for dropped samples; the metric
        # helpers already skip non-positive readings, so treat gaps as 0.
        hr_stream = [v or 0 for v in streams.get("heartrate") or []]
        power_stream = [v or 0 for v in streams.get("watts") or []]

        if (
            activity.has_heartrate
            and time_stream
            and hr_stream
            and len(time_stream) == len(hr_stream)
        ):
            trimp, hr_zones = calculate_trimp_from_streams(
                time_stream, hr_stream, ctx.hr_zone_boundaries
            )
        else:
            hr_zones = None
            trimp = estimate_trimp_without_hr(
                activity.sport_type, activity.moving_time
            )

        best_20min = None
        power_curve = None
        power_zones = None
        normalized_power = None
        if power_stream:
            best_20min = calculate_best_interval(power_stream, 1200)
            power_curve = calculate_power_curve(power_stream)
            normalized_power = calculate_normalized_power(power_stream)
            if time_stream:
                usable = min(len(time_stream), len(power_stream))
                if usable > 0:
                    power_zones = calculate_power_zones(
                        time_stream[:usable],
                        power_stream[:usable],
                        ctx.power_zone_boundaries,
                    )

        energy = await _computed_energy(
            session,
            ctx,
            energy_inputs,
            activity_day,
            kilojoules_from_stream(time_stream, power_stream)
            if activity.device_watts
            else None,
        )

        await session.execute(
            update(Activity)
            .where(Activity.id == activity.id)
            .values(
                **derived_summary,
                trimp=trimp,
                **energy,
                hr_zone_seconds=hr_zones,
                power_zone_seconds=power_zones,
                best_20min_power=best_20min,
                power_curve=power_curve,
                weighted_average_watts=normalized_power,
                synced_streams=True,
            )
        )
        await session.commit()
    except SourceRateLimitExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - fall back to an estimate
        logger.warning("%s streams for %s failed: %s", source.name, activity_id, exc)
        # A failed write leaves the session unusable until rolled back.
        await session.rollback()
        await session.execute(
            update(Activity)
            .where(Activity.id == activity_id)
            .values(
                trimp=fallback_trimp,
                synced_streams=True,
                **await _computed_energy(session, ctx, energy_inputs, activity_day),
            )
        )
        await session.commit()


async def _stream_one(
    source: ActivitySource,
    activity_id: str,
    ctx: EngineContext,
    counter_lock: asyncio.Lock,
) -> None:
    async with ctx.session_factory() as worker_session:
        activity = await worker_session.get(Activity, activity_id)
        if (
            activity is None
            or activity.synced_streams
            or activity.deleted_upstream_at is not None
        ):
            return
        await _apply_streams(worker_session, source, activity, ctx)
    async with counter_lock:
        ctx.state.streams_fetched += 1
        if ctx.state.streams_pending > 0:
            ctx.state.streams_pending -= 1


async def drain_stream_queue(
    source: ActivitySource, session: AsyncSession, ctx: EngineContext
) -> None:
    """Fetch streams for this source's visible, un-streamed activities."""
    if not source.supports_streams:
        return

    ctx.state.begin("streaming", source.name)
    await refresh_progress(session, ctx.athlete_id, ctx.state)

    result = await session.execute(
        select(Activity.id)
        .where(
            Activity.athlete_id == ctx.athlete_id,
            Activity.source == source.name,
            Activity.synced_streams == False,  # noqa: E712
            ACTIVITY_VISIBLE,
        )
        .order_by(Activity.start_date.desc())
    )
    pending_ids = [row[0] for row in result.all()]
    if not pending_ids:
        return

    counter_lock = asyncio.Lock()

    async def _worker(activity_id: str) -> None:
        await _stream_one(source, activity_id, ctx, counter_lock)

    completed = await run_with_concurrency(
        pending_ids, _worker, ctx.streams_concurrency
    )
    ctx.state.snapshot_rate_limits(source)
    if not completed:
        ctx.state.last_error = (
            f"{source.name} rate limit while fetching streams; "
            "will resume next sync."
        )


async def run_enrich_phase(
    source: ActivitySource, session: AsyncSession, ctx: EngineContext
) -> None:
    """Detail then streams, for the activities this source actually shows.

    The detail drain already streams each activity it merges; the stream
    drain picks up the rest (sources without detail, and earlier leftovers).
    """
    await drain_detail_queue(source, session, ctx)
    await drain_stream_queue(source, session, ctx)
