import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import AppError
from app.schemas.health import Health, Readiness

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> Health:
    """Liveness: the process is up. Does not touch the database."""
    return Health(status="ok")


@router.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> Readiness:
    """Readiness: the database answers and the pgvector extension is installed."""
    try:
        result = await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        pgvector_version = result.scalar_one_or_none()
    except Exception as error:
        logger.exception("Readiness check could not reach the database")
        raise AppError(503, "database_unavailable", "Database is unreachable.") from error

    if pgvector_version is None:
        raise AppError(
            503,
            "pgvector_missing",
            "The pgvector extension is not installed. Run the database migrations.",
        )

    return Readiness(status="ok", database="ok", pgvector=pgvector_version)
