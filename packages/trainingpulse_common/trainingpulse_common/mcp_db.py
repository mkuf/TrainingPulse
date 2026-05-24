"""Read-only database sessions for MCP tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@asynccontextmanager
async def readonly_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
