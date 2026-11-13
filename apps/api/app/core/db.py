from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

# The engine connects lazily, so importing this module never needs a running database.
engine = create_async_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_size=get_settings().db_pool_size,
    max_overflow=get_settings().db_max_overflow,
    # Give up on a connection that is accepted but never completes, instead of holding a
    # request for asyncpg's default of a full minute.
    connect_args={"timeout": 10},
    # Keep bound parameters (message text, document text, tokens) out of exception messages,
    # which are logged.
    hide_parameters=True,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields one database session per request."""
    async with SessionLocal() as session:
        yield session
