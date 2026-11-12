import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Fixed by the column type below. Changing the embedding model to one with a different
# output size needs a migration, not just a setting.
EMBEDDING_DIMENSIONS = 1536


class DocumentChunk(Base):
    """A passage of a document with its embedding, the unit that semantic search returns.

    `workspace_id` is stored on the chunk so search can filter by tenant without a join,
    and a composite foreign key guarantees it is the workspace of `document_id`: a chunk
    can never claim to belong to a workspace its document is not in.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            name="fk_document_chunks_document_id_workspace_id_documents",
            ondelete="CASCADE",
        ),
        # One chunk per position: reprocessing a document can never duplicate its chunks.
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_id_chunk_index"
        ),
        CheckConstraint("token_count > 0", name="token_count_positive"),
        Index("ix_document_chunks_workspace_id", "workspace_id"),
        # Approximate nearest-neighbour search by cosine distance.
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    # Reading order within the document, starting at 0.
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    # Where the chunk starts in the source. Only the references that exist for the file's
    # format are set: a DOCX chunk never has a page number, because Word files do not
    # record one.
    page_number: Mapped[int | None] = mapped_column(Integer)  # PDF, 1-based
    paragraph_index: Mapped[int | None] = mapped_column(Integer)  # DOCX, 0-based
    line_start: Mapped[int | None] = mapped_column(Integer)  # TXT, 1-based, inclusive
    line_end: Mapped[int | None] = mapped_column(Integer)  # TXT, 1-based, inclusive
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Where the chunk ends, and its section heading. Never credentials or user details.
    # The attribute is `chunk_metadata` because SQLAlchemy reserves the name `metadata`.
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
