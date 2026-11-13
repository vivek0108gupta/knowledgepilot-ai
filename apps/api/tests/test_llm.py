"""The model provider, through the real OpenAI SDK against a scripted Responses API.

Nothing here reaches OpenAI. The SDK's request building, response parsing and error classes
are all real.
"""

import json
import logging

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.services import llm as llm_module
from app.services.llm import LLMError, OpenAIResponsesModel, Turn, get_chat_model

API_KEY = "sk-test-key-never-real-0123456789"
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def response_body(**overrides: object) -> dict:
    body = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1700000000,
        "model": "gpt-4.1-mini-2025-04-14",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"answer": "ok"}', "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 321,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 45,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 366,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }
    body.update(overrides)
    return body


class Api:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.script: list[int | Exception] = []
        self.headers: dict[str, str] = {}
        self.body = response_body()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            if step != 200:
                return httpx.Response(
                    step,
                    headers=self.headers,
                    json={"error": {"message": "scripted", "type": "test", "code": None}},
                )
        return httpx.Response(200, json=self.body)

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


async def generate(model: OpenAIResponsesModel, turns: list[Turn] | None = None):  # noqa: ANN201
    return await model.generate(
        instructions="Follow the rules.",
        turns=turns or [Turn("user", "What is the refund policy?")],
        schema=SCHEMA,
        schema_name="answer",
    )


class TestSuccess:
    async def test_returns_the_text_model_and_token_counts(self) -> None:
        api = Api()
        model, _ = api.model()

        result = await generate(model)

        assert result.text == '{"answer": "ok"}'
        assert result.refused is False
        assert result.model == "gpt-4.1-mini-2025-04-14"
        assert (result.input_tokens, result.output_tokens) == (321, 45)

    async def test_sends_the_responses_api_request_with_a_strict_schema_and_no_storage(
        self,
    ) -> None:
        api = Api()
        model, _ = api.model()

        await generate(model, [Turn("user", "Q1"), Turn("assistant", "A1"), Turn("user", "Q2")])

        (request,) = api.requests
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        assert body["model"] == "gpt-4.1-mini"
        assert body["instructions"] == "Follow the rules."
        assert body["input"] == [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ]
        assert body["text"] == {
            "format": {"type": "json_schema", "name": "answer", "schema": SCHEMA, "strict": True}
        }
        assert body["store"] is False
        assert (body["temperature"], body["max_output_tokens"]) == (0.2, 1000)

    async def test_the_model_is_configurable(self) -> None:
        api = Api()
        model, _ = api.model(model="some-future-model")

        await generate(model)

        assert json.loads(api.requests[0].content)["model"] == "some-future-model"

    async def test_a_refusal_is_reported_rather_than_failing(self) -> None:
        api = Api()
        api.body = response_body(
            output=[
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "I can't help with that."}],
                }
            ]
        )
        model, _ = api.model()

        result = await generate(model)

        assert result.refused is True


class TestFailures:
    async def test_rate_limits_are_retried_with_backoff_then_succeed(self) -> None:
        api = Api()
        api.script = [429, 429]
        model, sleeps = api.model(max_attempts=3, base_delay_seconds=1.0)

        result = await generate(model)

        assert result.text
        assert len(api.requests) == 3
        assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0

    async def test_a_retry_after_header_is_respected(self) -> None:
        api = Api()
        api.script = [429]
        api.headers = {"retry-after": "4"}
        model, sleeps = api.model(base_delay_seconds=0.1)

        await generate(model)

        assert sleeps == [4.0]

    @pytest.mark.parametrize(
        "failure",
        [429, 500, 503, httpx.ReadTimeout("timed out"), httpx.ConnectError("refused")],
        ids=["rate limit", "500", "503", "timeout", "connection"],
    )
    async def test_transient_failures_give_up_after_the_attempt_limit(
        self, failure: object
    ) -> None:
        api = Api()
        api.script = [failure] * 10  # type: ignore[list-item]
        model, sleeps = api.model(max_attempts=3)

        with pytest.raises(LLMError) as caught:
            await generate(model)

        assert (caught.value.code, caught.value.retryable) == ("unavailable", True)
        assert len(api.requests) == 3
        assert len(sleeps) == 2

    @pytest.mark.parametrize("status", [401, 403, 404])
    async def test_authentication_permission_and_model_errors_are_not_retried(
        self, status: int
    ) -> None:
        api = Api()
        api.script = [status] * 5
        model, sleeps = api.model()

        with pytest.raises(LLMError) as caught:
            await generate(model)

        assert (caught.value.code, caught.value.retryable) == ("misconfigured", False)
        assert len(api.requests) == 1 and sleeps == []

    @pytest.mark.parametrize("status", [400, 409, 422])
    async def test_rejected_requests_are_not_retried(self, status: int) -> None:
        api = Api()
        api.script = [status] * 5
        model, _ = api.model()

        with pytest.raises(LLMError) as caught:
            await generate(model)

        assert caught.value.code == "rejected"
        assert len(api.requests) == 1

    async def test_an_answer_cut_off_by_the_output_limit_is_too_long(self) -> None:
        api = Api()
        api.body = response_body(
            status="incomplete", incomplete_details={"reason": "max_output_tokens"}
        )
        model, _ = api.model()

        with pytest.raises(LLMError) as caught:
            await generate(model)

        assert (caught.value.code, caught.value.retryable) == ("too_long", False)
        assert len(api.requests) == 1

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
            {"status": "failed"},
            {
                "output": [
                    {
                        "type": "message",
                        "id": "m",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "   ", "annotations": []}],
                    }
                ]
            },
            {"output": []},
        ],
        ids=["filtered", "failed", "blank text", "no output"],
    )
    async def test_unusable_responses_are_invalid(self, overrides: dict) -> None:
        api = Api()
        api.body = response_body(**overrides)
        model, _ = api.model()

        with pytest.raises(LLMError) as caught:
            await generate(model)

        assert (caught.value.code, caught.value.retryable) == ("invalid_response", False)

    async def test_neither_the_key_nor_the_conversation_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = Api()
        api.script = [429, 401]
        model, _ = api.model()

        with caplog.at_level(logging.DEBUG), pytest.raises(LLMError) as caught:
            await generate(model, [Turn("user", "my confidential merger question")])

        assert "retrying" in caplog.text
        for secret in (API_KEY, "confidential merger", "Follow the rules"):
            assert secret not in caplog.text
            assert secret not in str(caught.value)


class TestConfiguration:
    def configure(self, monkeypatch: pytest.MonkeyPatch, **values: object) -> None:
        monkeypatch.setattr(llm_module, "get_settings", lambda: Settings(**values))
        get_chat_model.cache_clear()

    def test_no_key_is_a_clear_configuration_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.configure(monkeypatch, openai_api_key="")

        with pytest.raises(AppError) as caught:
            get_chat_model()

        assert (caught.value.status_code, caught.value.code) == (503, "ai_not_configured")
        get_chat_model.cache_clear()

    def test_a_configured_model_uses_the_settings_and_the_shared_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(
            monkeypatch,
            openai_api_key=API_KEY,
            openai_chat_model="gpt-4.1-mini",
            chat_timeout_seconds=12.0,
            chat_max_attempts=4,
        )

        model = get_chat_model()

        assert isinstance(model, OpenAIResponsesModel)
        assert model._model == "gpt-4.1-mini"  # noqa: SLF001
        assert model._max_attempts == 4  # noqa: SLF001
        assert model._client.max_retries == 0  # noqa: SLF001
        assert model._client.timeout == 12.0  # noqa: SLF001
        get_chat_model.cache_clear()

    def test_defaults(self) -> None:
        settings = Settings()

        assert Settings.model_fields["openai_chat_model"].default == "gpt-6-luna"
        assert Settings.model_fields["openai_chat_fallback_model"].default == "gpt-5.4-mini"
        assert Settings.model_fields["chat_reasoning_effort"].default == "low"
        assert (settings.chat_top_k, settings.chat_min_similarity) == (5, 0.2)
        assert (
            settings.chat_generation_lease_seconds
            > settings.chat_timeout_seconds * settings.chat_max_attempts
        )
