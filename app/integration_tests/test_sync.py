"""A full sync, from provider HTTP responses to derived rows in Postgres."""

from __future__ import annotations

import pytest
from conftest import ICU_ACTIVITY_ID
from sqlalchemy import func, select, text

from database import async_session, engine
from models import (
    Activity,
    ActivityStream,
    AthleteSettings,
    DailyMetrics,
    DailyNutrition,
    RecordSyncCursor,
)
from schema_migrate import _ACTIVE_VIEWS, ensure_schema
from sync import run_sync, sync_state

# The engine's connection pool belongs to one event loop, so every test and
# fixture here shares the session loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_schema_migration_is_idempotent(empty_database):
    from config import config

    async with engine.begin() as conn:
        await ensure_schema(conn, timezone=config.athlete.timezone)
        views = (
            await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema = 'public'"
                )
            )
        ).scalars()
        assert set(views) == set(_ACTIVE_VIEWS)


async def test_full_sync_stores_and_derives(empty_database, upstream):
    async with async_session() as session:
        await run_sync(session)

    assert sync_state.last_error is None
    async with async_session() as session:
        activity = await session.get(Activity, f"icu_{ICU_ACTIVITY_ID}")
        assert activity is not None
        assert activity.detail_synced and activity.synced_streams
        assert activity.trimp and activity.trimp > 0
        assert activity.weighted_average_watts is not None
        assert activity.best_20min_power is not None
        assert activity.computed_calories_method == "power"

        streams = await session.get(ActivityStream, activity.id)
        assert streams is not None and len(streams.data["watts"]) == 3600

        settings = (await session.execute(select(AthleteSettings))).scalar_one()
        assert settings.athlete_id == 424242
        assert settings.estimated_ftp is not None

        metrics = await session.scalar(select(func.count()).select_from(DailyMetrics))
        assert metrics > 0

        nutrition = (await session.execute(select(DailyNutrition))).scalars().all()
        assert {row.provider for row in nutrition} == {"fddb"}
        assert len(nutrition) >= 3
        assert all(row.kcal > 0 for row in nutrition)

        cursors = (await session.execute(select(RecordSyncCursor))).scalars().all()
        assert [cursor.provider for cursor in cursors] == ["fddb"]


async def test_second_sync_updates_in_place(empty_database, upstream):
    async with async_session() as session:
        await run_sync(session)
        listed = len(upstream.requests)
        await run_sync(session)

    assert sync_state.last_error is None
    # Incremental and reconcile listing, but no second detail or stream fetch.
    again = [r.url.path for r in upstream.requests[listed:]]
    assert not any(path.startswith("/api/v1/activity/") for path in again)
    async with async_session() as session:
        count = await session.scalar(select(func.count()).select_from(Activity))
        assert count == 1


async def test_activity_gone_upstream_is_hidden(empty_database, upstream):
    async with async_session() as session:
        await run_sync(session)
        upstream.activity = {**upstream.activity, "id": "i1"}
        await run_sync(session)

    async with async_session() as session:
        gone = await session.get(Activity, f"icu_{ICU_ACTIVITY_ID}")
        assert gone.deleted_upstream_at is not None
        visible = await session.execute(
            text("SELECT id FROM active_activities ORDER BY id")
        )
        assert visible.scalars().all() == ["icu_i1"]

