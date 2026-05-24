"""Database engine and session management."""

from sqlalchemy.ext.asyncio import AsyncSession
from trainingpulse_common import make_async_engine, make_session_factory

from config import settings

engine = make_async_engine(settings.DATABASE_URL)

async_session = make_session_factory(engine)


async def get_session() -> AsyncSession:
    """Yield a database session."""
    async with async_session() as session:
        yield session
