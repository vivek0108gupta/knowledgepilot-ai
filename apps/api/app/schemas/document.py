import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.document import DocumentStatus


class DocumentOut(BaseModel):
    """A document as the API describes it. The storage path is internal and never included."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    uploaded_by: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    page_count: int | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentPage(BaseModel):
    """One page of a workspace's documents, newest first."""

    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class DocumentStatusOut(BaseModel):
    """The small payload a client polls while a document is being processed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DocumentStatus
    page_count: int | None
    error: str | None
    updated_at: datetime


class DownloadOut(BaseModel):
    """A link that downloads the file and stops working after `expires_in` seconds."""

    url: str
    expires_in: int
    filename: str
