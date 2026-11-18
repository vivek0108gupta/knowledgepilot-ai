import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models import Base

settings = get_settings()
configure_logging(settings.log_level)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# Several API replicas start together and each runs `alembic upgrade head`. This lock makes
# them run one at a time: the first migrates, the others then find nothing to do.
MIGRATION_LOCK = "SELECT pg_advisory_lock(hashtextextended('knowledgepilot-migrations', 0))"
MIGRATION_UNLOCK = "SELECT pg_advisory_unlock(hashtextextended('knowledgepilot-migrations', 0))"


def do_run_migrations(connection: Connection) -> None:
    connection.execute(text(MIGRATION_LOCK))
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text(MIGRATION_UNLOCK))
        connection.commit()


async def run_migrations_online() -> None:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
