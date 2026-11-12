import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_QUERY_LENGTH = 1000
MAX_DOCUMENT_FILTER = 50

SearchQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_LENGTH)
]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: SearchQuery
    # The upper bound is configurable (SEARCH_MAX_TOP_K) and enforced by the route.
    top_k: int = Field(default=5, ge=1)
    # Restrict the search to these documents. Empty means the whole workspace.
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_DOCUMENT_FILTER)
    # Leave out results less similar than this. Defaults to the server's setting.
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)


class SearchSource(BaseModel):
    """Where the passage sits in its document. Only what the file format provides is set."""

    page_number: int | None
    paragraph_index: int | None
    line_start: int | None
    line_end: int | None


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content: str
    # Cosine similarity between the query and the passage: 1 is identical in meaning.
    similarity: float
    source: SearchSource


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
