"""The embedding service, exercised through the real OpenAI SDK against a mocked HTTP API.

Nothing here reaches OpenAI: requests are answered by a scripted transport. That still runs
the SDK's real request building, response parsing and error classes.
"""

import json
import logging

import httpx
import pytest
from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.services import embeddings as embeddings_module
from app.services.embeddings import EmbeddingError, OpenAIEmbedder, get_embedder

API_KEY = "sk-test-key-never-real-0123456789"
DIMENSIONS = 1536


def vector_for(position: int, dimensions: int = DIMENSIONS) -> list[float]:
    """A recognisable vector: its first value identifies which input it belongs to."""
    return [float(position)] + [0.0] * (dimensions - 1)


class Api:
    """A scripted OpenAI embeddings endpoint that records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.inputs: list[list[str]] = []
        # Responses to give before behaving normally: status codes or exceptions.
        self.script: list[int | Exception] = []
        self.headers: dict[str, str] = {}
        self.mutate = None  # optional hook to corrupt a successful body
        self.seen: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.inputs.append(body["input"])
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            if step != 200:
                return httpx.Response(
                    step,
                    headers=self.headers,
                    json={"error": {"message": "scripted failure", "type": "test", "code": None}},
                )
        data = []
        for index, text in enumerate(body["input"]):
            # The same text always gets the same vector, whichever request carries it.
            identity = self.seen.setdefault(text, len(self.seen))
            data.append({"object": "embedding", "index": index, "embedding": vector_for(identity)})
        payload = {
            "object": "list",
            "data": data,
            "model": body["model"],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        if self.mutate:
            self.mutate(payload)
        return httpx.Response(200, json=payload)

    def embedder(self, **options: object) -> tuple[OpenAIEmbedder, list[float]]:
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client = AsyncOpenAI(
            api_key=API_KEY,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )
        defaults: dict[str, object] = {"model": "text-embedding-3-small", "sleep": sleep}
        return OpenAIEmbedder(client, **{**defaults, **options}), sleeps  # type: ignore[arg-type]


class TestSuccess:
    async def test_returns_one_vector_per_input_in_order(self) -> None:
        api = Api()
        embedder, _ = api.embedder()

        vectors = await embedder.embed(["alpha", "beta", "gamma"])

        assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]
        assert all(len(vector) == DIMENSIONS for vector in vectors)
        assert all(isinstance(value, float) for value in vectors[0])

    async def test_sends_the_configured_model_and_dimensions_with_the_key(self) -> None:
        api = Api()
        embedder, _ = api.embedder()

        await embedder.embed(["hello"])

        (request,) = api.requests
        body = json.loads(request.content)
        assert request.url.path.endswith("/embeddings")
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        assert body == {
            "model": "text-embedding-3-small",
            "input": ["hello"],
            "dimensions": 1536,
            "encoding_format": "float",
        }

    async def test_the_model_is_configurable(self) -> None:
        api = Api()
        embedder, _ = api.embedder(model="text-embedding-3-large")

        await embedder.embed(["hello"])

        assert json.loads(api.requests[0].content)["model"] == "text-embedding-3-large"

    async def test_no_request_for_no_input(self) -> None:
        api = Api()
        embedder, _ = api.embedder()

        assert await embedder.embed([]) == []
        assert api.requests == []

    async def test_a_response_in_a_different_order_is_put_back_in_input_order(self) -> None:
        api = Api()
        api.mutate = lambda payload: payload["data"].reverse()
        embedder, _ = api.embedder()

        vectors = await embedder.embed(["alpha", "beta", "gamma"])

        assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0]


class TestBatching:
    async def test_large_inputs_are_sent_in_batches_and_reassembled_in_order(self) -> None:
        api = Api()
        embedder, _ = api.embedder(batch_size=64)
        texts = [f"chunk {n}" for n in range(150)]

        vectors = await embedder.embed(texts)

        assert [len(batch) for batch in api.inputs] == [64, 64, 22]
        assert [vector[0] for vector in vectors] == [float(n) for n in range(150)]

    async def test_batch_size_is_configurable(self) -> None:
        api = Api()
        embedder, _ = api.embedder(batch_size=10)

        await embedder.embed([f"chunk {n}" for n in range(25)])

        assert [len(batch) for batch in api.inputs] == [10, 10, 5]

    async def test_identical_texts_are_embedded_once(self) -> None:
        api = Api()
        embedder, _ = api.embedder()

        vectors = await embedder.embed(["same", "other", "same", "same"])

        assert api.inputs == [["same", "other"]]
        assert vectors[0] == vectors[2] == vectors[3]
        assert vectors[0] != vectors[1]

    @pytest.mark.parametrize("size", [0, 257])
    def test_an_unsafe_batch_size_is_refused(self, size: int) -> None:
        with pytest.raises(ValueError, match="batch size"):
            Api().embedder(batch_size=size)


class TestRetries:
    async def test_rate_limiting_is_retried_with_growing_delays_then_succeeds(self) -> None:
        api = Api()
        api.script = [429, 429, 429]
        embedder, sleeps = api.embedder(base_delay_seconds=1.0)

        vectors = await embedder.embed(["hello"])

        assert len(vectors) == 1
        assert len(api.requests) == 4
        # Exponential backoff with jitter: each wait falls in a band that doubles.
        assert len(sleeps) == 3
        assert 0.5 <= sleeps[0] <= 1.0
        assert 1.0 <= sleeps[1] <= 2.0
        assert 2.0 <= sleeps[2] <= 4.0

    async def test_a_retry_after_header_is_respected(self) -> None:
        api = Api()
        api.script = [429]
        api.headers = {"retry-after": "7"}
        embedder, sleeps = api.embedder(base_delay_seconds=0.1)

        await embedder.embed(["hello"])

        assert sleeps == [7.0]

    async def test_an_absurd_retry_after_is_ignored(self) -> None:
        api = Api()
        api.script = [429]
        api.headers = {"retry-after": "86400"}
        embedder, sleeps = api.embedder(base_delay_seconds=1.0)

        await embedder.embed(["hello"])

        assert sleeps[0] <= 1.0

    async def test_retries_are_bounded(self) -> None:
        api = Api()
        api.script = [429] * 50
        embedder, sleeps = api.embedder(max_attempts=4)

        with pytest.raises(EmbeddingError) as caught:
            await embedder.embed(["hello"])

        assert caught.value.retryable is True
        assert len(api.requests) == 4  # exactly max_attempts, then it gives up
        assert len(sleeps) == 3

    async def test_delays_are_capped(self) -> None:
        api = Api()
        api.script = [500] * 11
        embedder, sleeps = api.embedder(max_attempts=12, base_delay_seconds=1.0)

        await embedder.embed(["hello"])

        assert max(sleeps) <= 30.0

    @pytest.mark.parametrize(
        "failure",
        [500, 502, 503, httpx.ReadTimeout("timed out"), httpx.ConnectError("refused")],
        ids=["500", "502", "503", "timeout", "connection error"],
    )
    async def test_transient_failures_are_retried(self, failure: int | Exception) -> None:
        api = Api()
        api.script = [failure]
        embedder, sleeps = api.embedder()

        vectors = await embedder.embed(["hello"])

        assert len(vectors) == 1
        assert len(api.requests) == 2
        assert len(sleeps) == 1

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_a_rejected_request_is_not_retried(self, status: int) -> None:
        api = Api()
        api.script = [status] * 5
        embedder, sleeps = api.embedder()

        with pytest.raises(EmbeddingError) as caught:
            await embedder.embed(["hello"])

        assert caught.value.retryable is False
        assert len(api.requests) == 1
        assert sleeps == []

    async def test_a_failure_in_a_later_batch_fails_the_whole_call(self) -> None:
        api = Api()
        api.script = [200, 401]
        embedder, _ = api.embedder(batch_size=2)

        with pytest.raises(EmbeddingError):
            await embedder.embed(["a", "b", "c", "d"])


class TestInvalidResponses:
    async def run(self, mutate) -> EmbeddingError:  # noqa: ANN001
        api = Api()
        api.mutate = mutate
        embedder, _ = api.embedder()
        with pytest.raises(EmbeddingError) as caught:
            await embedder.embed(["alpha", "beta"])
        assert caught.value.retryable is False
        assert len(api.requests) == 1
        return caught.value

    async def test_wrong_vector_length(self) -> None:
        def shorten(payload: dict) -> None:
            payload["data"][1]["embedding"] = [0.1] * 512

        error = await self.run(shorten)
        assert "invalid response" in str(error)

    async def test_fewer_vectors_than_inputs(self) -> None:
        await self.run(lambda payload: payload["data"].pop())

    async def test_more_vectors_than_inputs(self) -> None:
        await self.run(lambda payload: payload["data"].append(dict(payload["data"][0], index=2)))

    async def test_repeated_positions(self) -> None:
        def repeat(payload: dict) -> None:
            payload["data"][1]["index"] = 0

        await self.run(repeat)

    async def test_non_numeric_values(self) -> None:
        def corrupt(payload: dict) -> None:
            payload["data"][0]["embedding"] = ["not-a-number"] * DIMENSIONS

        await self.run(corrupt)

    async def test_empty_vector(self) -> None:
        def empty(payload: dict) -> None:
            payload["data"][0]["embedding"] = []

        await self.run(empty)

    async def test_no_data_at_all(self) -> None:
        await self.run(lambda payload: payload.update(data=[]))

    async def test_blank_input_is_refused_before_any_request(self) -> None:
        api = Api()
        embedder, _ = api.embedder()

        with pytest.raises(EmbeddingError):
            await embedder.embed(["real text", "   "])

        assert api.requests == []


class TestSecrecy:
    async def test_neither_the_text_nor_the_key_is_logged_on_success_or_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        secret_text = "Confidential salary figures for 2026"
        api = Api()
        api.script = [429, 500, 200, 401]
        embedder, _ = api.embedder(batch_size=1)

        with caplog.at_level(logging.DEBUG):
            await embedder.embed([secret_text])
            with pytest.raises(EmbeddingError) as caught:
                await embedder.embed(["another " + secret_text])

        assert "retrying" in caplog.text  # failures are logged...
        assert secret_text not in caplog.text  # ...without the content
        assert API_KEY not in caplog.text
        assert secret_text not in str(caught.value)
        assert API_KEY not in str(caught.value)


class TestConfiguration:
    def configure(self, monkeypatch: pytest.MonkeyPatch, **values: object) -> None:
        monkeypatch.setattr(embeddings_module, "get_settings", lambda: Settings(**values))
        get_embedder.cache_clear()

    def test_missing_key_is_reported_as_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(monkeypatch, openai_api_key="")

        with pytest.raises(AppError) as caught:
            get_embedder()

        assert (caught.value.status_code, caught.value.code) == (503, "embeddings_not_configured")
        get_embedder.cache_clear()

    def test_a_dimension_setting_that_does_not_match_the_database_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(monkeypatch, openai_api_key=API_KEY, embedding_dimensions=3072)

        with pytest.raises(AppError) as caught:
            get_embedder()

        assert caught.value.code == "embeddings_misconfigured"
        get_embedder.cache_clear()

    def test_defaults_match_the_specification(self) -> None:
        settings = Settings(openai_api_key=API_KEY)

        assert settings.embedding_model == "text-embedding-3-small"
        assert settings.embedding_dimensions == 1536
        assert (settings.chunk_size_tokens, settings.chunk_overlap_tokens) == (800, 100)
        assert API_KEY not in repr(settings)

    def test_a_configured_embedder_is_built_with_sdk_retries_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.configure(monkeypatch, openai_api_key=API_KEY)

        embedder = get_embedder()

        assert isinstance(embedder, OpenAIEmbedder)
        # Retries happen in one place only, where they are bounded and logged.
        assert embedder._client.max_retries == 0  # noqa: SLF001
        get_embedder.cache_clear()
