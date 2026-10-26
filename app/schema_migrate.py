"""Migrate older schemas in place, preserving every stored row.

Two generations of history are handled: the Strava-era activity tables, and
the per-plugin ``withings`` / ``fddb`` schemas that body and nutrition data
lived in before they became core data kinds.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from models import Base

logger = logging.getLogger(__name__)

#: How far back to re-queue preserved Strava activities for a detail refresh
#: when ``suffer_score`` is restored. Bounded so the migration cannot consume
#: an entire day's tight Strava read budget on multi-year-old history.
_SUFFER_SCORE_BACKFILL_WINDOW_DAYS = 90


async def _table_exists(
    conn: AsyncConnection, name: str, *, schema: str = "public"
) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :name"
        ),
        {"schema": schema, "name": name},
    )
    return result.scalar_one_or_none() is not None


async def _column_exists(conn: AsyncConnection, table: str, column: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table "
            "AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar_one_or_none() is not None


async def _column_type(conn: AsyncConnection, table: str, column: str) -> str | None:
    result = await conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table "
            "AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar_one_or_none()


async def _migrate_strava_era_schema(conn: AsyncConnection) -> bool:
    """Migrate Strava-era activity tables in-place, preserving all data.

    Detects the Strava-era schema (BigInteger id, strava_detail_synced column,
    no source column) and transforms it to the multi-source string-id schema
    without dropping any data.

    Returns True if a migration was performed.
    """
    if not await _table_exists(conn, "activities"):
        return False

    id_type = await _column_type(conn, "activities", "id")
    has_strava_col = await _column_exists(conn, "activities", "strava_detail_synced")
    has_source = await _column_exists(conn, "activities", "source")

    needs_migration = False
    if id_type and id_type in ("bigint", "integer"):
        needs_migration = True
        logger.info(
            "Detected Strava-era activities.id type (%s); migrating to "
            "multi-source string-id schema in-place",
            id_type,
        )
    elif has_strava_col and not has_source:
        needs_migration = True
        logger.info(
            "Detected Strava-era activity columns; migrating to multi-source schema"
        )

    if not needs_migration:
        # Already on the new schema. strava_tokens (if any) is migrated and
        # dropped later in ``ensure_schema``, once oauth_tokens is guaranteed
        # to exist.
        return False

    # ── Migrate activity_streams first (FK references activities.id) ────
    if await _table_exists(conn, "activity_streams"):
        stream_id_type = await _column_type(conn, "activity_streams", "activity_id")
        if stream_id_type and stream_id_type in ("bigint", "integer"):
            # Drop any foreign key constraints pointing to activities.id before altering types
            fk_result = await conn.execute(
                text(
                    """
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_name = 'activity_streams'
                      AND kcu.column_name = 'activity_id'
                    """
                )
            )
            for fk_row in fk_result.fetchall():
                logger.info("Dropping foreign key constraint %s", fk_row.constraint_name)
                await conn.execute(
                    text(f"ALTER TABLE activity_streams DROP CONSTRAINT {fk_row.constraint_name}")
                )

            logger.info("Migrating activity_streams.activity_id to VARCHAR(128)")
            await conn.execute(
                text(
                    "ALTER TABLE activity_streams "
                    "ALTER COLUMN activity_id TYPE VARCHAR(128) "
                    "USING activity_id::text"
                )
            )
            await conn.execute(
                text(
                    "UPDATE activity_streams "
                    "SET activity_id = 'strava_' || activity_id "
                    "WHERE activity_id NOT LIKE 'strava\\_%' ESCAPE '\\'"
                )
            )
            logger.info("Prefixed activity_streams.activity_id with 'strava_'")

    # ── Migrate activities table ────────────────────────────────────────
    # 1. Change PK type from BigInteger to VARCHAR(128)
    if id_type and id_type in ("bigint", "integer"):
        logger.info("Migrating activities.id to VARCHAR(128)")
        await conn.execute(
            text(
                "ALTER TABLE activities "
                "ALTER COLUMN id TYPE VARCHAR(128) "
                "USING id::text"
            )
        )
        # Prefix existing IDs
        await conn.execute(
            text(
                "UPDATE activities SET id = 'strava_' || id "
                "WHERE id NOT LIKE 'strava\\_%' ESCAPE '\\'"
            )
        )
        logger.info("Prefixed activities.id with 'strava_'")

    # 2. Add source column
    if not has_source:
        await conn.execute(
            text(
                "ALTER TABLE activities "
                "ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'strava'"
            )
        )
        logger.info("Added activities.source column (default='strava')")
        # Create index on source
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_activities_source "
                "ON activities (source)"
            )
        )

    # 3. Add superseded_by column
    if not await _column_exists(conn, "activities", "superseded_by"):
        await conn.execute(
            text(
                "ALTER TABLE activities "
                "ADD COLUMN superseded_by VARCHAR(128)"
            )
        )
        logger.info("Added activities.superseded_by column")

    # 4. Add training_load column
    if not await _column_exists(conn, "activities", "training_load"):
        await conn.execute(
            text("ALTER TABLE activities ADD COLUMN training_load FLOAT")
        )
        logger.info("Added activities.training_load column")

    # 5. Rename strava_detail_synced → detail_synced
    if has_strava_col:
        if not await _column_exists(conn, "activities", "detail_synced"):
            await conn.execute(
                text(
                    "ALTER TABLE activities "
                    "RENAME COLUMN strava_detail_synced TO detail_synced"
                )
            )
            logger.info("Renamed strava_detail_synced → detail_synced")
        else:
            # Both columns exist (shouldn't happen, but be safe)
            await conn.execute(
                text(
                    "ALTER TABLE activities DROP COLUMN IF EXISTS strava_detail_synced"
                )
            )

    # 6. Rename strava_updated_at → provider_updated_at
    if await _column_exists(conn, "activities", "strava_updated_at"):
        if not await _column_exists(conn, "activities", "provider_updated_at"):
            await conn.execute(
                text(
                    "ALTER TABLE activities "
                    "RENAME COLUMN strava_updated_at TO provider_updated_at"
                )
            )
            logger.info("Renamed strava_updated_at → provider_updated_at")
        else:
            await conn.execute(
                text(
                    "ALTER TABLE activities DROP COLUMN IF EXISTS strava_updated_at"
                )
            )

    # 7. suffer_score is Strava-only but no longer dropped: Strava is a
    #    first-class source again, so a Strava-era database keeps its values.

    # strava_tokens is migrated into oauth_tokens and dropped later in
    # ``ensure_schema``, once oauth_tokens is guaranteed to exist.

    logger.info(
        "Strava-era schema migration complete — all activity history preserved"
    )
    return True


async def _migrate_strava_tokens(conn: AsyncConnection) -> None:
    """Carry an existing Strava OAuth token over into ``oauth_tokens``.

    Must run after ``Base.metadata.create_all`` so ``oauth_tokens`` exists.
    Idempotent: once ``strava_tokens`` is dropped this is a no-op on every
    later startup, and ``ON CONFLICT DO NOTHING`` protects a rare case where
    both tables already have a row for ``provider = 'strava'``.
    """
    if not await _table_exists(conn, "strava_tokens"):
        return

    result = await conn.execute(
        text(
            "INSERT INTO oauth_tokens "
            "(provider, access_token, refresh_token, expires_at, external_user_id) "
            "SELECT 'strava', access_token, refresh_token, expires_at, "
            "athlete_id::text FROM strava_tokens "
            "ORDER BY updated_at DESC LIMIT 1 "
            "ON CONFLICT (provider) DO NOTHING"
        )
    )
    if result.rowcount:
        logger.info("Migrated existing Strava OAuth token into oauth_tokens")
    else:
        logger.info(
            "strava_tokens had no token to migrate, or oauth_tokens already "
            "had one for 'strava'"
        )

    await conn.execute(text("DROP TABLE IF EXISTS strava_tokens CASCADE"))
    logger.info("Dropped strava_tokens table")


async def _restore_suffer_score(conn: AsyncConnection) -> None:
    """Re-add ``activities.suffer_score`` and adopt preserved Strava rows.

    An earlier release dropped this column when Strava was replaced by
    intervals.icu and Google Health. Strava is a source again, so the column
    comes back and the preserved ``strava_<id>`` rows — whose ids are still real
    Strava activity ids — are re-queued for a detail fetch to recover the lost
    values and pick up any upstream edits.

    Guarded on the column's absence, so it runs exactly once per database.
    ``create_all`` adds the column on a fresh database, where there is nothing
    to adopt; a Strava-era database still has its values and is left alone.

    The re-queue is bounded to the last ``_SUFFER_SCORE_BACKFILL_WINDOW_DAYS``
    days rather than a user's entire history: Strava's read budget is tight
    (100/15min, 1000/day), and unconditionally re-queuing years of already-synced
    activities right after an upgrade would consume that whole budget on old
    data instead of the current sync. Older rows simply keep ``suffer_score``
    unset until a future detail fetch (e.g. a manual full resync) touches them.

    Streams are deliberately *not* re-queued: the earlier migration re-prefixed
    ``activity_streams`` rather than deleting them, so the time series survived.
    Re-fetching them would burn Strava's tight read budget to replace data we
    already hold.
    """
    if not await _table_exists(conn, "activities"):
        return
    if await _column_exists(conn, "activities", "suffer_score"):
        return

    await conn.execute(text("ALTER TABLE activities ADD COLUMN suffer_score FLOAT"))
    logger.info("Re-added activities.suffer_score column")

    result = await conn.execute(
        text(
            "UPDATE activities SET detail_synced = false "
            "WHERE source = 'strava' AND detail_synced = true "
            "AND start_date >= now() - make_interval(days => :window_days)"
        ),
        {"window_days": _SUFFER_SCORE_BACKFILL_WINDOW_DAYS},
    )
    adopted = result.rowcount or 0
    if adopted:
        logger.info(
            "Queued %d preserved Strava activities (last %d days) for a "
            "detail refresh; only the ones that win on precedence will be "
            "fetched",
            adopted,
            _SUFFER_SCORE_BACKFILL_WINDOW_DAYS,
        )


async def _add_deleted_upstream_at(conn: AsyncConnection) -> None:
    """Add ``activities.deleted_upstream_at`` to databases that predate it.

    ``create_all`` only creates missing tables, never missing columns.
    """
    if not await _table_exists(conn, "activities"):
        return
    if await _column_exists(conn, "activities", "deleted_upstream_at"):
        return
    await conn.execute(
        text("ALTER TABLE activities ADD COLUMN deleted_upstream_at TIMESTAMPTZ")
    )
    logger.info("Added activities.deleted_upstream_at column")


async def _add_computed_energy(conn: AsyncConnection) -> None:
    """Add the ``computed_*`` energy columns, renaming the ``estimated_*`` ones.

    Activities are only computed when their streams are processed, so older
    rows keep NULL and show their imported values.
    """
    if not await _table_exists(conn, "activities"):
        return
    for old, new, sql_type in (
        ("estimated_calories", "computed_calories", "FLOAT"),
        ("estimated_calories_method", "computed_calories_method", "VARCHAR(16)"),
        (None, "computed_kilojoules", "FLOAT"),
    ):
        if await _column_exists(conn, "activities", new):
            continue
        if old and await _column_exists(conn, "activities", old):
            await conn.execute(
                text(f"ALTER TABLE activities RENAME COLUMN {old} TO {new}")
            )
            logger.info("Renamed activities.%s to %s", old, new)
        else:
            await conn.execute(
                text(f"ALTER TABLE activities ADD COLUMN {new} {sql_type}")
            )
            logger.info("Added activities.%s column", new)


async def _backfill_hammerhead_kilojoules(conn: AsyncConnection) -> None:
    """Derive ``kilojoules`` for Hammerhead rides mapped before it was.

    Same rule as ``hammerhead.map_row``; rows that already have a value are
    left alone, so this is a no-op after the first run.
    """
    if not await _table_exists(conn, "activities"):
        return
    result = await conn.execute(
        text(
            "UPDATE activities SET kilojoules = average_watts * moving_time / 1000.0 "
            "WHERE source = 'hammerhead' AND kilojoules IS NULL AND device_watts "
            "AND average_watts > 0 AND moving_time > 0"
        )
    )
    if result.rowcount:
        logger.info("Derived kilojoules for %d Hammerhead activities", result.rowcount)


#: View name → (table, condition). Each holds only the visible rows.
_ACTIVE_VIEWS = {
    "active_activities": (
        "activities",
        "superseded_by IS NULL AND deleted_upstream_at IS NULL",
    ),
    "active_body_measurements": ("body_measurements", "superseded_by IS NULL"),
    "active_daily_nutrition": ("daily_nutrition", "superseded_by IS NULL"),
}


async def _ensure_active_views(conn: AsyncConnection) -> None:
    """Create the views Grafana and aggregates read, excluding duplicates.

    Each view is dropped and rebuilt rather than replaced: it is a
    ``SELECT *``, whose column list is fixed at creation time, and
    ``CREATE OR REPLACE VIEW`` can only *append* columns. Re-adding
    ``suffer_score`` puts a column back in the middle of ``activities``, which
    a replace would reject.
    """
    for view, (table, condition) in _ACTIVE_VIEWS.items():
        await conn.execute(text(f"DROP VIEW IF EXISTS {view}"))
        await conn.execute(
            text(f"CREATE VIEW {view} AS SELECT * FROM {table} WHERE {condition}")
        )


async def _drop_schema_if_empty(conn: AsyncConnection, schema: str) -> None:
    result = await conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = :schema"
        ),
        {"schema": schema},
    )
    if (result.scalar_one() or 0) == 0:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema}"))
        logger.info("Dropped empty %s schema", schema)
    else:
        # Something we did not create lives there; leave it alone.
        logger.warning("Left %s schema in place: it still contains tables", schema)


async def _migrate_withings_plugin(conn: AsyncConnection, timezone: str) -> None:
    """Move ``withings.*`` into ``body_measurements`` and ``oauth_tokens``.

    ``day`` is derived in ``timezone`` exactly as new rows are. Idempotent: the
    source tables are dropped once copied, and ``ON CONFLICT DO NOTHING``
    keeps any rows a newer sync already wrote.
    """
    if await _table_exists(conn, "weight_measurements", schema="withings"):
        result = await conn.execute(
            text(
                "INSERT INTO body_measurements "
                "(provider, external_id, measured_at, day, weight_kg, fat_pct, "
                "device, synced_at) "
                "SELECT 'withings', grpid::text, measured_at, "
                "(measured_at AT TIME ZONE CAST(:tz AS text))::date, weight_kg, "
                "fat_mass_pct, deviceid, synced_at "
                "FROM withings.weight_measurements "
                "ON CONFLICT (provider, external_id) DO NOTHING"
            ),
            {"tz": timezone},
        )
        logger.info(
            "Moved %d Withings weigh-ins into body_measurements", result.rowcount or 0
        )
        await conn.execute(text("DROP TABLE withings.weight_measurements"))

    if await _table_exists(conn, "withings_tokens", schema="withings"):
        result = await conn.execute(
            text(
                "INSERT INTO oauth_tokens "
                "(provider, access_token, refresh_token, expires_at, external_user_id) "
                "SELECT 'withings', access_token, refresh_token, expires_at, "
                "userid::text FROM withings.withings_tokens "
                "ORDER BY updated_at DESC LIMIT 1 "
                "ON CONFLICT (provider) DO NOTHING"
            )
        )
        if result.rowcount:
            logger.info("Moved the Withings OAuth token into oauth_tokens")
        await conn.execute(text("DROP TABLE withings.withings_tokens"))

    if await _schema_exists(conn, "withings"):
        await _drop_schema_if_empty(conn, "withings")


async def _migrate_fddb_plugin(conn: AsyncConnection) -> None:
    """Move ``fddb.daily_nutrition`` into the core ``daily_nutrition`` table."""
    if await _table_exists(conn, "daily_nutrition", schema="fddb"):
        result = await conn.execute(
            text(
                "INSERT INTO daily_nutrition "
                "(provider, day, kcal, protein_g, carbs_g, sugar_g, fat_g, "
                "fiber_g, synced_at) "
                "SELECT 'fddb', date, kcal, protein_g, carbs_g, sugar_g, fat_g, "
                "fiber_g, synced_at FROM fddb.daily_nutrition "
                "ON CONFLICT (provider, day) DO NOTHING"
            )
        )
        logger.info(
            "Moved %d FDDB diary days into daily_nutrition", result.rowcount or 0
        )
        await conn.execute(text("DROP TABLE fddb.daily_nutrition"))

    if await _schema_exists(conn, "fddb"):
        await _drop_schema_if_empty(conn, "fddb")


async def _schema_exists(conn: AsyncConnection, schema: str) -> bool:
    result = await conn.execute(
        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
        {"schema": schema},
    )
    return result.scalar_one_or_none() is not None


async def migrate_fitbit_labels(conn: AsyncConnection) -> None:
    """Rename short-lived fitbit provider labels to google_health (no data wipe)."""
    if await _table_exists(conn, "oauth_tokens"):
        await conn.execute(
            text(
                "UPDATE oauth_tokens SET provider = 'google_health' "
                "WHERE provider = 'fitbit'"
            )
        )
    if await _table_exists(conn, "activities"):
        has_source = await _column_exists(conn, "activities", "source")
        if has_source:
            await conn.execute(
                text(
                    "UPDATE activities SET source = 'google_health' "
                    "WHERE source = 'fitbit'"
                )
            )
            # Rename activity id prefix fitbit_ → ghealth_ (and matching streams)
            if await _table_exists(conn, "activity_streams"):
                await conn.execute(
                    text(
                        "UPDATE activity_streams SET activity_id = "
                        "'ghealth_' || substr(activity_id, 8) "
                        "WHERE activity_id LIKE 'fitbit\\_%' ESCAPE '\\'"
                    )
                )
            await conn.execute(
                text(
                    "UPDATE activities SET id = 'ghealth_' || substr(id, 8) "
                    "WHERE id LIKE 'fitbit\\_%' ESCAPE '\\'"
                )
            )


async def ensure_schema(conn: AsyncConnection, *, timezone: str = "UTC") -> None:
    """Migrate older tables in place, then create_all for current models.

    Runs inside one transaction (the caller's ``engine.begin()``), so a failed
    step leaves the database exactly as it was. ``timezone`` is
    ``athlete.timezone``, used to assign migrated weigh-ins to a day.
    """
    migrated = await _migrate_strava_era_schema(conn)
    if migrated:
        logger.info(
            "Strava activity history preserved with 'strava_' id prefix; "
            "overlapping activities will be marked as superseded during sync"
        )
    await conn.run_sync(Base.metadata.create_all)
    await _migrate_strava_tokens(conn)
    await migrate_fitbit_labels(conn)
    await _restore_suffer_score(conn)
    await _add_deleted_upstream_at(conn)
    await _add_computed_energy(conn)
    await _backfill_hammerhead_kilojoules(conn)
    await _migrate_withings_plugin(conn, timezone)
    await _migrate_fddb_plugin(conn)
    await _ensure_active_views(conn)

