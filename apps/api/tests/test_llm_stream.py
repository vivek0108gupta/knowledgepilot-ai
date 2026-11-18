"""Streaming from the model, through the real OpenAI SDK against a scripted Responses API
event stream. Nothing here reaches OpenAI."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from openai import AsyncOpenAI, AsyncStream

from app.core.config import Settings
from app.services.llm import Generation, LLMError, OpenAIResponsesModel, TextDelta, Turn
from tests.test_llm import API_KEY, SCHEMA, response_body

OUTPUT = '{"answer": "Refunds within 30 days."}'


def delta(text: str, sequence: int = 1) -> dict:
    return {
        "type": "response.output_text.delta",
        "item_id": "msg_1",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
        "sequence_number": sequence,
        "logprobs": [],
    }


def completed(text: str = OUTPUT, **overrides: object) -> dict:
    body = response_body(**overrides)
    if "output" not in overrides:
        body["output"][0]["content"][0]["text"] = text
    return {"type": "response.completed", "sequence_number": 99, "response": body}


def pieces(text: str = OUTPUT, size: int = 6) -> list[dict]:
    return [delta(text[i : i + size], i) for i in range(0, len(text), size)]


def encode(events: list[dict]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


class Upstream:
    """A scripted Responses API. Each request takes the next step: an HTTP status, an
    exception, or a list of events (an `Exception` inside the list is raised mid-stream)."""

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []
        self.closed = 0
        self.event_delay = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx.Response(
                step, json={"error": {"message": "scripted", "type": "test", "code": None}}
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=self.body(step)
        )

    async def body(self, events: list) -> AsyncIterator[bytes]:
        try:
            for event in events:
                if self.event_delay:
                    await asyncio.sleep(self.event_delay)
                if isinstance(event, Exception):
                    raise event
                yield encode([event])
        finally:
            self.closed += 1

    def model(self, **options: object) -> tuple[OpenAIResponsesModel, list[float]]:
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = AsyncOpenAI(
            api_key=API_KEY,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )
        defaults: dict[str, object] = {
            "model": "gpt-4.1-mini",
            "temperature": 0.2,
            "max_output_tokens": 1000,
            "sleep": sleep,
        }
        return OpenAIResponsesModel(client, **{**defaults, **options}), sleeps  # type: ignore[arg-type]


def stream(model: OpenAIResponsesModel):  # noqa: ANN201
    return model.stream(
        instructions="Follow the rules.",
        turns=[Turn("user", "What is the refund policy?")],
        schema=SCHEMA,
        schema_name="answer",
    )


async def collect(model: OpenAIResponsesModel) -> list:
    return [item async for item in stream(model)]


class TestStreaming:
    async def test_text_arrives_in_pieces_then_the_whole_generation(self) -> None:
        upstream = Upstream([*pieces(), completed()])
        model, _ = upstream.model()

        items = await collect(model)

        texts = [item.text for item in items if isinstance(item, TextDelta)]
        assert len(texts) > 1
        assert "".join(texts) == OUTPUT
        final = items[-1]
        assert isinstance(final, Generation)
        assert (final.text, final.refused, final.model) == (
            OUTPUT,
            False,
            "gpt-4.1-mini-2025-04-14",
        )
        assert (final.input_tokens, final.output_tokens) == (321, 45)
        assert sum(isinstance(item, Generation) for item in items) == 1

    async def test_sends_the_same_request_as_generate_with_streaming_on(self) -> None:
        upstream = Upstream([*pieces(), completed()])
        model, _ = upstream.model()

        await collect(model)

        body = json.loads(upstream.requests[0].content)
        assert body["stream"] is True
        assert body["store"] is False
        assert body["model"] == "gpt-4.1-mini"
        assert body["text"]["format"] == {
            "type": "json_schema",
            "name": "answer",
            "schema": SCHEMA,
            "strict": True,
        }

    async def test_a_refusal_is_reported_rather_than_failing(self) -> None:
        refusal = [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}],
            }
        ]
        upstream = Upstream([completed(output=refusal)])
        model, _ = upstream.model()

        items = await collect(model)

        assert items[-1].refused is True
        assert not [item for item in items if isinstance(item, TextDelta)]

    async def test_stopping_early_closes_the_upstream_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[bool] = []
        original = AsyncStream.close

        async def close(self: AsyncStream) -> None:
            closed.append(True)
            await original(self)

        monkeypatch.setattr(AsyncStream, "close", close)
        upstream = Upstream([*pieces(), completed()])
        model, _ = upstream.model()

        iterator = stream(model)
        first = await anext(iterator)
        await iterator.aclose()

        assert isinstance(first, TextDelta)
        assert closed == [True]


class TestFailures:
    async def test_a_rate_limit_before_any_text_is_retried(self) -> None:
        upstream = Upstream(429, [*pieces(), completed()])
        model, sleeps = upstream.model()

        items = await collect(model)

        assert len(upstream.requests) == 2
        assert len(sleeps) == 1
        assert "".join(i.text for i in items if isinstance(i, TextDelta)) == OUTPUT

    @pytest.mark.parametrize(
        ("step", "code", "attempts"),
        [
            (429, "unavailable", 3),
            (httpx.ReadTimeout("slow"), "unavailable", 3),
            (httpx.ConnectError("down"), "unavailable", 3),
            (500, "unavailable", 3),
            (401, "misconfigured", 1),
            (404, "misconfigured", 1),
            (400, "rejected", 1),
        ],
        ids=["rate limit", "timeout", "connection", "server error", "auth", "model", "bad request"],
    )
    async def test_failures_before_any_text(self, step: object, code: str, attempts: int) -> None:
        upstream = Upstream(step)
        model, _ = upstream.model()

        with pytest.raises(LLMError) as raised:
            await collect(model)

        assert raised.value.code == code
        assert len(upstream.requests) == attempts

    async def test_a_failure_after_text_was_sent_is_not_retried(self) -> None:
        upstream = Upstream([delta('{"answer": "Ref'), httpx.ReadTimeout("slow")])
        model, _ = upstream.model()
        received: list = []

        with pytest.raises(LLMError) as raised:
            async for item in stream(model):
                received.append(item)

        assert raised.value.code == "unavailable"
        assert len(upstream.requests) == 1
        assert [item.text for item in received] == ['{"answer": "Ref']

    @pytest.mark.parametrize(
        ("event", "code"),
        [
            (
                {
                    "type": "response.failed",
                    "sequence_number": 3,
                    "response": response_body(
                        status="failed", error={"code": "server_error", "message": "boom"}
                    ),
                },
                "unavailable",
            ),
            (
                {
                    "type": "response.failed",
                    "sequence_number": 3,
                    "response": response_body(
                        status="failed", error={"code": "invalid_prompt", "message": "no"}
                    ),
                },
                "rejected",
            ),
            (
                {
                    "type": "error",
                    "code": "rate_limit_exceeded",
                    "message": "slow down",
                    "param": None,
                    "sequence_number": 3,
                },
                "unavailable",
            ),
        ],
        ids=["failed: server error", "failed: rejected", "error event"],
    )
    async def test_errors_reported_inside_the_stream(self, event: dict, code: str) -> None:
        upstream = Upstream([delta('{"ans'), event])
        model, _ = upstream.model()

        with pytest.raises(LLMError) as raised:
            await collect(model)

        assert raised.value.code == code
        assert len(upstream.requests) == 1

    async def test_an_answer_cut_off_by_the_output_limit_is_too_long(self) -> None:
        event = {
            "type": "response.incomplete",
            "sequence_number": 4,
            "response": response_body(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            ),
        }
        upstream = Upstream([*pieces()[:2], event])
        model, _ = upstream.model()

        with pytest.raises(LLMError) as raised:
            await collect(model)

        assert raised.value.code == "too_long"

    async def test_a_stream_that_ends_without_a_final_response_is_invalid(self) -> None:
        upstream = Upstream(pieces())
        model, _ = upstream.model()

        with pytest.raises(LLMError) as raised:
            await collect(model)

        assert raised.value.code == "invalid_response"

    async def test_the_whole_stream_has_a_deadline(self) -> None:
        upstream = Upstream([*pieces(), completed()])
        upstream.event_delay = 0.05
        model, _ = upstream.model(stream_timeout_seconds=0.12)

        with pytest.raises(LLMError) as raised:
            await collect(model)

        assert raised.value.code == "unavailable"
        assert upstream.closed == 1

    async def test_neither_the_key_nor_the_content_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        upstream = Upstream(429, [*pieces(), completed()])
        model, _ = upstream.model()

        await collect(model)

        logged = caplog.text
        assert API_KEY not in logged
        assert "refund" not in logged.lower()
        assert "Follow the rules" not in logged


def test_the_lease_outlasts_the_longest_stream() -> None:
    settings = Settings()

    assert settings.chat_generation_lease_seconds > settings.chat_stream_timeout_seconds
