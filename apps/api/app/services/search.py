"""Semantic search over a workspace's document chunks.

Tenant isolation is part of the SQL itself: the one query that reads chunks joins the
caller's own membership row and filters on the workspace, so a chunk from another workspace
cannot be returned whatever workspace or document IDs the caller sends.

Chunk text is returned as data for display. It is never interpreted here, and anything that
later feeds it to a language model must treat it as untrusted.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.chunk import DocumentChunk
from app.models.document import Document, DocumentStatus
from app.models.workspace import WorkspaceMember
from app.services import workspaces as workspace_service
from app.services.embeddings import Embedder, EmbeddingError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Match:
    """One search hit, as plain values that stay valid after the database session moves on."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content: str
    similarity: float
    page_number: int | None
    paragraph_index: int | None
    line_start: int | None
    line_end: int | None


def embeddings_unavailable() -> AppError:
    return AppError(503, "embeddings_unavailable", "Search is temporarily unavailable. Try again.")


async def search(
    session: AsyncSession,
    embedder: Embedder,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    top_k: int,
    document_ids: list[uuid.UUID],
    min_similarity: float,
) -> list[Match]:
    # 404 for a workspace the caller is not in, before any text is sent to the embedding API.
    await workspace_service.get_access(session, workspace_id, user_id)
    # Do not hold a database connection while waiting on the embedding API.
    await session.rollback()

    try:
        (query_vector,) = await embedder.embed([query])
    except EmbeddingError as error:
        logger.error("Query embedding failed (retryable: %s)", error.retryable)
        raise embeddings_unavailable() from error

    distance = DocumentChunk.embedding.cosine_distance(query_vector)
    statement = (
        # Only the columns the response needs: never the vector, the workspace, or metadata.
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            Document.filename,
            DocumentChunk.content,
            (1 - distance).label("similarity"),
            DocumentChunk.page_number,
            DocumentChunk.paragraph_index,
            DocumentChunk.line_start,
            DocumentChunk.line_end,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        # The caller's membership is re-proved inside the query, so the result depends on
        # the database's view at this instant and not on the check made above.
        .join(
            WorkspaceMember,
            (WorkspaceMember.workspace_id == DocumentChunk.workspace_id)
            & (WorkspaceMember.user_id == user_id),
        )
        .where(
            DocumentChunk.workspace_id == workspace_id,
            Document.workspace_id == workspace_id,
            # Chunks of a document still being processed are incomplete and stay hidden.
            Document.status == DocumentStatus.READY.value,
        )
        .order_by(distance)
        .limit(top_k)
    )
    if document_ids:
        # IDs from another workspace simply match nothing: the workspace filter still applies.
        statement = statement.where(DocumentChunk.document_id.in_(document_ids))
    if min_similarity > 0:
        statement = statement.where(distance <= 1 - min_similarity)

    # With a filter, an approximate index can find fewer than `top_k` matching rows in its
    # first pass. Iterative scanning keeps going until enough rows pass the filter.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
    rows = (await session.execute(statement)).all()
    await session.rollback()

    logger.info("Search in workspace %s returned %d result(s)", workspace_id, len(rows))
    return [
        Match(
            chunk_id=row.id,
            document_id=row.document_id,
            document_name=row.filename,
            content=row.content,
            # Clamped: floating-point error can put an exact match a hair above 1.
            similarity=round(max(-1.0, min(1.0, float(row.similarity))), 4),
            page_number=row.page_number,
            paragraph_index=row.paragraph_index,
            line_start=row.line_start,
            line_end=row.line_end,
        )
        for row in rows
    ]
