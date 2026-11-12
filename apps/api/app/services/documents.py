"""Document rules: who may do what, and keeping the database and storage in step.

As with workspaces, every function takes the acting user's ID from a verified token and
checks membership and role itself.

    upload, view, download   any member of the workspace
    delete, retry            the member who uploaded it, or an admin or owner
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.document import Document, DocumentStatus
from app.models.workspace import WorkspaceMember, WorkspaceRole
from app.services import usage
from app.services import workspaces as workspace_service
from app.services.file_validation import ValidatedFile
from app.services.processing import STALE_AFTER, ProcessingQueue, owns_storage_path
from app.services.storage import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentAccess:
    """A document together with the acting user's proven role in its workspace."""

    document: Document
    role: WorkspaceRole
    user_id: uuid.UUID

    @property
    def can_manage(self) -> bool:
        """May delete or retry: admins and owners, and whoever uploaded the document."""
        return (
            self.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN)
            or self.document.uploaded_by == self.user_id
        )


def not_found() -> AppError:
    # The same answer whether the document does not exist or belongs to a workspace the
    # caller is not in, so document IDs cannot be probed.
    return AppError(404, "document_not_found", "Document not found.")


def forbidden() -> AppError:
    return AppError(403, "forbidden", "You do not have permission to do this.")


def duplicate(existing_name: str) -> AppError:
    return AppError(
        409, "duplicate_document", f"This file is already in the workspace as “{existing_name}”."
    )


async def get_access(
    session: AsyncSession, document_id: uuid.UUID, user_id: uuid.UUID
) -> DocumentAccess:
    """Load a document only if the user is a member of the workspace it belongs to."""
    row = (
        await session.execute(
            select(Document, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Document.workspace_id)
            .where(Document.id == document_id, WorkspaceMember.user_id == user_id)
        )
    ).one_or_none()
    if row is None:
        raise not_found()
    return DocumentAccess(document=row[0], role=WorkspaceRole(row[1]), user_id=user_id)


def storage_path_of(document: Document) -> str:
    """The document's storage path, after confirming it lies inside the document's own folder.

    The database constrains this too. Checking again means a bad row can never make the API,
    which holds the storage key, read or delete another tenant's file.
    """
    if not owns_storage_path(document.workspace_id, document.id, document.storage_path):
        logger.error("Document %s has a storage path outside its own folder", document.id)
        raise AppError(500, "internal_error", "Something went wrong on our side.")
    return document.storage_path


def ingestion_busy(limit: int) -> AppError:
    return AppError(
        429,
        "ingestion_busy",
        f"This workspace already has {limit} documents waiting to be processed. Upload more "
        "once some are ready.",
        retry_after_seconds=30,
    )


async def ensure_ingestion_capacity(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Refuse more work while the workspace has its maximum number of documents waiting or
    being processed. Takes a workspace lock held until the caller's transaction ends, so two
    uploads cannot both take the last place."""
    limit = get_settings().max_ingestion_jobs_per_workspace
    await usage.lock(session, "ingestion", workspace_id)
    waiting = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.workspace_id == workspace_id,
            Document.status.in_([DocumentStatus.UPLOADED.value, DocumentStatus.PROCESSING.value]),
        )
    )
    if (waiting or 0) >= limit:
        await session.rollback()
        raise ingestion_busy(limit)


async def create(
    session: AsyncSession,
    storage: Storage,
    queue: ProcessingQueue,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    validated: ValidatedFile,
    file: BinaryIO,
) -> Document:
    """Store a validated file and record it, so that neither exists without the other."""
    await workspace_service.get_access(session, workspace_id, user_id)

    existing_name = await session.scalar(
        select(Document.filename).where(
            Document.workspace_id == workspace_id, Document.sha256 == validated.sha256
        )
    )
    if existing_name is not None:
        raise duplicate(existing_name)
    await ensure_ingestion_capacity(session, workspace_id)

    document_id = uuid.uuid4()
    # Built only from server-generated IDs and a sanitised ASCII name: nothing in this path
    # comes straight from the request.
    storage_path = f"{workspace_id}/{document_id}/{validated.storage_name}"
    document = Document(
        id=document_id,
        workspace_id=workspace_id,
        uploaded_by=user_id,
        filename=validated.filename,
        storage_path=storage_path,
        mime_type=validated.mime_type,
        size_bytes=validated.size_bytes,
        sha256=validated.sha256,
        status=DocumentStatus.UPLOADED.value,
    )
    session.add(document)
    try:
        # Reserve the row first, so a duplicate racing with this one loses before any upload.
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        raise duplicate(validated.filename) from error

    try:
        await storage.upload(storage_path, file, validated.mime_type, validated.size_bytes)
    except BaseException:
        # Nothing was stored (or it failed part-way): leave no database record behind.
        await session.rollback()
        raise

    try:
        await session.commit()
    except BaseException:
        # The file is stored but its record is not: remove the orphaned file.
        await session.rollback()
        await delete_stored_files(storage, [storage_path])
        raise

    await session.refresh(document)
    logger.info(
        "Stored document %s in workspace %s: %s, %d bytes",
        document.id,
        workspace_id,
        validated.kind.value,
        validated.size_bytes,
    )
    queue.enqueue(document.id)
    return document


async def delete_stored_files(storage: Storage, paths: list[str]) -> bool:
    """Best-effort removal of files whose records are gone. Never raises.

    Returns False if the files could not be removed, after logging how many were left behind.
    """
    if not paths:
        return True
    try:
        await storage.delete(paths)
    except Exception as error:
        logger.error(
            "Could not remove %d orphaned file(s) from storage: %s",
            len(paths),
            type(error).__name__,
        )
        return False
    return True


async def list_for_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    status: DocumentStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[Document], int]:
    await workspace_service.get_access(session, workspace_id, user_id)

    conditions = [Document.workspace_id == workspace_id]
    if status is not None:
        conditions.append(Document.status == status.value)

    total = await session.scalar(select(func.count()).select_from(Document).where(*conditions))
    documents = await session.scalars(
        select(Document)
        .where(*conditions)
        # The ID breaks ties, so pages never repeat or skip a document.
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(documents), total or 0


async def signed_download_url(
    session: AsyncSession,
    storage: Storage,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    expires_in: int,
) -> tuple[Document, str]:
    access = await get_access(session, document_id, user_id)
    document = access.document
    url = await storage.create_signed_url(storage_path_of(document), expires_in, document.filename)
    logger.info("Issued a %ds download link for document %s", expires_in, document.id)
    return document, url


async def remove(
    session: AsyncSession, storage: Storage, document_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    access = await get_access(session, document_id, user_id)
    if not access.can_manage:
        raise forbidden()
    document = access.document

    # File first. If storage refuses, the record stays and the user can try again; if the
    # record then fails to delete, a second attempt still works because a missing file is
    # not an error. Either way no file is left without a record pointing at it.
    await storage.delete([storage_path_of(document)])
    await session.execute(delete(Document).where(Document.id == document.id))
    await session.commit()
    logger.info("Deleted document %s", document.id)


async def retry(
    session: AsyncSession, queue: ProcessingQueue, document_id: uuid.UUID, user_id: uuid.UUID
) -> Document:
    """Queue a failed document again, or one whose processing was interrupted."""
    access = await get_access(session, document_id, user_id)
    if not access.can_manage:
        raise forbidden()
    document = access.document

    interrupted = (
        document.status == DocumentStatus.PROCESSING.value
        and document.updated_at < datetime.now(UTC) - STALE_AFTER
    )
    if document.status != DocumentStatus.FAILED.value and not interrupted:
        raise AppError(
            409, "not_retryable", "Only a document that failed to process can be retried."
        )
    if not interrupted:
        await ensure_ingestion_capacity(session, document.workspace_id)

    document.status = DocumentStatus.UPLOADED.value
    document.error = None
    await session.commit()
    await session.refresh(document)
    queue.enqueue(document.id)
    return document
