"""Background sync of Withings weight measurements."""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from trainingpulse_common import SimpleSyncState

from withings_plugin.client import WithingsClient, parse_measure_groups
from withings_plugin.config import settings
from withings_plugin.models import WeightMeasurement, WithingsToken

logger = logging.getLogger(__name__)

sync_state = SimpleSyncState()


async def run_sync(session: AsyncSession) -> int:
    token = (await session.execute(select(WithingsToken).limit(1))).scalar_one_or_none()
    if token is None:
        logger.warning("Withings sync skipped: no token")
        return 0

    sync_state.mark_running()
    try:
        client = WithingsClient(session)
        end_ts = int(time.time())
        start_ts = end_ts - settings.SYNC_LOOKBACK_DAYS * 86400
        body = await client.get_measurements(startdate=start_ts, enddate=end_ts)
        rows = parse_measure_groups(body)

        for row in rows:
            stmt = pg_insert(WeightMeasurement).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=["grpid"],
                set_={
                    "measured_at": stmt.excluded.measured_at,
                    "weight_kg": stmt.excluded.weight_kg,
                    "fat_mass_pct": stmt.excluded.fat_mass_pct,
                    "deviceid": stmt.excluded.deviceid,
                },
            )
            await session.execute(stmt)

        await session.commit()
        sync_state.mark_finished(last_inserted=len(rows))
        logger.info("Withings sync complete: %d measurement groups", len(rows))
        return len(rows)
    except Exception as exc:
        sync_state.mark_finished(error=str(exc))
        logger.exception("Withings sync failed")
        raise


async def get_status(session: AsyncSession) -> dict:
    token = (await session.execute(select(WithingsToken).limit(1))).scalar_one_or_none()
    count = (
        await session.execute(select(func.count(WeightMeasurement.id)))
    ).scalar_one()
    latest = (
        await session.execute(
            select(WeightMeasurement)
            .order_by(WeightMeasurement.measured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    snap = sync_state.snapshot()
    return {
        "connected": token is not None,
        "userid": token.userid if token else None,
        "measurement_count": int(count or 0),
        "latest_weight_kg": latest.weight_kg if latest else None,
        "latest_measured_at": latest.measured_at.isoformat() if latest else None,
        "sync": snap,
    }
