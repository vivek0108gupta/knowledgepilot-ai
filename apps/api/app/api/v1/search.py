import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.search import SearchRequest, SearchResponse, SearchResult, SearchSource
from app.services import search as service
from app.services.embeddings import EmbedderProvider, get_embedder

router = APIRouter(tags=["search"])


def get_embedder_provider() -> EmbedderProvider:
    """How routes reach the embedder. A callable, so a missing API key fails only the
    requests that need embeddings."""
    return get_embedder


EmbedderDep = Annotated[EmbedderProvider, Depends(get_embedder_provider)]


@router.post("/workspaces/{workspace_id}/search")
async def search_workspace(
    workspace_id: uuid.UUID,
    body: SearchRequest,
    user: CurrentUser,
    session: DbSession,
    embedder_provider: EmbedderDep,
) -> SearchResponse:
    """Find the passages in a workspace's documents closest in meaning to the query.

    Only members of the workspace may search it, and only fully processed documents are
    searched. Results come from this workspace alone.
    """
    settings = get_settings()
    if body.top_k > settings.search_max_top_k:
        raise AppError(
            422, "validation_error", f"top_k can be at most {settings.search_max_top_k}."
        )

    matches = await service.search(
        session,
        embedder_provider(),
        workspace_id,
        user.id,
        body.query,
        body.top_k,
        body.document_ids,
        settings.search_min_similarity if body.min_similarity is None else body.min_similarity,
    )
    return SearchResponse(
        query=body.query,
        results=[
            SearchResult(
                chunk_id=match.chunk_id,
                document_id=match.document_id,
                document_name=match.document_name,
                content=match.content,
                similarity=match.similarity,
                source=SearchSource(
                    page_number=match.page_number,
                    paragraph_index=match.paragraph_index,
                    line_start=match.line_start,
                    line_end=match.line_end,
                ),
            )
            for match in matches
        ],
    )
