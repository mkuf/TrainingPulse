"""Detect Strava-era schema and wipe/recreate core activity tables."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from models import Base

logger = logging.getLogger(__name__)

# Tables owned by core activity sync (safe to drop on provider migration)
_CORE_TABLES = (
    "activity_streams",
    "activities",
    "daily_metrics",
    "strava_tokens",
    "oauth_tokens",
    "athlete_settings",
)


async def _table_exists(conn: AsyncConnection, name: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :name"
        ),
        {"name": name},
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


async def migrate_strava_era_schema(conn: AsyncConnection) -> bool:
    """Drop Strava-era activity tables if present so create_all can rebuild.

    Returns True if a wipe was performed.
    """
    if not await _table_exists(conn, "activities"):
        return False

    id_type = await _column_type(conn, "activities", "id")
    has_strava_col = await _column_type(conn, "activities", "strava_detail_synced")
    has_source = await _column_type(conn, "activities", "source")

    needs_wipe = False
    if id_type and id_type in ("bigint", "integer"):
        needs_wipe = True
        logger.warning(
            "Detected Strava-era activities.id type (%s); wiping core tables for "
            "intervals.icu / Google Health string ids",
            id_type,
        )
    elif has_strava_col is not None and has_source is None:
        needs_wipe = True
        logger.warning(
            "Detected Strava-era activity columns; wiping core tables for multi-source schema"
        )
    elif await _table_exists(conn, "strava_tokens"):
        # Leftover token table only — drop it without wiping activities if schema is already new
        if has_source is not None:
            await conn.execute(text("DROP TABLE IF EXISTS strava_tokens CASCADE"))
            logger.info("Dropped leftover strava_tokens table")
            return False
        needs_wipe = True

    if not needs_wipe:
        return False

    for table in _CORE_TABLES:
        await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        logger.info("Dropped table %s", table)
    return True


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
        has_source = await _column_type(conn, "activities", "source")
        if has_source is not None:
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
    """Wipe incompatible Strava-era tables, then create_all for current models."""
    wiped = await migrate_strava_era_schema(conn)
    if wiped:
        logger.warning(
            "Existing Strava activity history was discarded; re-sync from "
            "intervals.icu and/or Google Health"
        )
    await conn.run_sync(Base.metadata.create_all)
    await migrate_fitbit_labels(conn)
