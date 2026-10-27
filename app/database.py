"""Database engine and session management."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import config

engine = create_async_engine(config.database.url)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    """Yield a database session."""
    async with async_session() as session:
        yield session
