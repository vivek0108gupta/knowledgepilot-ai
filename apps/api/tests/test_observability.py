"""Phase 5.7: operational metrics and structured logs. Metrics are for operators only, carry no
identifiers or content, and are off unless a token is configured."""

# `library` is a fixture shared with test_chat; tests take it as an argument.
# ruff: noqa: F811

import json
import logging
import re
import uuid

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

import app.api.metrics as metrics_module
from app.core.config import Settings
from app.core.logging import JsonFormatter, RequestIdFilter
from app.core.metrics import Registry, metrics
from tests.auth import auth
from tests.test_chat import ask, library  # noqa: F401
from tests.test_conversations import API

TOKEN = "metrics-test-token-0123456789"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        metrics_module, "get_settings", lambda: Settings(metrics_token=SecretStr(TOKEN))
    )
    return TOKEN


@pytest.fixture(autouse=True)
def fresh_metrics() -> None:
    metrics.reset()


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


class TestAccess:
    async def test_off_unless_a_token_is_configured(self, client: AsyncClient) -> None:
        response = await client.get("/metrics", headers=bearer("anything"))
        assert response.status_code == 404

    async def test_a_missing_or_wrong_token_is_refused(
        self, client: AsyncClient, token: str
    ) -> None:
        assert (await client.get("/metrics")).status_code == 401
        assert (await client.get("/metrics", headers=bearer("wrong"))).status_code == 401

    async def test_a_users_session_does_not_give_access(
        self, client: AsyncClient, token: str
    ) -> None:
        response = await client.get("/metrics", headers=auth(uuid.uuid4()))
        assert response.status_code == 401

    async def test_not_published_in_the_api_schema(self) -> None:
        from app.main import app

        assert "/metrics" not in app.openapi()["paths"]


class TestContent:
    async def test_requests_answers_and_usage_are_counted_without_identifiers(
        self, db_client: AsyncClient, library: dict, token: str
    ) -> None:
        response = await ask(
            db_client, library["conversation"], library["owner"], "What is the refund policy?"
        )
        assert response.status_code == 201
        refused = await db_client.get(
            f"{API}/workspaces/{uuid.uuid4()}", headers=auth(uuid.uuid4())
        )
        assert refused.status_code in (403, 404)

        scraped = await db_client.get("/metrics", headers=bearer(token))

        assert scraped.status_code == 200
        assert scraped.headers["content-type"].startswith("text/plain")
        body = scraped.text
        assert 'route="/conversations/{conversation_id}/chat"' in body
        assert 'http_requests_total{method="POST",route="/conversations/' in body
        assert "http_request_duration_seconds_bucket" in body
        assert 'usage_operations_total{operation="chat"' in body
        assert "usage_tokens_total" in body
        assert "app_errors_total" in body
        assert 'ingestion_jobs{status="succeeded"}' in body or "ingestion_jobs" in body
        assert "database_connections" in body and "metrics_database_up 1" in body
        # No workspace, user, conversation or document ID, no question, no token.
        assert not UUID.search(body)
        for secret in (token, "refund policy", library["workspace"]):
            assert secret not in body

    async def test_quota_refusals_are_counted_by_code(self) -> None:
        from app.core.errors import AppError, handle_app_error

        await handle_app_error(None, AppError(429, "workspace_quota_exceeded", "x"))  # type: ignore[arg-type]

        assert metrics.value("app_errors_total", code="workspace_quota_exceeded", status=429) == 1


class TestRegistry:
    def test_histograms_are_cumulative_and_render_in_prometheus_format(self) -> None:
        registry = Registry()
        registry.observe("llm_duration_seconds", 0.3, model="m", mode="stream")
        registry.observe("llm_duration_seconds", 3.0, model="m", mode="stream")
        registry.inc("llm_requests_total", model="m", mode="stream", outcome="ok")

        text = registry.render({})

        assert 'llm_duration_seconds_bucket{mode="stream",model="m",le="0.5"} 1' in text
        assert 'llm_duration_seconds_bucket{mode="stream",model="m",le="5"} 2' in text
        assert 'llm_duration_seconds_bucket{mode="stream",model="m",le="+Inf"} 2' in text
        assert 'llm_duration_seconds_count{mode="stream",model="m"} 2' in text
        assert 'llm_requests_total{mode="stream",model="m",outcome="ok"} 1' in text
        assert "# TYPE llm_duration_seconds histogram" in text

    def test_label_values_cannot_break_the_format(self) -> None:
        registry = Registry()
        registry.inc("app_errors_total", code='bad"}\nx', status=400)

        line = next(x for x in registry.render({}).splitlines() if x.startswith("app_errors_total"))
        # Quotes and newlines are replaced; braces inside a quoted value are harmless.
        assert line == 'app_errors_total{code="bad_}_x",status="400"} 1'

    def test_unknown_metrics_are_a_programming_error(self) -> None:
        with pytest.raises(KeyError):
            Registry().inc("not_a_metric")


class TestModelMetrics:
    async def test_calls_retries_and_fallbacks_are_counted(self) -> None:
        from tests.test_llm import Api, generate
        from tests.test_llm_stream import Upstream, collect, completed, pieces

        api = Api()
        api.script = [429]
        model, _ = api.model(model="gpt-4.1-mini")
        await generate(model)

        upstream = Upstream([*pieces(), completed()])
        streaming, _ = upstream.model(model="gpt-4.1-mini")
        await collect(streaming)

        assert (
            metrics.value("llm_requests_total", model="gpt-4.1-mini", mode="generate", outcome="ok")
            == 1
        )
        assert (
            metrics.value("llm_requests_total", model="gpt-4.1-mini", mode="stream", outcome="ok")
            == 1
        )
        assert metrics.value("llm_retries_total", model="gpt-4.1-mini", error="RateLimitError") == 1
        assert metrics.value("llm_first_token_seconds", model="gpt-4.1-mini") == 1

    async def test_a_stream_the_reader_abandons_is_counted_as_cancelled(self) -> None:
        from tests.test_llm_stream import Upstream, completed, pieces, stream

        upstream = Upstream([*pieces(), completed()])
        model, _ = upstream.model(model="gpt-4.1-mini")
        events = stream(model)
        await anext(events)
        await events.aclose()

        assert (
            metrics.value(
                "llm_requests_total", model="gpt-4.1-mini", mode="stream", outcome="cancelled"
            )
            == 1
        )


class TestJsonLogs:
    def test_one_json_object_per_line_with_the_request_id(self) -> None:
        record = logging.LogRecord(
            "app.x", logging.ERROR, __file__, 1, "Job %s failed", ("j1",), None
        )
        RequestIdFilter().filter(record)

        entry = json.loads(JsonFormatter().format(record))

        assert entry["level"] == "ERROR" and entry["logger"] == "app.x"
        assert entry["message"] == "Job j1 failed" and entry["request_id"] == "-"
        assert entry["time"].endswith("+00:00")

    def test_exceptions_are_kept(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = logging.LogRecord("a", logging.ERROR, __file__, 1, "x", (), sys.exc_info())
        assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exception"]

    def test_the_format_is_a_setting(self) -> None:
        assert Settings.model_fields["log_format"].default == "text"
        assert Settings(log_format="json").log_format == "json"


class TestDatabasePool:
    def test_the_pool_is_a_setting_with_safe_defaults(self) -> None:
        from app.core.db import engine

        assert (
            Settings.model_fields["db_pool_size"].default,
            Settings.model_fields["db_max_overflow"].default,
        ) == (5, 10)
        assert Settings(db_pool_size=20, db_max_overflow=0).db_pool_size == 20
        with pytest.raises(ValueError):
            Settings(db_pool_size=0)
        assert engine.pool.size() == Settings().db_pool_size
