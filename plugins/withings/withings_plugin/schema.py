"""Create tables and apply lightweight schema fixes."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from withings_plugin.models import Base

logger = logging.getLogger(__name__)

_ALTER_GRPID = text(
    "ALTER TABLE weight_measurements "
    "ALTER COLUMN grpid TYPE BIGINT USING grpid::bigint"
)
_ALTER_USERID = text(
    "ALTER TABLE withings_tokens "
    "ALTER COLUMN userid TYPE BIGINT USING userid::bigint"
)


async def ensure_schema(conn: AsyncConnection) -> None:
    await conn.run_sync(Base.metadata.create_all)
    for label, stmt in (
        ("weight_measurements.grpid", _ALTER_GRPID),
        ("withings_tokens.userid", _ALTER_USERID),
    ):
        try:
            await conn.execute(stmt)
        except Exception as exc:
            if "does not exist" not in str(exc).lower():
                logger.warning("Schema migration skipped for %s: %s", label, exc)
