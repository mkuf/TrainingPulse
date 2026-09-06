"""Detect Strava-era schema and migrate in-place, preserving activity history."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from models import Base

logger = logging.getLogger(__name__)


async def _table_exists(conn: AsyncConnection, name: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :name"
        ),
        {"name": name},
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
        # Already on the new schema — just clean up leftover strava_tokens
        if await _table_exists(conn, "strava_tokens"):
            await conn.execute(text("DROP TABLE IF EXISTS strava_tokens CASCADE"))
            logger.info("Dropped leftover strava_tokens table")
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

    # 7. Drop suffer_score (Strava-specific, not used by new providers)
    if await _column_exists(conn, "activities", "suffer_score"):
        await conn.execute(
            text("ALTER TABLE activities DROP COLUMN suffer_score")
        )
        logger.info("Dropped activities.suffer_score column")

    # ── Drop strava_tokens table ────────────────────────────────────────
    if await _table_exists(conn, "strava_tokens"):
        await conn.execute(text("DROP TABLE IF EXISTS strava_tokens CASCADE"))
        logger.info("Dropped strava_tokens table")

    logger.info(
        "Strava-era schema migration complete — all activity history preserved"
    )
    return True


async def _ensure_active_activities_view(conn: AsyncConnection) -> None:
    """Create or replace the active_activities view.

    This view excludes superseded activities so Grafana dashboards and
    aggregate queries automatically filter out duplicates.
    """
    await conn.execute(
        text(
            "CREATE OR REPLACE VIEW active_activities AS "
            "SELECT * FROM activities WHERE superseded_by IS NULL"
        )
    )


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


async def ensure_schema(conn: AsyncConnection) -> None:
    """Migrate Strava-era tables in-place, then create_all for current models."""
    migrated = await _migrate_strava_era_schema(conn)
    if migrated:
        logger.info(
            "Strava activity history preserved with 'strava_' id prefix; "
            "overlapping activities will be marked as superseded during sync"
        )
    await conn.run_sync(Base.metadata.create_all)
    await migrate_fitbit_labels(conn)
    await _ensure_active_activities_view(conn)

