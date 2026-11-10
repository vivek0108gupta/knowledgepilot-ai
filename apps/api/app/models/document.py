import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.workspace import utc_now

# 25 MB. The database enforces the same ceiling as the upload endpoint.
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class SegmentKind(StrEnum):
    """What one extracted segment corresponds to in the source file."""

    PAGE = "page"  # PDF: one page
    PARAGRAPH = "paragraph"  # DOCX: one paragraph or table row
    LINES = "lines"  # TXT: a block of consecutive lines


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded', 'processing', 'ready', 'failed')", name="status_valid"
        ),
        CheckConstraint(
            f"size_bytes > 0 AND size_bytes <= {MAX_DOCUMENT_BYTES}", name="size_bytes_range"
        ),
        # A stored file always lives under its own workspace and document ID. Enforced here
        # too, so no code path or manual edit can point a document at another tenant's file.
        CheckConstraint(
            "storage_path LIKE workspace_id::text || '/' || id::text || '/%'",
            name="storage_path_owned",
        ),
        # The same file content is stored once per workspace.
        UniqueConstraint("workspace_id", "sha256", name="uq_documents_workspace_id_sha256"),
        UniqueConstraint("storage_path", name="uq_documents_storage_path"),
        # Listing a workspace's documents newest first, optionally filtered by status.
        Index("ix_documents_workspace_id_created_at", "workspace_id", "created_at"),
        Index("ix_documents_workspace_id_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    # A Supabase Auth user ID from a verified token. Not a foreign key, for the same reason
    # as `workspaces.owner_id`.
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=DocumentStatus.UPLOADED.value)
    page_count: Mapped[int | None] = mapped_column(Integer)
    # Total characters of extracted text; null until processing succeeds.
    char_count: Mapped[int | None] = mapped_column(Integer)
    # A message safe to show the uploader; set only when status is `failed`.
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utc_now
    )


class DocumentSegment(Base):
    """A piece of extracted text with a reference back to where it sits in the source file.

    Phase 4 builds chunks and citations from these rows; nothing here is an embedding.
    """

    __tablename__ = "document_segments"
    __table_args__ = (
        CheckConstraint("kind IN ('page', 'paragraph', 'lines')", name="kind_valid"),
        UniqueConstraint(
            "document_id", "position", name="uq_document_segments_document_id_position"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE")
    )
    # Reading order within the document, starting at 0.
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    # Exactly the references that apply to `kind` are set; the rest stay null.
    page_number: Mapped[int | None] = mapped_column(Integer)  # 1-based
    paragraph_index: Mapped[int | None] = mapped_column(Integer)  # 0-based
    line_start: Mapped[int | None] = mapped_column(Integer)  # 1-based, inclusive
    line_end: Mapped[int | None] = mapped_column(Integer)  # 1-based, inclusive
    # DOCX: the nearest heading above this paragraph, for "section" citations.
    section: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
