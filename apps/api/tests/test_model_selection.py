"""Phase 5.7: choosing the chat model from configuration alone, the parameters each model
accepts, and the automatic fallback (rollback) model. Nothing here reaches OpenAI."""

import json

import pytest
from openai import AsyncOpenAI

import app.services.llm as llm_module
from app.core.config import (
    CHAT_MODEL_CAPABILITIES,
    DEFAULT_PRICING,
    Settings,
    chat_model_capabilities,
)
from app.services.llm import Generation, LLMError, OpenAIResponsesModel, get_chat_model
from tests.test_llm import API_KEY, Api, generate
from tests.test_llm_stream import Upstream, collect, completed, pieces


def body(api: Api | Upstream, index: int = 0) -> dict:
    return json.loads(api.requests[index].content)


class TestCapabilities:
    @pytest.mark.parametrize(
        ("model", "temperature", "effort"),
        [
            ("gpt-4.1-mini", True, False),
            ("gpt-4.1-mini-2025-04-14", True, False),
            ("gpt-5.4-mini", True, True),
            ("gpt-5.6-luna", False, True),
            ("gpt-6-luna", False, True),
            ("gpt-6-luna-2026-10-01", False, True),
        ],
    )
    def test_known_models_and_their_snapshots(
        self, model: str, temperature: bool, effort: bool
    ) -> None:
        accepts = chat_model_capabilities(model)
        assert (accepts["temperature"], accepts["reasoning_effort"]) == (temperature, effort)

    def test_an_unknown_model_is_sent_neither_optional_parameter(self) -> None:
        assert chat_model_capabilities("some-future-model") == {}

    def test_every_evaluated_model_has_a_price(self) -> None:
        for model in CHAT_MODEL_CAPABILITIES:
            assert model in DEFAULT_PRICING["models"], model


class TestRequestParameters:
    async def test_temperature_is_left_out_when_the_model_rejects_it(self) -> None:
        api = Api()
        model, _ = api.model(model="gpt-6-luna", temperature=None, reasoning_effort="low")

        await generate(model)

        sent = body(api)
        assert "temperature" not in sent
        assert sent["reasoning"] == {"effort": "low"}
        assert sent["store"] is False

    async def test_reasoning_is_left_out_unless_configured(self) -> None:
        api = Api()
        model, _ = api.model()

        await generate(model)

        assert "reasoning" not in body(api)
        assert body(api)["temperature"] == 0.2

    async def test_streaming_requests_follow_the_same_rules(self) -> None:
        upstream = Upstream([*pieces(), completed()])
        model, _ = upstream.model(model="gpt-6-luna", temperature=None, reasoning_effort="none")

        await collect(model)

        sent = body(upstream)
        assert "temperature" not in sent and sent["reasoning"] == {"effort": "none"}


class TestFallback:
    def fallback(self, client: AsyncOpenAI) -> OpenAIResponsesModel:
        return OpenAIResponsesModel(
            client, model="gpt-4.1-mini", temperature=0.2, max_output_tokens=1000
        )

    async def test_a_model_that_is_not_found_falls_back_once(self) -> None:
        api = Api()
        api.script = [404]
        primary, _ = api.model(model="gpt-6-luna", temperature=None)
        primary._fallback = self.fallback(primary._client)  # noqa: SLF001

        result = await generate(primary)

        assert result.text
        assert [body(api, i)["model"] for i in range(2)] == ["gpt-6-luna", "gpt-4.1-mini"]
        assert "temperature" not in body(api, 0) and body(api, 1)["temperature"] == 0.2

    @pytest.mark.parametrize("status", [401, 403, 400, 429])
    async def test_other_failures_do_not_fall_back(self, status: int) -> None:
        api = Api()
        api.script = [status] * 5
        primary, _ = api.model(model="gpt-6-luna", temperature=None, max_attempts=1)
        primary._fallback = self.fallback(primary._client)  # noqa: SLF001

        with pytest.raises(LLMError):
            await generate(primary)

        assert {body(api, i)["model"] for i in range(len(api.requests))} == {"gpt-6-luna"}

    async def test_without_a_fallback_not_found_is_a_configuration_error(self) -> None:
        api = Api()
        api.script = [404]
        primary, _ = api.model(model="gpt-6-luna", temperature=None)

        with pytest.raises(LLMError) as caught:
            await generate(primary)

        assert caught.value.code == "misconfigured"
        assert len(api.requests) == 1

    async def test_a_stream_falls_back_before_any_output(self) -> None:
        upstream = Upstream(404, [*pieces(), completed()])
        primary, _ = upstream.model(model="gpt-6-luna", temperature=None)
        primary._fallback = self.fallback(primary._client)  # noqa: SLF001

        items = await collect(primary)

        assert isinstance(items[-1], Generation)
        assert [body(upstream, i)["model"] for i in range(2)] == ["gpt-6-luna", "gpt-4.1-mini"]


class TestDefaults:
    def test_the_evaluated_default_and_its_rollback(self) -> None:
        assert Settings.model_fields["openai_chat_model"].default == "gpt-6-luna"
        assert Settings.model_fields["chat_reasoning_effort"].default == "low"
        assert Settings.model_fields["openai_chat_fallback_model"].default == "gpt-5.4-mini"
        for model in ("gpt-6-luna", "gpt-5.4-mini", "gpt-4.1-mini"):
            assert chat_model_capabilities(model), model


class TestFactory:
    def configure(self, monkeypatch: pytest.MonkeyPatch, **values: object) -> OpenAIResponsesModel:
        monkeypatch.setattr(
            llm_module, "get_settings", lambda: Settings(openai_api_key=API_KEY, **values)
        )
        get_chat_model.cache_clear()
        model = get_chat_model()
        get_chat_model.cache_clear()
        return model

    def test_one_setting_switches_the_model_and_its_parameters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self.configure(
            monkeypatch, openai_chat_model="gpt-6-luna", chat_reasoning_effort="low"
        )

        assert model._model == "gpt-6-luna"  # noqa: SLF001
        assert model._temperature is None  # noqa: SLF001
        assert model._reasoning_effort == "low"  # noqa: SLF001

    def test_effort_is_not_sent_to_a_model_that_rejects_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = self.configure(
            monkeypatch, openai_chat_model="gpt-4.1-mini", chat_reasoning_effort="low"
        )

        assert model._reasoning_effort is None  # noqa: SLF001
        assert model._temperature == 0.2  # noqa: SLF001

    def test_the_fallback_gets_its_own_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = self.configure(
            monkeypatch,
            openai_chat_model="gpt-6-luna",
            openai_chat_fallback_model="gpt-4.1-mini",
            chat_reasoning_effort="none",
        )

        fallback = model._fallback  # noqa: SLF001
        assert fallback is not None
        assert fallback._model == "gpt-4.1-mini"  # noqa: SLF001
        assert (fallback._temperature, fallback._reasoning_effort) == (0.2, None)  # noqa: SLF001
        assert fallback._fallback is None  # noqa: SLF001

    def test_no_fallback_when_unset_or_to_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        unset = self.configure(monkeypatch, openai_chat_fallback_model="")
        assert unset._fallback is None  # noqa: SLF001
        same = self.configure(
            monkeypatch, openai_chat_model="gpt-6-luna", openai_chat_fallback_model="gpt-6-luna"
        )
        assert same._fallback is None  # noqa: SLF001

    @pytest.mark.parametrize("value", ["", "  ", None])
    def test_a_blank_effort_means_the_model_default(self, value: object) -> None:
        assert Settings(chat_reasoning_effort=value).chat_reasoning_effort is None

    def test_an_invalid_effort_is_refused(self) -> None:
        with pytest.raises(ValueError, match="CHAT_REASONING_EFFORT"):
            Settings(chat_reasoning_effort="extreme")
