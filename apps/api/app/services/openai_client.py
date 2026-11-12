"""The single OpenAI client configuration, shared by embeddings and answer generation.

Each caller sets its own timeout per request (`with_options`). The SDK's built-in retries are
switched off: callers retry themselves, so the number of attempts is bounded and logged in
one place per feature.
"""

import random
from functools import lru_cache

import openai
from openai import AsyncOpenAI

MAX_RETRY_DELAY_SECONDS = 30.0

# Errors where the request itself was fine and trying again later can succeed.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


@lru_cache
def openai_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, max_retries=0)


def retry_after_seconds(error: Exception) -> float | None:
    """The wait the API asked for, if it sent a usable Retry-After header."""
    if not isinstance(error, openai.APIStatusError):
        return None
    try:
        value = float(error.response.headers.get("retry-after", ""))
    except (ValueError, AttributeError):
        return None
    return value if 0 <= value <= MAX_RETRY_DELAY_SECONDS else None


def backoff_delay(attempt: int, base_delay: float, error: Exception) -> float:
    """Exponential backoff with jitter, or the wait the API asked for if that is longer."""
    backoff = min(base_delay * 2 ** (attempt - 1), MAX_RETRY_DELAY_SECONDS)
    backoff *= 0.5 + random.random() / 2  # noqa: S311 - jitter, not security
    return max(backoff, retry_after_seconds(error) or 0.0)
