"""The document processing pipeline: uploaded -> processing -> ready, or -> failed.

Request handlers only call `queue.enqueue(document_id)`. Everything else happens here, away
from the request:

    claim -> download -> extract -> chunk -> embed -> save chunks and vectors -> ready

Each stage lives in its own module and knows nothing of the others: `extraction.py` reads
files, `chunking.py` splits text, `embeddings.py` calls OpenAI. This module is the only
place that joins them to the database and to storage.

A document becomes `ready` only after every chunk has its vector saved. Chunks are written
in batches while the document is still `processing`, where search cannot see them, and are
removed again if anything fails.

Usage: each document's embeddings are one usage record. Before the first embedding call, the
document's whole token count (plus chunk overlap) is reserved against the uploader's and the
workspace's quotas; a document over a limit fails with a message saying so, and nothing is
sent to the provider. The provider's reported token counts are recorded as batches finish.

Limitations of running in-process (see the README): this is not a durable queue. Work in
progress is lost if the API process stops, and there is no retry schedule.
`ProcessingQueue` is the seam for one: a worker in another process would call
`process_document` exactly as this does.
"""

import asyncio
import itertools
import logging
import math
import tempfile
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.chunk import DocumentChunk
from app.models.document import Document, DocumentSegment, DocumentStatus
from app.models.usage import UsageOperation, UsageSource, UsageStatus
from app.services import usage
from app.services.chunking import Chunk, chunk_segments
from app.services.embeddings import EmbedderProvider, EmbeddingError, embed_metered, get_embedder
from app.services.extraction import Extraction, ExtractionError, extract
from app.services.file_validation import KIND_BY_MIME
from app.services.storage import Storage, get_storage

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]
StorageProvider = Callable[[], Storage]

# A document still `processing` and untouched for this long was interrupted (for example by
# a restart). Processing refreshes the document's timestamp after every batch, so a large
# document that is still being worked on is never mistaken for a stuck one.
STALE_AFTER = timedelta(minutes=10)
INSERT_BATCH = 500
# How many chunks are embedded and written per step. Bounds memory for large documents.
CHUNK_BATCH = 64

# Recorded on a document that could not be indexed because no embedding key was set.
# Such documents are retried automatically once a key is configured.
NOT_CONFIGURED_MESSAGE = "Search indexing is not set up on the server yet."

UPLOADED = DocumentStatus.UPLOADED.value
PROCESSING = DocumentStatus.PROCESSING.value
READY = DocumentStatus.READY.value
FAILED = DocumentStatus.FAILED.value


class ProcessingQueue(Protocol):
    """Accepts documents for processing. The only thing request handlers know about."""

    def enqueue(self, document_id: uuid.UUID) -> None: ...


class DocumentGone(Exception):
    """The document was deleted while it was being processed. Nothing more to do."""


def owns_storage_path(workspace_id: uuid.UUID, document_id: uuid.UUID, path: str) -> bool:
    """True if `path` is inside this document's own folder, with no way to climb out of it."""
    prefix = f"{workspace_id}/{document_id}/"
    name = path[len(prefix) :]
    return path.startswith(prefix) and bool(name) and "/" not in name and name not in {".", ".."}


async def claim(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    """Move a document from `uploaded` to `processing`, or return None if it is not waiting.

    One atomic UPDATE, so if two workers pick up the same document only one proceeds.
    """
    document = await session.scalar(
        update(Document)
        .where(Document.id == document_id, Document.status == UPLOADED)
        .values(status=PROCESSING, error=None, updated_at=datetime.now(UTC))
        .returning(Document)
    )
    await session.commit()
    return document


async def remove_chunks(session: AsyncSession, document_id: uuid.UUID) -> None:
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    await session.commit()


async def mark_failed(session: AsyncSession, document_id: uuid.UUID, message: str) -> None:
    """Record the failure and remove any chunks the failed attempt had already written."""
    await session.rollback()
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status == PROCESSING)
        .values(status=FAILED, error=message, updated_at=datetime.now(UTC))
    )
    await session.commit()


def chunk_row(document: Document, chunk: Chunk, vector: list[float]) -> dict[str, object]:
    return {
        "document_id": document.id,
        # Taken from the document row itself, never from anything a request supplied.
        "workspace_id": document.workspace_id,
        "chunk_index": chunk.index,
        "content": chunk.content,
        "token_count": chunk.token_count,
        "page_number": chunk.page_number,
        "paragraph_index": chunk.paragraph_index,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "embedding": vector,
        "chunk_metadata": chunk.metadata,
    }


class EmbeddingMeter:
    """Tokens embedded so far for one document: as reported by the provider, and as counted
    here for any batch it did not report on."""

    def __init__(self) -> None:
        self.reported = 0
        self.estimated = 0
        self.in_flight = 0

    def outcome(self, status: UsageStatus, error_code: str | None = None) -> usage.Outcome:
        if status == UsageStatus.COMPLETED and self.estimated == 0:
            return usage.Outcome(status, UsageSource.PROVIDER, self.reported, 0)
        # A batch the provider did not report on, or one that was sent when the work
        # stopped, is counted at its own token count: it may have been billed.
        estimated = self.reported + self.estimated + self.in_flight
        source = UsageSource.ESTIMATED if estimated else UsageSource.NONE
        return usage.Outcome(status, source, estimated, 0, error_code=error_code)


def ingestion_estimate(extraction: Extraction, chunk_tokens: int, overlap_tokens: int) -> int:
    """The tokens a document's chunks will hold: its text, plus the overlap repeated between
    neighbouring chunks, plus a little for chunk boundaries."""
    text_tokens = sum(usage.count_tokens(segment.content) for segment in extraction.segments)
    stride = max(1, chunk_tokens - overlap_tokens)
    return math.ceil(text_tokens * chunk_tokens / stride) + 16


async def embed_and_store(
    session: AsyncSession,
    document: Document,
    chunks: Iterator[Chunk],
    embedder_provider: EmbedderProvider,
    meter: EmbeddingMeter | None = None,
) -> int:
    """Embed the chunks a batch at a time and write each batch as it completes.

    Returns how many chunks were written. Each batch is its own short transaction, so no
    database transaction is held open across a call to the embedding API, and only one
    batch of text and vectors is ever in memory.
    """
    embedder = embedder_provider()
    written = 0
    while True:
        # Tokenising is CPU-bound, so the next batch is cut off the event loop.
        batch = await asyncio.to_thread(lambda: list(itertools.islice(chunks, CHUNK_BATCH)))
        if not batch:
            return written

        batch_tokens = sum(chunk.token_count for chunk in batch)
        if meter is not None:
            meter.in_flight = batch_tokens
        embedded = await embed_metered(embedder, [chunk.content for chunk in batch])
        if meter is not None:
            meter.in_flight = 0
            if embedded.input_tokens is not None:
                meter.reported += embedded.input_tokens
            else:
                meter.estimated += batch_tokens
        vectors = embedded.vectors
        if len(vectors) != len(batch):
            raise EmbeddingError(
                "The embedding service returned an invalid response.", retryable=False
            )

        await session.execute(
            insert(DocumentChunk),
            [
                chunk_row(document, chunk, vector)
                for chunk, vector in zip(batch, vectors, strict=True)
            ],
        )
        # Also a heartbeat: shows the document is still being worked on, and detects a
        # document deleted mid-way before more embedding calls are spent on it.
        alive = await session.execute(
            update(Document)
            .where(Document.id == document.id, Document.status == PROCESSING)
            .values(updated_at=datetime.now(UTC))
        )
        if alive.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            raise DocumentGone
        await session.commit()
        written += len(batch)


async def finish(
    session: AsyncSession, document_id: uuid.UUID, extraction: Extraction, chunk_count: int
) -> bool:
    """Store the extracted segments and mark the document ready, in one transaction.

    Refuses, leaving the document unready, unless exactly `chunk_count` chunks with vectors
    are in the database. Returns False if the document was deleted meanwhile.
    """
    stored = await session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
    )
    if stored != chunk_count or chunk_count == 0:
        await session.rollback()
        raise RuntimeError(f"expected {chunk_count} chunks, found {stored}")

    # A retried document may have segments from an earlier attempt.
    await session.execute(delete(DocumentSegment).where(DocumentSegment.document_id == document_id))

    marked = await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status == PROCESSING)
        .values(
            status=READY,
            page_count=extraction.page_count,
            char_count=extraction.char_count,
            error=None,
            updated_at=datetime.now(UTC),
        )
    )
    if marked.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False

    rows = [
        {
            "document_id": document_id,
            "position": position,
            "kind": segment.kind.value,
            "page_number": segment.page_number,
            "paragraph_index": segment.paragraph_index,
            "line_start": segment.line_start,
            "line_end": segment.line_end,
            "section": segment.section,
            "content": segment.content,
        }
        for position, segment in enumerate(extraction.segments)
    ]
    for start in range(0, len(rows), INSERT_BATCH):
        await session.execute(insert(DocumentSegment), rows[start : start + INSERT_BATCH])
    await session.commit()
    return True


QUOTA_MESSAGES = {
    "user_quota_exceeded": "The uploader has reached their daily AI usage limit. Retry this "
    "document after midnight UTC.",
    "workspace_quota_exceeded": "This workspace has reached its monthly AI usage limit, so the "
    "document was not indexed. Retry it once the limit allows.",
    "workspace_busy": "This workspace was running too many AI operations at once. Retry this "
    "document.",
    "usage_unavailable": "Usage limits could not be checked, so the document was not indexed. "
    "Try again.",
}


def failure_message(error: AppError) -> str:
    if error.code in QUOTA_MESSAGES:
        return QUOTA_MESSAGES[error.code]
    if error.code.startswith("embeddings_"):
        return NOT_CONFIGURED_MESSAGE
    return "The file could not be read from storage. Try again."


async def process_document(
    document_id: uuid.UUID,
    session_factory: SessionFactory,
    storage_provider: StorageProvider,
    embedder_provider: EmbedderProvider,
) -> None:
    """Extract, chunk and embed one document. Safe to call twice: the second call finds
    nothing to claim.

    Never raises for a problem with the document: every failure is recorded on the document
    with a message its uploader can act on, and leaves no chunks behind.
    """
    settings = get_settings()
    async with session_factory() as session:
        document = await claim(session, document_id)
        if document is None:
            return

        try:
            # Checked again here, independently of the request that queued it: this code
            # holds the storage key, so it must never be steered to another tenant's file.
            if not owns_storage_path(document.workspace_id, document.id, document.storage_path):
                logger.error("Document %s has a storage path outside its own folder", document.id)
                raise ExtractionError("This document could not be processed.")

            kind = KIND_BY_MIME.get(document.mime_type)
            if kind is None:
                raise ExtractionError("This file type is not supported.")

            with tempfile.NamedTemporaryFile(prefix="kp-", suffix=f".{kind.value}") as temporary:
                await storage_provider().download_to(document.storage_path, temporary)
                temporary.flush()
                # Extraction is CPU-bound and blocking, so it runs off the event loop.
                extraction = await asyncio.to_thread(extract, kind, Path(temporary.name))

            # Chunks left by an attempt that was cut short (for example by a restart).
            await remove_chunks(session, document.id)

            chunks = chunk_segments(
                extraction.segments,
                chunk_tokens=settings.chunk_size_tokens,
                overlap_tokens=settings.chunk_overlap_tokens,
            )
            if not extraction.segments:
                raise ExtractionError("No text could be found in this document.")
            reservation = await usage.reserve(
                session,
                user_id=document.uploaded_by,
                workspace_id=document.workspace_id,
                operation=UsageOperation.DOCUMENT_EMBEDDING,
                model=settings.embedding_model,
                input_tokens=await asyncio.to_thread(
                    ingestion_estimate,
                    extraction,
                    settings.chunk_size_tokens,
                    settings.chunk_overlap_tokens,
                ),
                output_tokens=0,
                # One reservation per processing attempt: claiming stamps updated_at.
                idempotency_key=f"document_embedding:{document.id}:{document.updated_at.isoformat()}",
                ttl_seconds=settings.ingestion_reservation_ttl_seconds,
                document_id=document.id,
            )
            meter = EmbeddingMeter()
            try:
                chunk_count = await embed_and_store(
                    session, document, chunks, embedder_provider, meter
                )
            except DocumentGone:
                await usage.finalize(
                    session,
                    reservation.id,
                    meter.outcome(UsageStatus.CANCELLED, "document_deleted"),
                )
                raise
            except BaseException as error:
                code = "embeddings_unavailable" if isinstance(error, EmbeddingError) else "failed"
                await session.rollback()
                await usage.finalize(
                    session, reservation.id, meter.outcome(UsageStatus.FAILED, code)
                )
                raise
            await usage.finalize(session, reservation.id, meter.outcome(UsageStatus.COMPLETED))
            if chunk_count == 0:
                raise ExtractionError("No text could be found in this document.")

            if await finish(session, document.id, extraction, chunk_count):
                logger.info(
                    "Processed document %s: %d segments, %d chunks, %d characters",
                    document.id,
                    len(extraction.segments),
                    chunk_count,
                    extraction.char_count,
                )
            else:
                await remove_chunks(session, document.id)
        except DocumentGone:
            # Deleted while processing: its chunks went with it through the foreign key.
            logger.info("Document %s was deleted while it was being processed", document_id)
        except ExtractionError as error:
            logger.info("Document %s could not be extracted or chunked", document_id)
            await mark_failed(session, document_id, str(error))
        except EmbeddingError as error:
            logger.error(
                "Document %s failed on embeddings (retryable: %s)", document_id, error.retryable
            )
            await mark_failed(
                session,
                document_id,
                "The document's text could not be indexed for search. Try again."
                if error.retryable
                else "The document's text could not be indexed for search.",
            )
        except AppError as error:
            logger.error("Document %s failed: %s", document_id, error.code)
            await mark_failed(session, document_id, failure_message(error))
        except Exception:
            # The traceback goes to the logs; the user gets a message with no internals.
            logger.exception("Document %s failed unexpectedly", document_id)
            await mark_failed(
                session, document_id, "Something went wrong while processing this document."
            )


class InProcessQueue:
    """Runs processing as asyncio tasks inside the API process, a few at a time."""

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
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        # Keep a reference to each task, or the event loop may drop it mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()

    def enqueue(self, document_id: uuid.UUID) -> None:
        task = asyncio.create_task(self._run(document_id), name=f"process-document-{document_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, document_id: uuid.UUID) -> None:
        async with self._semaphore:
            try:
                await process_document(
                    document_id,
                    self._session_factory,
                    self._storage_provider,
                    self._embedder_provider,
                )
            except Exception:
                logger.exception("Processing task for document %s crashed", document_id)

    async def drain(self) -> None:
        """Wait for everything queued so far. Used at shutdown and in tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def recover_interrupted(
    session_factory: SessionFactory, queue: ProcessingQueue, *, reindex_unchunked: bool = False
) -> int:
    """Re-queue documents left waiting or half-done, for example after a restart.

    With `reindex_unchunked` (set when an embedding key is configured), two more kinds of
    document are queued again so they become searchable: those that are `ready` but have no
    chunks (processed before semantic search existed), and those that failed only because
    no embedding key was set at the time.
    Returns how many were queued.
    """
    async with session_factory() as session:
        await session.execute(
            update(Document)
            .where(
                Document.status == PROCESSING,
                Document.updated_at < datetime.now(UTC) - STALE_AFTER,
            )
            .values(status=UPLOADED, updated_at=datetime.now(UTC))
        )
        if reindex_unchunked:
            has_chunks = exists().where(DocumentChunk.document_id == Document.id)
            await session.execute(
                update(Document)
                .where(Document.status == READY, ~has_chunks)
                .values(status=UPLOADED, updated_at=datetime.now(UTC))
            )
            await session.execute(
                update(Document)
                .where(Document.status == FAILED, Document.error == NOT_CONFIGURED_MESSAGE)
                .values(status=UPLOADED, error=None, updated_at=datetime.now(UTC))
            )
        await session.commit()
        waiting = list(
            await session.scalars(select(Document.id).where(Document.status == UPLOADED))
        )

    for document_id in waiting:
        queue.enqueue(document_id)
    return len(waiting)


@lru_cache
def get_processing_queue() -> ProcessingQueue:
    from app.core.db import SessionLocal

    return InProcessQueue(
        SessionLocal, get_storage, get_embedder, get_settings().processing_concurrency
    )
