"""Generating text with a language model.

The only module that knows a provider's API. It takes instructions, conversation turns and a
JSON schema for the output, and returns the raw output with token counts. It knows nothing
about documents, prompts or citations.

Neither the instructions, the turns nor the output are ever logged, and neither is the key.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Protocol

import openai

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.openai_client import TRANSIENT_ERRORS, backoff_delay, openai_client

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

# Statuses where the request was understood and resending it cannot help.
MISCONFIGURED_STATUSES = {401, 403, 404}


@dataclass(frozen=True, slots=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Generation:
    text: str
    # True when the model declined to answer instead of producing output.
    refused: bool
    model: str
    input_tokens: int | None
    output_tokens: int | None


class LLMError(Exception):
    """Generation failed. `code` says why; the message never contains request content.

    unavailable       rate limited, timed out or a server error, even after retrying
    misconfigured     the key, permissions or model name were rejected
    rejected          the request itself was refused (for example, too long)
    invalid_response  the model returned nothing usable
    """

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ChatModel(Protocol):
    """What the chat service needs from a model. Swappable for a fake in tests."""

    async def generate(
        self, *, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> Generation: ...


ChatModelProvider = Callable[[], ChatModel]


class OpenAIResponsesModel:
    """OpenAI's Responses API, asking for output that matches a strict JSON schema."""

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        model: str,
        temperature: float,
        max_output_tokens: int,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("at least one attempt is required")
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._sleep = sleep

    async def generate(
        self, *, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> Generation:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.responses.create(
                    model=self._model,
                    instructions=instructions,
                    input=[{"role": turn.role, "content": turn.content} for turn in turns],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": schema_name,
                            "schema": schema,
                            "strict": True,
                        }
                    },
                    temperature=self._temperature,
                    max_output_tokens=self._max_output_tokens,
                    # Do not keep the conversation on OpenAI's side.
                    store=False,
                )
            except TRANSIENT_ERRORS as error:
                if attempt == self._max_attempts:
                    logger.error(
                        "Model request failed after %d attempts: %s", attempt, type(error).__name__
                    )
                    raise LLMError(
                        "The AI service is busy or unreachable.", code="unavailable", retryable=True
                    ) from error
                delay = backoff_delay(attempt, self._base_delay, error)
                logger.warning(
                    "Model request failed (%s); attempt %d of %d, retrying in %.1fs",
                    type(error).__name__,
                    attempt,
                    self._max_attempts,
                    delay,
                )
                await self._sleep(delay)
            except openai.APIStatusError as error:
                logger.error("Model request rejected with status %d", error.status_code)
                if error.status_code in MISCONFIGURED_STATUSES:
                    raise LLMError(
                        "The AI service rejected the server's configuration.",
                        code="misconfigured",
                        retryable=False,
                    ) from error
                raise LLMError(
                    "The AI service rejected the request.", code="rejected", retryable=False
                ) from error
            except openai.OpenAIError as error:
                logger.error("Model request failed: %s", type(error).__name__)
                raise LLMError(
                    "The AI service returned an error.", code="unavailable", retryable=False
                ) from error
            else:
                return self._read(response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _read(self, response: Any) -> Generation:  # noqa: ANN401 - the SDK's Response
        if response.status != "completed":
            reason = getattr(response.incomplete_details, "reason", None) or response.status
            logger.error("Model response was not completed: %s", reason)
            raise LLMError(
                "The AI service did not finish its answer.",
                code="invalid_response",
                retryable=False,
            )

        refused = any(
            getattr(part, "type", None) == "refusal"
            for item in response.output or []
            if getattr(item, "type", None) == "message"
            for part in getattr(item, "content", None) or []
        )
        text = response.output_text or ""
        if not refused and not text.strip():
            logger.error("Model response contained no text")
            raise LLMError(
                "The AI service returned an empty answer.",
                code="invalid_response",
                retryable=False,
            )

        usage = response.usage
        return Generation(
            text=text,
            refused=refused,
            model=response.model or self._model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )


def ai_not_configured() -> AppError:
    return AppError(
        503,
        "ai_not_configured",
        "Answering questions is not set up on the server yet. Set OPENAI_API_KEY.",
    )


@lru_cache
def get_chat_model() -> ChatModel:
    """The configured model. Fails clearly when no API key has been set."""
    settings = get_settings()
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise ai_not_configured()
    return OpenAIResponsesModel(
        openai_client(api_key).with_options(timeout=settings.chat_timeout_seconds),
        # The model is always a setting: no code names one.
        model=settings.openai_chat_model,
        temperature=settings.chat_temperature,
        max_output_tokens=settings.chat_max_output_tokens,
        max_attempts=settings.chat_max_attempts,
    )
