"""Background sync of FDDB daily nutrition."""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from trainingpulse_common import SimpleSyncState

from fddb_plugin.client import FddbAuthenticationError, FddbClient
from fddb_plugin.config import settings
from fddb_plugin.models import DailyNutrition

logger = logging.getLogger(__name__)

sync_state = SimpleSyncState()


async def run_sync(session: AsyncSession) -> int:
    if not settings.credentials_configured:
        sync_state.mark_finished(error="FDDB credentials not configured")
        logger.warning("FDDB sync skipped: missing credentials")
        return 0

    sync_state.mark_running()
    try:
        end_day = date.today()
        start_day = end_day - timedelta(days=settings.SYNC_LOOKBACK_DAYS)
        client = FddbClient()
        rows = await client.fetch_range(start_day, end_day)

        upserted = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            stmt = pg_insert(DailyNutrition).values(
                date=row.day,
                kcal=row.kcal,
                protein_g=row.protein_g,
                carbs_g=row.carbs_g,
                sugar_g=row.sugar_g,
                fat_g=row.fat_g,
                fiber_g=row.fiber_g,
                synced_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["date"],
                set_={
                    "kcal": stmt.excluded.kcal,
                    "protein_g": stmt.excluded.protein_g,
                    "carbs_g": stmt.excluded.carbs_g,
                    "sugar_g": stmt.excluded.sugar_g,
                    "fat_g": stmt.excluded.fat_g,
                    "fiber_g": stmt.excluded.fiber_g,
                    "synced_at": stmt.excluded.synced_at,
                },
            )
            await session.execute(stmt)
            upserted += 1

        await session.commit()
        sync_state.mark_finished(last_upserted=upserted)
        logger.info("FDDB sync complete: %d days upserted", upserted)
        return upserted
    except FddbAuthenticationError as exc:
        sync_state.mark_finished(error=str(exc))
        logger.exception("FDDB sync failed: authentication")
        raise
    except Exception as exc:
        sync_state.mark_finished(error=str(exc))
        logger.exception("FDDB sync failed")
        raise


async def get_status(session: AsyncSession) -> dict:
    count = (await session.execute(select(func.count(DailyNutrition.id)))).scalar_one()
    latest = (
        await session.execute(
            select(DailyNutrition).order_by(DailyNutrition.date.desc()).limit(1)
        )
    ).scalar_one_or_none()
    earliest = (
        await session.execute(
            select(DailyNutrition).order_by(DailyNutrition.date.asc()).limit(1)
        )
    ).scalar_one_or_none()

    return {
        "configured": settings.credentials_configured,
        "day_count": int(count or 0),
        "latest_date": latest.date.isoformat() if latest else None,
        "latest_kcal": round(latest.kcal, 1) if latest else None,
        "earliest_date": earliest.date.isoformat() if earliest else None,
        "sync": sync_state.snapshot(),
    }
