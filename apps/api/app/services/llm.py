"""Generating text with a language model.

The only module that knows a provider's API. It takes instructions, conversation turns and a
JSON schema for the output, and returns the raw output with token counts. It knows nothing
about documents, prompts or citations.

Output comes back whole (`generate`) or as it is written (`stream`). Both send the same
request and fail in the same way.

Neither the instructions, the turns nor the output are ever logged, and neither is the key.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Protocol

import anyio
import openai

from app.core.config import chat_model_capabilities, get_settings
from app.core.errors import AppError
from app.core.metrics import metrics
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


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A piece of output, in the order the model writes it. Raw output: for a JSON schema,
    a fragment of the JSON document."""

    text: str


# What `stream` yields: text pieces, then exactly one Generation for the whole output.
StreamEvent = TextDelta | Generation


class LLMError(Exception):
    """Generation failed. `code` says why; the message never contains request content.

    unavailable       rate limited, timed out or a server error, even after retrying
    misconfigured     the key, permissions or model name were rejected
    rejected          the request itself was refused (for example, too long)
    invalid_response  the model returned nothing usable
    too_long          the answer reached the output token limit before it was finished
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        model: str | None = None,
        maybe_executed: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        # False when the provider is known not to have run the request (it refused it with a
        # rate limit); True when it may have (a timeout, a lost connection, a server error).
        self.maybe_executed = maybe_executed
        # What the provider reported using, when it reported anything despite the failure
        # (an answer cut off by the output limit is still billed).
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model


class ChatModel(Protocol):
    """What the chat service needs from a model. Swappable for a fake in tests."""

    async def generate(
        self, *, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> Generation: ...

    def stream(
        self, *, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> AsyncIterator[StreamEvent]:
        """Text pieces as they are written, then the whole Generation. Closing the iterator
        early stops the generation upstream."""
        ...


ChatModelProvider = Callable[[], ChatModel]


class OpenAIResponsesModel:
    """OpenAI's Responses API, asking for output that matches a strict JSON schema."""

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        model: str,
        temperature: float | None,
        max_output_tokens: int,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        stream_timeout_seconds: float = 120.0,
        sleep: Sleep = asyncio.sleep,
        reasoning_effort: str | None = None,
        fallback: "OpenAIResponsesModel | None" = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("at least one attempt is required")
        self._client = client
        self._model = model
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        # Tried once, for one request, when the configured model is reported as not found.
        self._fallback = fallback
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds
        self._stream_timeout = stream_timeout_seconds
        self._sleep = sleep

    def _request(
        self, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        return {
            "model": self._model,
            "instructions": instructions,
            "input": [{"role": turn.role, "content": turn.content} for turn in turns],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
            "max_output_tokens": self._max_output_tokens,
            # Do not keep the conversation on OpenAI's side.
            "store": False,
            # Only what this model accepts (some reject `temperature`, others `reasoning`).
            **({"temperature": self._temperature} if self._temperature is not None else {}),
            **(
                {"reasoning": {"effort": self._reasoning_effort}}
                if self._reasoning_effort is not None
                else {}
            ),
        }

    def _permanent(self, error: openai.OpenAIError) -> LLMError:
        """An error that resending cannot fix."""
        if isinstance(error, openai.APIStatusError):
            logger.error("Model request rejected with status %d", error.status_code)
            if error.status_code in MISCONFIGURED_STATUSES:
                failure = LLMError(
                    "The AI service rejected the server's configuration.",
                    code="misconfigured",
                    retryable=False,
                )
                failure.model_not_found = error.status_code == 404
                return failure
            return LLMError(
                "The AI service rejected the request.", code="rejected", retryable=False
            )
        logger.error("Model request failed: %s", type(error).__name__)
        return LLMError("The AI service returned an error.", code="unavailable", retryable=False)

    def _record(self, mode: str, outcome: str, started: float) -> None:
        metrics.inc("llm_requests_total", model=self._model, mode=mode, outcome=outcome)
        metrics.observe(
            "llm_duration_seconds", time.perf_counter() - started, model=self._model, mode=mode
        )

    async def _before_retry(self, attempt: int, error: Exception) -> None:
        """Wait before the next attempt, or raise if this was the last one."""
        metrics.inc("llm_retries_total", model=self._model, error=type(error).__name__)
        if attempt == self._max_attempts:
            logger.error(
                "Model request failed after %d attempts: %s", attempt, type(error).__name__
            )
            raise unavailable(
                maybe_executed=not isinstance(error, openai.RateLimitError)
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

    async def generate(
        self, *, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> Generation:
        started = time.perf_counter()
        try:
            result = await self._generate(instructions, turns, schema, schema_name)
            self._record("generate", "ok", started)
            return result
        except LLMError as error:
            self._record("generate", error.code, started)
            if not (self._fallback and getattr(error, "model_not_found", False)):
                raise
            metrics.inc("llm_fallbacks_total", model=self._model, fallback=self._fallback._model)
            logger.error(
                "Chat model %s not found; using fallback %s", self._model, self._fallback._model
            )
            return await self._fallback.generate(
                instructions=instructions, turns=turns, schema=schema, schema_name=schema_name
            )

    async def _generate(
        self, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> Generation:
        request = self._request(instructions, turns, schema, schema_name)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.responses.create(**request)
            except TRANSIENT_ERRORS as error:
                await self._before_retry(attempt, error)
            except openai.OpenAIError as error:
                raise self._permanent(error) from error
            else:
                return self._read(response)
        raise AssertionError("unreachable")  # pragma: no cover

    async def stream(
        self, *, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> AsyncIterator[StreamEvent]:
        """`_stream`, falling back once to the fallback model if the configured one is reported
        as not found before any output."""
        started = False
        began = time.perf_counter()
        outcome = "cancelled"  # unless it ends otherwise: the consumer stopped reading
        try:
            async with aclosing(self._stream(instructions, turns, schema, schema_name)) as inner:
                async for item in inner:
                    if not started:
                        metrics.observe(
                            "llm_first_token_seconds",
                            time.perf_counter() - began,
                            model=self._model,
                        )
                    started = True
                    yield item
            outcome = "ok"
            return
        except LLMError as error:
            outcome = error.code
            if started or not (self._fallback and getattr(error, "model_not_found", False)):
                raise
            metrics.inc("llm_fallbacks_total", model=self._model, fallback=self._fallback._model)
            logger.error(
                "Chat model %s not found; using fallback %s", self._model, self._fallback._model
            )
        finally:
            self._record("stream", outcome, began)
        async with aclosing(
            self._fallback.stream(
                instructions=instructions, turns=turns, schema=schema, schema_name=schema_name
            )
        ) as fallback:
            async for item in fallback:
                yield item

    async def _stream(
        self, instructions: str, turns: list[Turn], schema: dict[str, Any], schema_name: str
    ) -> AsyncIterator[StreamEvent]:
        """Stream the output. A failure before any text has been yielded is retried like
        `generate`; once text has been yielded, a failure ends the stream with an LLMError,
        since the caller has already used part of the output.

        The whole stream must finish within `stream_timeout_seconds`, and each wait for the
        next event is bounded by the client's own timeout.
        """
        request = self._request(instructions, turns, schema, schema_name)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stream_timeout
        for attempt in range(1, self._max_attempts + 1):
            started = False
            try:
                upstream = await asyncio.wait_for(
                    self._client.responses.create(**request, stream=True),
                    max(deadline - loop.time(), 0.001),
                )
                try:
                    events = upstream.__aiter__()
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                events.__anext__(), max(deadline - loop.time(), 0.001)
                            )
                        except StopAsyncIteration:
                            logger.error("Model stream ended without a final response")
                            raise LLMError(
                                "The AI service did not finish its answer.",
                                code="invalid_response",
                                retryable=False,
                            ) from None
                        kind = getattr(event, "type", None)
                        if kind == "response.output_text.delta":
                            if event.delta:
                                started = True
                                yield TextDelta(event.delta)
                        elif kind in ("response.completed", "response.incomplete"):
                            yield self._read(event.response)
                            return
                        elif kind == "response.failed":
                            raise self._failed(getattr(event.response, "error", None))
                        elif kind == "error":
                            raise self._failed(event)
                finally:
                    # Stops generation upstream when the caller stops early, even while the
                    # caller is being cancelled.
                    with anyio.CancelScope(shield=True):
                        await upstream.close()
            except (*TRANSIENT_ERRORS, TimeoutError) as error:
                if started or loop.time() >= deadline:
                    logger.error("Model stream failed: %s", type(error).__name__)
                    raise unavailable() from error
                await self._before_retry(attempt, error)
            except openai.OpenAIError as error:
                raise self._permanent(error) from error
        raise AssertionError("unreachable")  # pragma: no cover

    def _failed(self, error: Any) -> LLMError:  # noqa: ANN401 - the SDK's error objects
        """An error reported inside the stream. Only its code is logged, never its message."""
        code = getattr(error, "code", None) or "unknown"
        logger.error("Model stream reported an error: %s", code)
        if code in ("rate_limit_exceeded", "server_error", "server_is_overloaded", "timeout"):
            return unavailable()
        return LLMError("The AI service could not answer.", code="rejected", retryable=False)

    def _read(self, response: Any) -> Generation:  # noqa: ANN401 - the SDK's Response
        if response.status != "completed":
            reason = getattr(response.incomplete_details, "reason", None) or response.status
            logger.error("Model response was not completed: %s", reason)
            if reason == "max_output_tokens":
                # An unfinished answer cannot be checked, and asking again gets the same.
                usage = response.usage
                raise LLMError(
                    "The answer reached the output limit.",
                    code="too_long",
                    retryable=False,
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                    model=response.model,
                )
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


def unavailable(*, maybe_executed: bool = True) -> LLMError:
    return LLMError(
        "The AI service is busy or unreachable.",
        code="unavailable",
        retryable=True,
        maybe_executed=maybe_executed,
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
    client = openai_client(api_key).with_options(timeout=settings.chat_timeout_seconds)

    def build(model: str, fallback: OpenAIResponsesModel | None = None) -> OpenAIResponsesModel:
        accepts = chat_model_capabilities(model)
        return OpenAIResponsesModel(
            client,
            # The model is always a setting: no code names one.
            model=model,
            temperature=settings.chat_temperature if accepts.get("temperature") else None,
            reasoning_effort=settings.chat_reasoning_effort
            if accepts.get("reasoning_effort")
            else None,
            max_output_tokens=settings.chat_max_output_tokens,
            max_attempts=settings.chat_max_attempts,
            stream_timeout_seconds=settings.chat_stream_timeout_seconds,
            fallback=fallback,
        )

    fallback = (
        build(settings.openai_chat_fallback_model)
        if settings.openai_chat_fallback_model
        and settings.openai_chat_fallback_model != settings.openai_chat_model
        else None
    )
    return build(settings.openai_chat_model, fallback)
