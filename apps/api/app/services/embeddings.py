"""Turns text into embedding vectors with the OpenAI API.

Knows nothing about documents, chunks or the database: it takes strings and returns one
vector per string. Chunking lives in `chunking.py`; the pipeline joins the two.

Text sent here is document content and search queries. None of it is ever logged, and
neither is the API key.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import openai
from openai import AsyncOpenAI

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.chunk import EMBEDDING_DIMENSIONS
from app.services.openai_client import TRANSIENT_ERRORS, backoff_delay, openai_client

logger = logging.getLogger(__name__)

# OpenAI accepts up to 2,048 inputs and 300,000 tokens per request. At 800 tokens a chunk,
# 64 inputs is about 51,000 tokens, comfortably inside both limits.
MAX_BATCH_SIZE = 256


class EmbeddingError(Exception):
    """Embeddings could not be produced. `retryable` says whether trying later could help."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Embedder(Protocol):
    """What the pipeline and search need. Swappable for a fake in tests."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class Embedded:
    vectors: list[list[float]]
    # Tokens the provider reported for the request(s), or None if it reported none.
    input_tokens: int | None


async def embed_metered(embedder: Embedder, texts: list[str]) -> Embedded:
    """Embed, and say how many tokens the provider counted when the embedder can tell."""
    metered = getattr(embedder, "embed_with_usage", None)
    if metered is not None:
        return await metered(texts)
    return Embedded(await embedder.embed(texts), None)


EmbedderProvider = Callable[[], Embedder]
Sleep = Callable[[float], Awaitable[None]]


class OpenAIEmbedder:
    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str,
        dimensions: int = EMBEDDING_DIMENSIONS,
        batch_size: int = 64,
        max_attempts: int = 5,
        base_delay_seconds: float = 1.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
        if max_attempts < 1:
            raise ValueError("at least one attempt is required")
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._sleep = sleep

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input, in the same order. Identical inputs are sent once."""
        return (await self.embed_with_usage(texts)).vectors

    async def embed_with_usage(self, texts: list[str]) -> Embedded:
        """`embed`, with the token count the provider reported (None if any request lacked
        one)."""
        if not texts:
            return Embedded([], 0)
        if any(not text.strip() for text in texts):
            raise EmbeddingError("Cannot embed empty text.", retryable=False)

        unique = list(dict.fromkeys(texts))
        vectors: dict[str, list[float]] = {}
        tokens: int | None = 0
        # One request at a time: simple, and it keeps well inside the API's rate limits.
        for start in range(0, len(unique), self._batch_size):
            batch = unique[start : start + self._batch_size]
            batch_vectors, batch_tokens = await self._embed_batch(batch)
            for text, vector in zip(batch, batch_vectors, strict=True):
                vectors[text] = vector
            tokens = None if tokens is None or batch_tokens is None else tokens + batch_tokens
        return Embedded([vectors[text] for text in texts], tokens)

    async def _embed_batch(self, batch: list[str]) -> tuple[list[list[float]], int | None]:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await self._request(batch)
            except TRANSIENT_ERRORS as error:
                # Worth another try: the request itself was fine.
                if attempt == self._max_attempts:
                    logger.error(
                        "Embedding request failed after %d attempts: %s",
                        attempt,
                        type(error).__name__,
                    )
                    raise EmbeddingError(
                        "The embedding service is busy or unreachable.", retryable=True
                    ) from error
                delay = self._delay(attempt, error)
                logger.warning(
                    "Embedding request failed (%s); attempt %d of %d, retrying in %.1fs",
                    type(error).__name__,
                    attempt,
                    self._max_attempts,
                    delay,
                )
                await self._sleep(delay)
            except openai.APIStatusError as error:
                # 400, 401, 403, 404 and the like: sending it again will not change the answer.
                logger.error("Embedding request rejected with status %d", error.status_code)
                raise EmbeddingError(
                    "The embedding service rejected the request.", retryable=False
                ) from error
            except openai.OpenAIError as error:
                logger.error("Embedding request failed: %s", type(error).__name__)
                raise EmbeddingError(
                    "The embedding service returned an error.", retryable=False
                ) from error
        raise AssertionError("unreachable")  # pragma: no cover

    def _delay(self, attempt: int, error: Exception) -> float:
        """Exponential backoff with jitter, or the wait the API asked for if that is longer."""
        return backoff_delay(attempt, self._base_delay, error)

    async def _request(self, batch: list[str]) -> tuple[list[list[float]], int | None]:
        # The SDK's own retries are switched off (see `get_embedder`), so retries happen
        # only in `_embed_batch`, where they are bounded and observable.
        response = await self._client.embeddings.create(
            model=self._model,
            input=batch,
            dimensions=self._dimensions,
            encoding_format="float",
        )
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "prompt_tokens", None)
        return self._validated(response.data, len(batch)), tokens if isinstance(
            tokens, int
        ) else None

    def _validated(self, data: list, expected: int) -> list[list[float]]:  # noqa: ANN001
        """Refuse a response that is not exactly one well-formed vector per input."""
        if len(data) != expected:
            logger.error("Embedding response had %d vectors for %d inputs", len(data), expected)
            raise EmbeddingError(
                "The embedding service returned an invalid response.", retryable=False
            )
        ordered = sorted(data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(expected)):
            logger.error("Embedding response had missing or repeated positions")
            raise EmbeddingError(
                "The embedding service returned an invalid response.", retryable=False
            )
        vectors = []
        for item in ordered:
            vector = item.embedding
            if (
                not isinstance(vector, list)
                or len(vector) != self._dimensions
                or not all(
                    isinstance(value, int | float) and math.isfinite(value) for value in vector
                )
            ):
                logger.error(
                    "Embedding response had a malformed vector (expected %d dimensions)",
                    self._dimensions,
                )
                raise EmbeddingError(
                    "The embedding service returned an invalid response.", retryable=False
                )
            vectors.append([float(value) for value in vector])
        return vectors


def embeddings_not_configured() -> AppError:
    return AppError(
        503, "embeddings_not_configured", "Search is not configured. Set OPENAI_API_KEY."
    )


@lru_cache
def get_embedder() -> Embedder:
    """The configured embedder. Fails clearly when no API key has been set."""
    settings = get_settings()
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise embeddings_not_configured()
    if settings.embedding_dimensions != EMBEDDING_DIMENSIONS:
        # The vector column has a fixed size; a different size needs a migration first.
        raise AppError(
            500,
            "embeddings_misconfigured",
            "The embedding size does not match the database.",
        )
    client = openai_client(api_key).with_options(timeout=settings.embedding_timeout_seconds)
    return OpenAIEmbedder(
        client,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
        max_attempts=settings.embedding_max_attempts,
    )
