import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import CurrentUser, DbSession
from app.api.uploads import single_file_upload
from app.core.config import get_settings
from app.models.document import MAX_DOCUMENT_BYTES, DocumentStatus
from app.schemas.document import DocumentOut, DocumentPage, DocumentStatusOut, DownloadOut
from app.services import documents as service
from app.services import rate_limits
from app.services import workspaces as workspace_service
from app.services.file_validation import validate_upload
from app.services.processing import ProcessingQueue, StorageProvider, get_processing_queue
from app.services.storage import get_storage

router = APIRouter(tags=["documents"])


def get_storage_provider() -> StorageProvider:
    """How routes reach storage. Returned as a callable so storage is only required, and
    only fails for being unconfigured, on the requests that actually use it."""
    return get_storage


StorageDep = Annotated[StorageProvider, Depends(get_storage_provider)]
QueueDep = Annotated[ProcessingQueue, Depends(get_processing_queue)]

UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }
            }
        },
    }
}


@router.post(
    "/workspaces/{workspace_id}/documents",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=UPLOAD_BODY,
)
async def upload_document(
    workspace_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    storage_provider: StorageDep,
    queue: QueueDep,
) -> DocumentOut:
    """Upload a PDF, DOCX or TXT file of up to 25 MB. Any member of the workspace may upload.

    The body is read only after the caller is known to be a member, so an outsider cannot
    make the server receive a large file.
    """
    await workspace_service.get_access(session, workspace_id, user.id)
    await session.rollback()
    # Counted before the body is read, so a refused upload costs no bandwidth.
    await rate_limits.hit(session, rate_limits.upload_per_user(get_settings()), user.id)
    # Release the database connection while the file is received.
    await session.rollback()
    storage = storage_provider()

    async with single_file_upload(request, MAX_DOCUMENT_BYTES) as upload:
        # Hashing and inspecting the file is blocking work, so it runs off the event loop.
        validated = await asyncio.to_thread(
            validate_upload, upload.filename, upload.content_type, upload.file
        )
        document = await service.create(
            session, storage, queue, workspace_id, user.id, validated, upload.file
        )
    return DocumentOut.model_validate(document)


@router.get("/workspaces/{workspace_id}/documents")
async def list_documents(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    status_filter: Annotated[
        DocumentStatus | None, Query(alias="status", description="Only this status.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentPage:
    """A workspace's documents, newest first."""
    documents, total = await service.list_for_workspace(
        session, workspace_id, user.id, status_filter, limit, offset
    )
    return DocumentPage(
        items=[DocumentOut.model_validate(document) for document in documents],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> DocumentOut:
    access = await service.get_access(session, document_id, user.id)
    return DocumentOut.model_validate(access.document)


@router.get("/documents/{document_id}/status")
async def get_document_status(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> DocumentStatusOut:
    """Processing status only. Cheap enough to poll."""
    access = await service.get_access(session, document_id, user.id)
    return DocumentStatusOut.model_validate(access.document)


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    storage_provider: StorageDep,
) -> DownloadOut:
    """A short-lived link to the original file. The file itself is never public."""
    expires_in = get_settings().signed_url_ttl_seconds
    document, url = await service.signed_download_url(
        session, storage_provider(), document_id, user.id, expires_in
    )
    # The link is a credential while it lasts: keep it out of caches.
    response.headers["Cache-Control"] = "no-store"
    return DownloadOut(url=url, expires_in=expires_in, filename=document.filename)


@router.post("/documents/{document_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_document(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, queue: QueueDep
) -> DocumentOut:
    """Process a failed document again. Its uploader, or an admin or owner."""
    document = await service.retry(session, queue, document_id, user.id)
    return DocumentOut.model_validate(document)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, user: CurrentUser, session: DbSession, storage_provider: StorageDep
) -> Response:
    """Delete a document and its file. Its uploader, or an admin or owner."""
    await service.remove(session, storage_provider(), document_id, user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
