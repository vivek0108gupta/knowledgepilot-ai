"""`GET /metrics`: operational metrics for the operator's monitoring, not for users.

Off unless METRICS_TOKEN is set; then it needs `Authorization: Bearer <METRICS_TOKEN>`. It is
not part of the user API: no Supabase session gives access, so workspace members (owners
included) never see operational data. Expose it only on the private network, or keep the
token secret. Nothing here is per user or per workspace."""

import hmac
import logging
import resource
import sys
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import engine, get_session
from app.core.errors import AppError
from app.core.metrics import metrics
from app.models.usage import IngestionJob, UsageEvent

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

STARTED = time.time()


def authorised(request: Request) -> None:
    token = get_settings().metrics_token.get_secret_value()
    if not token:
        # Indistinguishable from a route that does not exist.
        raise AppError(404, "not_found", "Not found.")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise AppError(401, "unauthorized", "Not authorised.")


async def database_gauges(session: AsyncSession) -> dict:
    gauges: dict = {}
    # Bounded: a slow database must not make the scrape hang.
    await session.execute(text("SET LOCAL statement_timeout = '2s'"))
    jobs = (
        await session.execute(
            select(IngestionJob.status, func.count()).group_by(IngestionJob.status)
        )
    ).all()
    gauges["ingestion_jobs"] = (
        "Ingestion jobs by status (all replicas)",
        [({"status": status}, count) for status, count in jobs],
    )
    oldest = await session.scalar(
        select(func.extract("epoch", func.now() - func.min(IngestionJob.available_at))).where(
            IngestionJob.status == "queued", IngestionJob.available_at <= func.now()
        )
    )
    gauges["ingestion_oldest_queued_seconds"] = (
        "Age of the oldest job waiting to run (queue wait)",
        [({}, float(oldest or 0))],
    )
    stalled = await session.scalar(
        select(func.count())
        .select_from(IngestionJob)
        .where(IngestionJob.status == "running", IngestionJob.leased_until < func.now())
    )
    gauges["ingestion_jobs_stalled"] = (
        "Running jobs past their lease (should be 0)",
        [({}, stalled or 0)],
    )
    open_rows = (
        await session.execute(
            select(UsageEvent.operation, func.count())
            .where(UsageEvent.status == "reserved")
            .group_by(UsageEvent.operation)
        )
    ).all()
    gauges["usage_reservations_open"] = (
        "Open usage reservations by operation",
        [({"operation": op}, count) for op, count in open_rows],
    )
    overdue = await session.scalar(
        select(func.count())
        .select_from(UsageEvent)
        .where(
            UsageEvent.status == "reserved",
            UsageEvent.expires_at < func.now(),
        )
    )
    gauges["usage_reservations_overdue"] = (
        "Open reservations past their expiry (should be 0 after a sweep)",
        [({}, overdue or 0)],
    )
    connections = await session.scalar(
        text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
    )
    gauges["database_connections"] = (
        "Connections to this database (all clients)",
        [({}, connections or 0)],
    )
    await session.rollback()
    return gauges


@router.get("/metrics", dependencies=[Depends(authorised)])
async def metrics_endpoint(session: AsyncSession = Depends(get_session)) -> PlainTextResponse:
    pool = engine.pool
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    peak_rss = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
    gauges: dict = {
        "process_cpu_seconds": (
            "CPU time used by this process",
            [({}, usage.ru_utime + usage.ru_stime)],
        ),
        "process_peak_resident_bytes": ("Peak resident memory of this process", [({}, peak_rss)]),
        "process_uptime_seconds": (
            "Seconds since this process started",
            [({}, time.time() - STARTED)],
        ),
        "db_pool_checked_out": (
            "Connections this process holds from its pool",
            [({}, getattr(pool, "checkedout", lambda: 0)())],
        ),
    }
    try:
        gauges.update(await database_gauges(session))
        gauges["metrics_database_up"] = ("Whether the database gauges could be read", [({}, 1)])
    except SQLAlchemyError as error:
        await session.rollback()
        logger.warning("Metrics could not read the database: %s", type(error).__name__)
        gauges["metrics_database_up"] = ("Whether the database gauges could be read", [({}, 0)])
    return PlainTextResponse(
        metrics.render(gauges), media_type="text/plain; version=0.0.4; charset=utf-8"
    )
