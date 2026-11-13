"""Durable ingestion jobs, in PostgreSQL. No other infrastructure.

Lifecycle of a job (`ingestion_jobs`):

    queued ──claim──► running ──► succeeded
       ▲                │   └──► failed            (permanent failure, or no attempts left)
       └──── retry ─────┤                          (transient failure, attempts remain)
       └──── recover ───┘                          (lease expired: the worker stopped)

- **Enqueue** inserts a job for a document, at most one active (queued or running) job per
  document (a partial unique index), so enqueueing twice is harmless. Documents are created
  together with their job, in one transaction.
- **Claim** takes the oldest available job with `FOR UPDATE SKIP LOCKED`, so concurrent
  workers in any number of processes never take the same job. Under a per-workspace advisory
  lock it then checks how many of that workspace's jobs are running, so no workspace exceeds
  INGESTION_WORKERS_PER_WORKSPACE at once and none can occupy every worker.
- **Lease**: a running job belongs to its worker until `leased_until`. The worker extends it
  after every batch. A job whose lease expired (the process stopped) is recovered: queued
  again if attempts remain, failed otherwise. Its usage reservation stops being refreshed and
  is recovered by the usage sweep.
- **Results** are written only by the worker that still holds the lease, so a worker that
  lost its job (it was recovered and taken by another) cannot overwrite the new attempt.
- **Idempotency**: every attempt reserves its usage under `document_embedding:{job}:{attempt}`
  and removes the chunks of any earlier attempt before writing its own, so retries and
  recoveries leave neither duplicate chunks nor duplicate usage.

Tenant isolation: a job carries only document and workspace IDs from the document's own
row; processing re-checks the storage path belongs to that workspace and document.
"""

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.metrics import metrics
from app.models.document import Document, DocumentStatus
from app.models.usage import ACTIVE_JOB, IngestionJob, JobStatus
from app.services import usage
from app.services.embeddings import EmbedderProvider
from app.services.processing import (
    FAILED as DOCUMENT_FAILED,
)
from app.services.processing import (
    ProcessingResult,
    SessionFactory,
    StorageProvider,
    process_document,
)

logger = logging.getLogger(__name__)

QUEUED, RUNNING = JobStatus.QUEUED.value, JobStatus.RUNNING.value
# Shutdown: how long cancelled jobs may take to stop, and to hand them back.
CANCEL_WAIT_SECONDS = 5.0
RELEASE_TIMEOUT_SECONDS = 5.0
# How many candidates one claim looks at before giving up for this round.
CLAIM_CANDIDATES = 20
GAVE_UP = "Processing failed repeatedly. Retry the document to try again."


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


async def enqueue(session: AsyncSession, document_id: uuid.UUID, *, commit: bool = True) -> bool:
    """Queue a document, unless it already has an active job. Returns whether one was added."""
    settings = get_settings()
    workspace_id = await session.scalar(
        select(Document.workspace_id).where(Document.id == document_id)
    )
    if workspace_id is None:
        return False
    result = await session.execute(
        insert(IngestionJob)
        .values(
            document_id=document_id,
            workspace_id=workspace_id,
            status=QUEUED,
            max_attempts=settings.ingestion_max_attempts,
        )
        .on_conflict_do_nothing(index_elements=["document_id"], index_where=text(ACTIVE_JOB))
    )
    if commit:
        await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def claim_next(
    session: AsyncSession, worker: str, *, now: datetime | None = None
) -> IngestionJob | None:
    """Take the next job this worker may run, or None. Atomic across processes."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    candidates = (
        await session.execute(
            select(IngestionJob.id, IngestionJob.workspace_id)
            .where(IngestionJob.status == QUEUED, IngestionJob.available_at <= now)
            .order_by(IngestionJob.available_at, IngestionJob.created_at)
            .limit(CLAIM_CANDIDATES)
        )
    ).all()
    await session.rollback()
    full: set[uuid.UUID] = set()
    for job_id, workspace_id in candidates:
        if workspace_id in full:
            continue
        locked = await session.scalar(
            select(IngestionJob.id)
            .where(IngestionJob.id == job_id, IngestionJob.status == QUEUED)
            .with_for_update(skip_locked=True)
        )
        if locked is None:  # taken by another worker meanwhile
            await session.rollback()
            continue
        # Serialise claims per workspace, so two workers cannot both take its last slot.
        await usage.lock(session, "ingestion-run", workspace_id)
        running = await session.scalar(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.workspace_id == workspace_id, IngestionJob.status == RUNNING)
        )
        if (running or 0) >= settings.ingestion_workers_per_workspace:
            await session.rollback()
            full.add(workspace_id)
            continue
        job = await session.scalar(
            update(IngestionJob)
            .where(IngestionJob.id == job_id)
            .values(
                status=RUNNING,
                lease_owner=worker,
                leased_until=now + timedelta(seconds=settings.ingestion_job_lease_seconds),
                attempts=IngestionJob.attempts + 1,
                updated_at=now,
            )
            .returning(IngestionJob)
        )
        await session.commit()
        if job is not None:
            metrics.observe(
                "ingestion_queue_wait_seconds", max(0.0, (now - job.available_at).total_seconds())
            )
        return job
    return None


async def extend_lease(session: AsyncSession, job_id: uuid.UUID, worker: str) -> bool:
    """Keep a running job. False if this worker no longer holds it."""
    settings = get_settings()
    now = datetime.now(UTC)
    result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.status == RUNNING,
            IngestionJob.lease_owner == worker,
        )
        .values(
            leased_until=now + timedelta(seconds=settings.ingestion_job_lease_seconds),
            updated_at=now,
        )
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def fail_document(session: AsyncSession, document_id: uuid.UUID, message: str) -> None:
    from app.models.chunk import DocumentChunk

    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status.in_([DocumentStatus.UPLOADED.value, DocumentStatus.PROCESSING.value]),
        )
        .values(status=DOCUMENT_FAILED, error=message, updated_at=datetime.now(UTC))
    )


async def complete(
    session: AsyncSession, job: IngestionJob, worker: str, result: ProcessingResult
) -> str | None:
    """Record an attempt's result, if this worker still holds the job. Returns the job's new
    status, or None if the lease was lost (the result is then discarded)."""
    settings = get_settings()
    now = datetime.now(UTC)
    mine = (
        IngestionJob.id == job.id,
        IngestionJob.status == RUNNING,
        IngestionJob.lease_owner == worker,
    )
    if result == ProcessingResult.RETRY and job.attempts < job.max_attempts:
        status, values = (
            QUEUED,
            {
                "available_at": now
                + timedelta(seconds=settings.ingestion_retry_delay_seconds * job.attempts),
                "last_error": "retrying",
            },
        )
    elif result == ProcessingResult.RETRY:
        status, values = (
            JobStatus.FAILED.value,
            {"last_error": "attempts_exhausted", "finished_at": now},
        )
    elif result == ProcessingResult.FAILED:
        status, values = JobStatus.FAILED.value, {"last_error": "failed", "finished_at": now}
    elif result == ProcessingResult.GONE:
        status, values = (
            JobStatus.FAILED.value,
            {"last_error": "document_deleted", "finished_at": now},
        )
    else:  # READY, or SKIPPED (already processed or not waiting)
        status, values = (
            JobStatus.SUCCEEDED.value,
            {
                "last_error": None if result == ProcessingResult.READY else "nothing_to_do",
                "finished_at": now,
            },
        )
    updated = await session.execute(
        update(IngestionJob)
        .where(*mine)
        .values(status=status, lease_owner=None, leased_until=None, updated_at=now, **values)
    )
    if not updated.rowcount:  # type: ignore[attr-defined]
        await session.rollback()
        logger.warning("Job %s finished after its lease was lost; result discarded", job.id)
        return None
    if values.get("last_error") == "attempts_exhausted":
        await fail_document(session, job.document_id, GAVE_UP)
    await session.commit()
    metrics.inc(
        "ingestion_jobs_finished_total",
        status=status,
        reason=values.get("last_error") or "indexed",
    )
    return status


async def release_jobs(session: AsyncSession, worker: str, job_ids: list[uuid.UUID]) -> int:
    """Hand jobs this worker is giving up (because it is shutting down) back to the queue at
    once, without charging an attempt: the work was interrupted, not failed."""
    if not job_ids:
        return 0
    # Never wait long on a row another transaction holds: the lease is the fallback.
    await session.execute(text("SET LOCAL lock_timeout = '3s'"))
    now = datetime.now(UTC)
    released = (
        (
            await session.execute(
                update(IngestionJob)
                .where(
                    IngestionJob.id.in_(job_ids),
                    IngestionJob.status == RUNNING,
                    IngestionJob.lease_owner == worker,
                )
                .values(
                    status=QUEUED,
                    # The attempt number stays (each attempt's usage key is unique); the
                    # attempt is given back by raising the limit, so it costs nothing.
                    max_attempts=IngestionJob.max_attempts + 1,
                    lease_owner=None,
                    leased_until=None,
                    available_at=now,
                    last_error="worker_shutdown",
                    updated_at=now,
                )
                .returning(IngestionJob.document_id)
            )
        )
        .scalars()
        .all()
    )
    if released:
        # Ready for the next attempt, which removes any chunks this one left.
        await session.execute(
            update(Document)
            .where(Document.id.in_(released), Document.status == DocumentStatus.PROCESSING.value)
            .values(status=DocumentStatus.UPLOADED.value, updated_at=now)
        )
    await session.commit()
    metrics.inc("ingestion_jobs_handed_back_total", len(released))
    return len(released)


async def enqueue_missing(
    session: AsyncSession, *, older_than: timedelta = timedelta(minutes=1)
) -> int:
    """Give a job to every document waiting without one (for example from before jobs
    existed, or if its job could not be written). Idempotent."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - older_than
    active = (
        select(IngestionJob.id)
        .where(IngestionJob.document_id == Document.id, IngestionJob.status.in_([QUEUED, RUNNING]))
        .exists()
    )
    waiting = select(
        Document.id, Document.workspace_id, func.cast(settings.ingestion_max_attempts, Integer)
    ).where(Document.status == DocumentStatus.UPLOADED.value, Document.updated_at < cutoff, ~active)
    result = await session.execute(
        insert(IngestionJob)
        .from_select(["document_id", "workspace_id", "max_attempts"], waiting)
        .on_conflict_do_nothing(index_elements=["document_id"], index_where=text(ACTIVE_JOB))
    )
    await session.commit()
    added = result.rowcount or 0  # type: ignore[attr-defined]
    if added:
        logger.warning("Queued %d waiting document(s) that had no ingestion job", added)
    return added


async def recover_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Take back running jobs whose lease expired: their worker stopped. Requeued while
    attempts remain, failed (with the document) otherwise."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    stale = (
        (
            await session.execute(
                select(IngestionJob)
                .where(IngestionJob.status == RUNNING, IngestionJob.leased_until < now)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for job in stale:
        exhausted = job.attempts >= job.max_attempts
        job.status = JobStatus.FAILED.value if exhausted else QUEUED
        job.lease_owner = None
        job.leased_until = None
        job.last_error = "lease_expired"
        job.updated_at = now
        if exhausted:
            job.finished_at = now
            await fail_document(session, job.document_id, GAVE_UP)
        else:
            job.available_at = now + timedelta(seconds=settings.ingestion_retry_delay_seconds)
            # Ready for the next attempt, which removes any chunks this one left.
            await session.execute(
                update(Document)
                .where(
                    Document.id == job.document_id,
                    Document.status == DocumentStatus.PROCESSING.value,
                )
                .values(status=DocumentStatus.UPLOADED.value, updated_at=now)
            )
    await session.commit()
    if stale:
        logger.warning("Recovered %d ingestion job(s) whose worker stopped", len(stale))
        metrics.inc("ingestion_jobs_recovered_total", len(stale))
    return len(stale)


async def run_job(
    job: IngestionJob,
    worker: str,
    session_factory: SessionFactory,
    storage_provider: StorageProvider,
    embedder_provider: EmbedderProvider,
) -> str | None:
    """Run one claimed job to its result."""

    async def keep_lease() -> None:
        async with session_factory() as lease_session:
            await extend_lease(lease_session, job.id, worker)

    started = time.perf_counter()
    result = await process_document(
        job.document_id,
        session_factory,
        storage_provider,
        embedder_provider,
        allow_retry=job.attempts < job.max_attempts,
        attempt_key=f"document_embedding:{job.id}:{job.attempts}",
        on_progress=keep_lease,
    )
    metrics.observe(
        "ingestion_job_duration_seconds", time.perf_counter() - started, result=result.value
    )
    async with session_factory() as session:
        return await complete(session, job, worker, result)


class JobQueue:
    """The durable queue seen by request handlers (`enqueue`), and this process's workers."""

    def __init__(
        self,
        session_factory: SessionFactory,
        storage_provider: StorageProvider,
        embedder_provider: EmbedderProvider,
        concurrency: int,
    ) -> None:
        self._session_factory = session_factory
        self._storage_provider = storage_provider
        self._embedder_provider = embedder_provider
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self._wake = asyncio.Event()
        self._worker = worker_name()
        self._tasks: set[asyncio.Task[None]] = set()
        # Jobs this process is running, by task, so a shutdown can hand them back.
        self._running: dict[asyncio.Task[None], uuid.UUID] = {}
        self._stopping = False
        self._loop: asyncio.Task[None] | None = None

    def enqueue(self, document_id: uuid.UUID) -> None:
        """Make sure the document has a job, and wake the workers. The job itself is normally
        created with the document; this covers documents from before jobs existed."""

        async def add() -> None:
            try:
                async with self._session_factory() as session:
                    await enqueue(session, document_id)
            except SQLAlchemyError:
                logger.exception("Could not queue document %s", document_id)
            self._wake.set()

        task = asyncio.create_task(add(), name=f"enqueue-{document_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def maintain(self) -> None:
        async with self._session_factory() as session:
            await recover_expired(session)
            await usage.sweep_expired(session)
            await enqueue_missing(session)

    async def run_available(self) -> int:
        """Start as many available jobs as there are free slots. Returns how many started."""
        started = 0
        while not self._slots.locked() and not self._stopping:
            async with self._session_factory() as session:
                job = await claim_next(session, self._worker)
            if job is None:
                break
            await self._slots.acquire()
            started += 1
            task = asyncio.create_task(self._run(job), name=f"ingest-{job.document_id}")
            self._tasks.add(task)
            self._running[task] = job.id
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(lambda done: self._running.pop(done, None))
        return started

    async def _run(self, job: IngestionJob) -> None:
        try:
            await run_job(
                job,
                self._worker,
                self._session_factory,
                self._storage_provider,
                self._embedder_provider,
            )
        except Exception:
            # The lease will expire and the job be recovered.
            logger.exception("Ingestion job %s crashed", job.id)
        finally:
            self._slots.release()
            self._wake.set()

    async def _forever(self) -> None:
        settings = get_settings()
        last_maintenance = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                if loop.time() - last_maintenance >= settings.usage_sweep_interval_seconds:
                    await self.maintain()
                    last_maintenance = loop.time()
                await self.run_available()
            except Exception:
                logger.exception("Ingestion worker loop failed; continuing")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), settings.ingestion_poll_seconds)
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._loop is None:
            self._loop = asyncio.create_task(self._forever(), name="ingestion-worker")

    async def stop(self, grace_seconds: float | None = None) -> None:
        """Stop gracefully: claim nothing more, let running jobs finish for up to the grace
        period, then cancel the rest and hand their jobs back to the queue at once, without
        charging an attempt, so another process picks them up straight away rather than
        after their lease expires."""
        self._stopping = True
        if self._loop is not None:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
            self._loop = None
        grace = (
            get_settings().ingestion_shutdown_grace_seconds
            if grace_seconds is None
            else grace_seconds
        )
        running = dict(self._running)
        if not running:
            return
        _done, pending = await asyncio.wait(list(running), timeout=grace)
        if not pending:
            return
        for task in pending:
            task.cancel()
        # Bounded: a task that does not finish cancelling (stuck in a network call, say)
        # must not hold the process up.
        stopped, still_running = await asyncio.wait(pending, timeout=CANCEL_WAIT_SECONDS)
        if still_running:
            # Not handed back: its task may still write, so another worker must not start
            # the same document meanwhile. Its lease expires and it is recovered then.
            logger.warning(
                "Shutdown: %d ingestion job(s) did not stop; they will be recovered when "
                "their lease expires",
                len(still_running),
            )
        if not stopped:
            return
        try:
            async with self._session_factory() as session:
                released = await asyncio.wait_for(
                    release_jobs(session, self._worker, [running[task] for task in stopped]),
                    RELEASE_TIMEOUT_SECONDS,
                )
            logger.info(
                "Shutdown: handed %d unfinished ingestion job(s) back to the queue", released
            )
        except Exception:
            # Their leases will expire and they will be recovered.
            logger.exception("Shutdown: could not hand unfinished jobs back")

    async def drain(self) -> None:
        """Wait for work started so far. Used in tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
