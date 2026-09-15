"""Activity sync orchestration.

This module owns the *order* of a sync run. All per-provider behaviour lives in
:mod:`sources` and all phase mechanics live in :mod:`sources.engine`.

A run is deliberately two-pass:

1. **List** — every enabled source, in precedence order, lists its window and
   stores summary rows.
2. **Supersede** — ``superseded_by`` is recomputed across every stored row.
3. **Enrich** — each source fetches detail and streams, but only for activities
   that are not superseded. A provider with a tight budget therefore never
   spends requests on activities that are hidden behind a better source.
4. **Supersede again** — detail can change ``elapsed_time``, which can change
   which activities overlap. The pass is idempotent and cheap.
5. **Derive** — estimated FTP, power curves, and daily CTL/ATL/TSB.

The second pass is what makes precedence reversible: flipping
``sources.precedence`` re-queues the new winners for enrichment on the next run
instead of requiring a re-backfill.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import config
from database import async_session
from metrics import (
    calculate_power_curve,
    get_hr_zone_boundaries,
    get_power_zone_boundaries,
    recalculate_daily_metrics,
)
from models import Activity, ActivityStream, AthleteSettings
from sources.base import ActivitySource, SourceRateLimitExceeded
from sources.engine import (
    EngineContext,
    recompute_supersessions,
    refresh_progress,
    run_enrich_phase,
    run_list_phase,
)
from sources.registry import build_enabled_sources
from sources.state import SyncState, sync_state

logger = logging.getLogger(__name__)

__all__ = [
    "SyncState",
    "force_full_resync",
    "resolve_athlete_id",
    "run_sync",
    "store_athlete_settings",
    "sync_state",
]


# ── Athlete settings ────────────────────────────────────────────────


async def store_athlete_settings(
    session: AsyncSession, athlete_id: int
) -> AthleteSettings:
    """Project the configured athlete values into ``athlete_settings``.

    Heart-rate and power settings come from configuration only. No provider is
    asked for zones or FTP, so ``hr_zones`` stays NULL and
    :func:`metrics.get_hr_zone_boundaries` computes zones from ``max_hr`` and
    ``rest_hr``.
    """
    stmt = pg_insert(AthleteSettings).values(
        athlete_id=athlete_id,
        max_hr=config.athlete.max_hr,
        rest_hr=config.athlete.rest_hr,
        hr_zones=None,
        ftp=config.athlete.ftp,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["athlete_id"],
        set_={
            "max_hr": stmt.excluded.max_hr,
            "rest_hr": stmt.excluded.rest_hr,
            "hr_zones": stmt.excluded.hr_zones,
            "ftp": stmt.excluded.ftp,
        },
    )
    await session.execute(stmt)
    await session.commit()

    result = await session.execute(
        select(AthleteSettings).where(AthleteSettings.athlete_id == athlete_id)
    )
    return result.scalar_one()


async def resolve_athlete_id(
    session: AsyncSession, sources: list[ActivitySource] | None = None
) -> int:
    """Pick a stable local athlete id.

    An existing row always wins so upgrades keep the id their activities were
    stored under. Otherwise the highest-precedence source that can name an
    athlete provides one, and failing that we fall back to 1 — this is
    single-athlete software and the value is only an internal key.
    """
    existing = await session.execute(select(AthleteSettings).limit(1))
    row = existing.scalar_one_or_none()
    if row is not None:
        return row.athlete_id

    for source in sources or []:
        try:
            provider_id = await source.verify_credentials()
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            logger.warning("%s credential check failed: %s", source.name, exc)
            continue
        if provider_id:
            return provider_id

    return 1


# ── Shared post-processing ──────────────────────────────────────────


async def _recalculate_estimated_ftp(session: AsyncSession, athlete_id: int) -> None:
    result = await session.execute(
        select(func.max(Activity.best_20min_power)).where(
            Activity.athlete_id == athlete_id,
            Activity.superseded_by == None,  # noqa: E711
        )
    )
    best_20min = result.scalar_one_or_none()
    if best_20min:
        estimated_ftp = int(best_20min * 0.95)
        logger.info(
            "Recalculated estimated FTP: %d W (best 20min %.1f W)",
            estimated_ftp,
            best_20min,
        )
        await session.execute(
            update(AthleteSettings)
            .where(AthleteSettings.athlete_id == athlete_id)
            .values(estimated_ftp=estimated_ftp)
        )
        await session.commit()


async def _backfill_power_curves(session: AsyncSession, athlete_id: int) -> None:
    result = await session.execute(
        select(Activity, ActivityStream.data)
        .join(ActivityStream, Activity.id == ActivityStream.activity_id)
        .where(
            Activity.athlete_id == athlete_id,
            Activity.power_curve == None,  # noqa: E711
        )
    )
    to_process = result.all()
    if not to_process:
        return
    for activity, stream_data in to_process:
        power_stream = stream_data.get("watts")
        if power_stream:
            curve = calculate_power_curve(power_stream)
            if curve:
                await session.execute(
                    update(Activity)
                    .where(Activity.id == activity.id)
                    .values(power_curve=curve)
                )
    await session.commit()


async def force_full_resync(session: AsyncSession, athlete_id: int) -> None:
    """Re-queue every activity for detail, streams, and metric recalculation.

    Every source is re-fetchable now, including Strava, so nothing is excluded.
    """
    logger.info("Resetting sync state for athlete %d", athlete_id)
    await session.execute(
        update(Activity)
        .where(Activity.athlete_id == athlete_id)
        .values(
            synced_streams=False,
            detail_synced=False,
            trimp=None,
            hr_zone_seconds=None,
            power_zone_seconds=None,
            best_20min_power=None,
            power_curve=None,
        )
    )
    await session.commit()


# ── Main entry ──────────────────────────────────────────────────────


async def refresh_rate_limits() -> None:
    """Ask every enabled source for its quota usage, outside a sync run."""
    sources = build_enabled_sources(config, async_session)
    for source in sources:
        try:
            if not await source.is_connected():
                continue
            await source.verify_credentials()
        except Exception as exc:  # noqa: BLE001 - status page only
            logger.warning("%s rate-limit refresh failed: %s", source.name, exc)
        finally:
            sync_state.snapshot_rate_limits(source)
            await source.close()


async def run_sync(session: AsyncSession, *, force_resync: bool = False) -> None:
    """Run one full sync across every enabled activity source."""
    if sync_state.is_running:
        logger.info("Sync already running; skipping this trigger")
        return

    sync_state.is_running = True
    sync_state.last_error = None
    sync_state.begin("starting")

    sources = build_enabled_sources(config, async_session)
    if not sources:
        sync_state.is_running = False
        sync_state.begin("idle")
        sync_state.last_error = (
            "No activity source is enabled. Set sources.<name>.enabled in "
            "config.yaml for intervals.icu, Strava, or Google Health."
        )
        logger.warning(sync_state.last_error)
        return

    try:
        # Only sources that can actually authenticate take part in this run.
        # A configured-but-not-yet-authorized OAuth source is normal, not an
        # error: the user has simply not finished connecting it.
        live: list[ActivitySource] = []
        for source in sources:
            try:
                connected = await source.is_connected()
            except Exception as exc:  # noqa: BLE001 - reported below
                logger.warning("%s connection check failed: %s", source.name, exc)
                connected = False
            if connected:
                live.append(source)
            else:
                logger.info("%s is enabled but not connected; skipping", source.name)

        if not live:
            sync_state.last_error = (
                "No activity source is connected yet. Finish authorizing from "
                "the home page."
            )
            logger.warning(sync_state.last_error)
            return

        athlete_id = await resolve_athlete_id(session, live)
        athlete_settings = await store_athlete_settings(session, athlete_id)

        if force_resync:
            await force_full_resync(session, athlete_id)

        ctx = EngineContext(
            session_factory=async_session,
            athlete_id=athlete_id,
            state=sync_state,
            backfill_years=config.sync.backfill_years,
            window_days=config.sync.window_days,
            reconcile_days=config.sync.reconcile_days,
            detail_concurrency=config.sync.detail_concurrency,
            streams_concurrency=config.sync.streams_concurrency,
            hr_zone_boundaries=get_hr_zone_boundaries(
                athlete_settings.max_hr,
                athlete_settings.rest_hr,
                athlete_settings.hr_zones,
            ),
            power_zone_boundaries=get_power_zone_boundaries(athlete_settings.ftp),
        )

        precedence = config.sources.precedence

        # Pass 1: list everything. A rate limit in one source must not stop the
        # others, so each is isolated.
        for source in live:
            try:
                await run_list_phase(
                    source, session, ctx, force_resync=force_resync
                )
            except SourceRateLimitExceeded as exc:
                sync_state.last_error = (
                    f"{exc.source} rate limit while listing activities; "
                    "will resume next sync."
                )
                logger.warning(sync_state.last_error)
            except Exception as exc:  # noqa: BLE001 - keep other sources alive
                sync_state.last_error = f"{source.name} listing failed: {exc}"
                logger.exception("%s listing failed", source.name)

        # Pass 2: decide what is visible before spending any per-activity calls.
        sync_state.begin("deduplicating")
        changed = await recompute_supersessions(session, athlete_id, precedence)
        if changed:
            logger.info("Supersession updated for %d activities", changed)

        # Pass 3: enrich only the winners.
        for source in live:
            try:
                await run_enrich_phase(source, session, ctx)
            except SourceRateLimitExceeded as exc:
                sync_state.last_error = (
                    f"{exc.source} rate limit while enriching activities; "
                    "will resume next sync."
                )
                logger.warning(sync_state.last_error)
            except Exception as exc:  # noqa: BLE001 - keep other sources alive
                sync_state.last_error = f"{source.name} enrichment failed: {exc}"
                logger.exception("%s enrichment failed", source.name)

        # Detail can change durations, which can change what overlaps.
        sync_state.begin("deduplicating")
        await recompute_supersessions(session, athlete_id, precedence)

        sync_state.begin("finalizing")
        await _recalculate_estimated_ftp(session, athlete_id)
        await _backfill_power_curves(session, athlete_id)
        await recalculate_daily_metrics(session, athlete_id)
        await refresh_progress(session, athlete_id, sync_state)

        sync_state.last_sync = datetime.now(timezone.utc)
        logger.info("Sync complete for athlete %d", athlete_id)
    finally:
        for source in sources:
            sync_state.snapshot_rate_limits(source)
            try:
                await source.close()
            except Exception as exc:  # noqa: BLE001 - shutdown best effort
                logger.warning("Closing %s failed: %s", source.name, exc)
        sync_state.is_running = False
        sync_state.begin("idle")
