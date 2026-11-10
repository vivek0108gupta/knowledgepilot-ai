"""The document processing pipeline: uploaded -> processing -> ready, or -> failed.

Request handlers only call `queue.enqueue(document_id)`. Everything else happens here, away
from the request, and extraction itself lives in `extraction.py` with no knowledge of
databases or storage.

Limitations of running in-process (see the README): work in progress is lost if the API
process stops, and there is no retry schedule. `ProcessingQueue` is the seam for a durable
queue: a worker in another process would call `process_document` exactly as this does.
"""

import asyncio
import logging
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.document import Document, DocumentSegment, DocumentStatus
from app.services.extraction import Extraction, ExtractionError, extract
from app.services.file_validation import KIND_BY_MIME
from app.services.storage import Storage, get_storage

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]
StorageProvider = Callable[[], Storage]

# A document still `processing` after this long was interrupted (for example by a restart).
STALE_AFTER = timedelta(minutes=10)
INSERT_BATCH = 500

UPLOADED = DocumentStatus.UPLOADED.value
PROCESSING = DocumentStatus.PROCESSING.value
READY = DocumentStatus.READY.value
FAILED = DocumentStatus.FAILED.value


class ProcessingQueue(Protocol):
    """Accepts documents for processing. The only thing request handlers know about."""

    def enqueue(self, document_id: uuid.UUID) -> None: ...


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


async def mark_failed(session: AsyncSession, document_id: uuid.UUID, message: str) -> None:
    await session.rollback()
    await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.status == PROCESSING)
        .values(status=FAILED, error=message, updated_at=datetime.now(UTC))
    )
    await session.commit()


async def save_extraction(
    session: AsyncSession, document_id: uuid.UUID, extraction: Extraction
) -> bool:
    """Store the segments and mark the document ready, all in one transaction.

    Returns False, saving nothing, if the document was deleted while it was being processed.
    """
    # A retried document may have segments from an earlier, partial attempt.
    await session.execute(delete(DocumentSegment).where(DocumentSegment.document_id == document_id))

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

    for start in range(0, len(rows), INSERT_BATCH):
        await session.execute(insert(DocumentSegment), rows[start : start + INSERT_BATCH])
    await session.commit()
    return True


async def process_document(
    document_id: uuid.UUID, session_factory: SessionFactory, storage_provider: StorageProvider
) -> None:
    """Extract one document's text. Safe to call twice: the second call finds nothing to claim.

    Never raises for a problem with the document: every failure is recorded on the document
    with a message its uploader can act on.
    """
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

            if await save_extraction(session, document.id, extraction):
                logger.info(
                    "Processed document %s: %d segments, %d characters",
                    document.id,
                    len(extraction.segments),
                    extraction.char_count,
                )
        except ExtractionError as error:
            logger.info("Document %s could not be extracted", document_id)
            await mark_failed(session, document_id, str(error))
        except AppError as error:
            logger.error("Document %s failed on storage: %s", document_id, error.code)
            await mark_failed(
                session, document_id, "The file could not be read from storage. Try again."
            )
        except Exception:
            # The traceback goes to the logs; the user gets a message with no internals.
            logger.exception("Document %s failed unexpectedly", document_id)
            await mark_failed(
                session, document_id, "Something went wrong while processing this document."
            )


class InProcessQueue:
    """Runs processing as asyncio tasks inside the API process, a few at a time."""

    def __init__(
        self, session_factory: SessionFactory, storage_provider: StorageProvider, concurrency: int
    ) -> None:
        self._session_factory = session_factory
        self._storage_provider = storage_provider
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
                await process_document(document_id, self._session_factory, self._storage_provider)
            except Exception:
                logger.exception("Processing task for document %s crashed", document_id)

    async def drain(self) -> None:
        """Wait for everything queued so far. Used at shutdown and in tests."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def recover_interrupted(session_factory: SessionFactory, queue: ProcessingQueue) -> int:
    """Re-queue documents left waiting or half-done, for example after a restart.

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

    return InProcessQueue(SessionLocal, get_storage, get_settings().processing_concurrency)
