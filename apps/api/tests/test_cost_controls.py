"""Security and cost controls, end to end against real PostgreSQL: rate limits, usage
metering, reservations and quotas, and the usage APIs. The provider is a fake.

The suite's environment sets generous limits (conftest.py); each test here sets the limits it
needs on the live settings object.
"""

# `library` is a fixture shared with test_chat; tests take it as an argument.
# ruff: noqa: F811

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core import db as app_db
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.usage import UsageOperation
from app.services import chat as chat_service
from app.services import chat_stream, pricing, rate_limits, usage, usage_reports
from app.services.embeddings import EmbeddingError
from app.services.llm import LLMError
from tests.auth import auth
from tests.fakes import FakeChatModel, FakeEmbedder, sources_in
from tests.test_chat import ask, library, rows  # noqa: F401
from tests.test_chat_stream import events as sse_events
from tests.test_chat_stream import long_answer, stream
from tests.test_conversations import API, create_conversation
from tests.test_search import add_text
from tests.test_workspaces import add_member, create_workspace

TEST_PRICING = pricing.parse_pricing(
    {
        "version": "test-2026-01",
        "currency": "USD",
        "models": {
            "fake-model": {"input_per_million": "1.00", "output_per_million": "2.00"},
            "gpt-4.1-mini": {"input_per_million": "1.00", "output_per_million": "2.00"},
            "text-embedding-3-small": {"input_per_million": "0.10", "output_per_million": "0"},
        },
    }
)


@pytest.fixture(autouse=True)
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prices for the fake models, so costs can be checked exactly."""
    for module in (usage, usage_reports):
        monkeypatch.setattr(module, "get_pricing", lambda: TEST_PRICING)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Set limits on the live settings object for one test."""
    settings = get_settings()

    def apply(**values: object) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return apply


async def usage_rows(engine: AsyncEngine, where: str = "true", **params: object) -> list[dict]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(f"SELECT * FROM usage_events WHERE {where} ORDER BY created_at, id"), params
        )
        return [dict(row._mapping) for row in result]


async def generation_rows(engine: AsyncEngine) -> list[dict]:
    return await usage_rows(engine, "operation IN ('chat', 'chat_stream')")


async def message_count(engine: AsyncEngine) -> int:
    return (await rows(engine, "SELECT count(*) FROM messages"))[0][0]


# --- Configuration and pricing ------------------------------------------------------------


class TestConfiguration:
    def test_defaults_are_the_documented_policies(self) -> None:
        fields = Settings.model_fields
        assert fields["rate_limit_chat_per_minute"].default == 10
        assert fields["max_concurrent_generations_per_user"].default == 3
        assert fields["rate_limit_search_per_minute"].default == 30
        assert fields["rate_limit_upload_per_hour"].default == 10
        assert fields["rate_limits_enabled"].default is True

    @pytest.mark.parametrize(
        "values",
        [
            {"rate_limit_chat_per_minute": 0},
            {"user_daily_token_limit": -1},
            {"workspace_monthly_cost_limit": "-1"},
            {"max_concurrent_generations_per_user": 0},
            {"usage_reservation_ttl_seconds": 120},  # not longer than the 300 s lease
            {"chat_max_output_tokens": 0},
        ],
    )
    def test_invalid_limits_are_refused_at_startup(self, values: dict) -> None:
        with pytest.raises(ValidationError):
            Settings(**values)

    def test_costs_are_computed_from_the_versioned_table(self) -> None:
        assert TEST_PRICING.cost("fake-model-1", 1_000_000, 500_000) == Decimal("2.00000000")
        # A dated snapshot uses its family's price; an unknown model has none.
        assert TEST_PRICING.cost("gpt-4.1-mini-2025-04-14", 1000, 0) == Decimal("0.00100000")
        assert TEST_PRICING.cost("unknown-model", 1000, 1000) is None

    def test_the_default_table_is_valid(self) -> None:
        table = pricing.parse_pricing(
            json.loads(json.dumps(__import__("app.core.config").core.config.DEFAULT_PRICING))
        )
        assert table.currency == "USD" and table.version

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {"version": "", "currency": "USD", "models": {"m": {"input_per_million": "1"}}},
            {"version": "v", "currency": "usd", "models": {"m": {"input_per_million": "1"}}},
            {"version": "v", "currency": "USD", "models": {}},
            {"version": "v", "currency": "USD", "models": {"m": {"input_per_million": "-1"}}},
            {"version": "v", "currency": "USD", "models": {"m": {"input_per_million": "abc"}}},
            {
                "version": "v",
                "currency": "USD",
                "models": {"bad name!": {"input_per_million": "1"}},
            },
        ],
        ids=[
            "not an object",
            "no version",
            "currency",
            "no models",
            "negative",
            "not a number",
            "model name",
        ],
    )
    def test_malformed_pricing_is_refused(self, data: object) -> None:
        with pytest.raises(pricing.PricingError):
            pricing.parse_pricing(data)

    def test_sql_parameters_are_kept_out_of_logged_errors(self) -> None:
        assert app_db.engine.sync_engine.hide_parameters is True


# --- Rate limits --------------------------------------------------------------------------


class TestRateLimiter:
    POLICY = rate_limits.Policy("test:user", 3, 60)

    async def hit(
        self, factory: async_sessionmaker, subject: uuid.UUID, now: datetime
    ) -> AppError | None:
        async with factory() as session:
            try:
                await rate_limits.hit(session, self.POLICY, subject, now=now)
            except AppError as error:
                return error
        return None

    async def test_requests_within_the_limit_pass_and_the_next_is_refused(
        self, db_engine: AsyncEngine, session_factory: async_sessionmaker
    ) -> None:
        user, now = uuid.uuid4(), datetime(2026, 11, 12, 10, 0, 5, tzinfo=UTC)

        results = [await self.hit(session_factory, user, now) for _ in range(4)]

        assert results[:3] == [None, None, None]
        refused = results[3]
        assert refused is not None
        assert (refused.status_code, refused.code) == (429, "rate_limited")
        assert refused.retry_after_seconds == 55
        assert refused.headers["Retry-After"] == "55"
        # The refused request was not counted.
        assert (await rows(db_engine, "SELECT count FROM rate_limit_buckets"))[0][0] == 3

    async def test_the_window_slides_and_resets(
        self, db_engine: AsyncEngine, session_factory: async_sessionmaker
    ) -> None:
        user = uuid.uuid4()
        start = datetime(2026, 11, 12, 10, 0, 50, tzinfo=UTC)
        for _ in range(3):
            assert await self.hit(session_factory, user, start) is None

        # 5 s into the next window, 11/12 of the previous one still counts: 2.75 + 1 > 3.
        assert await self.hit(session_factory, user, start + timedelta(seconds=15)) is not None
        # 30 s in, half of it counts: 1.5 + 1 is within the limit again.
        assert await self.hit(session_factory, user, start + timedelta(seconds=40)) is None
        # Two windows later, the earlier requests no longer count at all.
        assert await self.hit(session_factory, user, start + timedelta(seconds=130)) is None
        # Old windows are removed as new ones are used.
        assert (await rows(db_engine, "SELECT count(*) FROM rate_limit_buckets"))[0][0] <= 2

    async def test_subjects_and_policies_are_counted_separately(
        self, session_factory: async_sessionmaker
    ) -> None:
        now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
        alice, bob = uuid.uuid4(), uuid.uuid4()
        for _ in range(3):
            assert await self.hit(session_factory, alice, now) is None

        assert await self.hit(session_factory, alice, now) is not None
        assert await self.hit(session_factory, bob, now) is None
        async with session_factory() as session:
            await rate_limits.hit(session, rate_limits.Policy("other:user", 3, 60), alice, now=now)

    async def test_concurrent_requests_cannot_exceed_the_limit(
        self, session_factory: async_sessionmaker
    ) -> None:
        user, now = uuid.uuid4(), datetime(2026, 11, 12, 10, 0, 1, tzinfo=UTC)

        results = await asyncio.gather(*(self.hit(session_factory, user, now) for _ in range(20)))

        assert sum(result is None for result in results) == 3
        assert all(r is None or r.code == "rate_limited" for r in results)

    async def test_limits_can_be_switched_off(
        self, session_factory: async_sessionmaker, limits
    ) -> None:  # noqa: ANN001
        limits(rate_limits_enabled=False)
        user, now = uuid.uuid4(), datetime(2026, 11, 12, 10, 0, 1, tzinfo=UTC)

        assert all([await self.hit(session_factory, user, now) is None for _ in range(10)])

    async def test_a_limit_that_cannot_be_checked_refuses_the_request(
        self, session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy.exc import OperationalError

        async with session_factory() as session:

            async def broken(*_args: object, **_kwargs: object) -> None:
                raise OperationalError("SELECT", {}, Exception("down"))

            monkeypatch.setattr(session, "scalar", broken)
            with pytest.raises(AppError) as raised:
                await rate_limits.hit(session, self.POLICY, uuid.uuid4())

        assert (raised.value.status_code, raised.value.code) == (503, "usage_unavailable")


class TestRateLimitedEndpoints:
    async def test_chat_and_streaming_share_the_per_user_limit(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(rate_limit_chat_per_minute=2)

        first = await ask(db_client, library["conversation"], library["owner"], "refund policy")
        second = await stream(db_client, library["conversation"], library["owner"], "shipping")
        third = await stream(db_client, library["conversation"], library["owner"], "holidays")

        assert (first.status_code, second.status_code) == (201, 200)
        assert third.status_code == 429
        error = third.json()["error"]
        assert error["code"] == "rate_limited"
        assert error["retry_after_seconds"] >= 1
        assert third.headers["retry-after"] == str(error["retry_after_seconds"])
        assert await message_count(db_engine) == 4  # nothing stored for the refused one

    async def test_the_per_workspace_chat_limit_covers_every_member(
        self,
        db_client: AsyncClient,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(rate_limit_chat_per_workspace_per_minute=1)
        theirs = (await create_conversation(db_client, library["workspace"], library["member"]))[
            "id"
        ]

        mine = await ask(db_client, library["conversation"], library["owner"], "refund policy")
        other = await ask(db_client, theirs, library["member"], "refund policy")

        assert mine.status_code == 201
        assert (other.status_code, other.json()["error"]["code"]) == (429, "rate_limited")
        assert "this workspace" in other.json()["error"]["message"]

    async def test_another_users_conversation_is_not_counted_against_its_workspace(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(rate_limit_chat_per_workspace_per_minute=1)
        outsider = uuid.uuid4()

        for _ in range(3):
            response = await ask(db_client, library["conversation"], outsider, "refund policy")
            assert response.status_code == 404
        allowed = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert allowed.status_code == 201

    async def test_search_is_rate_limited(
        self, db_client: AsyncClient, library: dict, limits
    ) -> None:  # noqa: ANN001
        limits(rate_limit_search_per_minute=1)
        url = f"{API}/workspaces/{library['workspace']}/search"

        first = await db_client.post(url, json={"query": "refund"}, headers=auth(library["owner"]))
        second = await db_client.post(url, json={"query": "refund"}, headers=auth(library["owner"]))

        assert first.status_code == 200
        assert (second.status_code, second.json()["error"]["code"]) == (429, "rate_limited")

    async def test_uploads_are_rate_limited(
        self,
        db_client: AsyncClient,
        process,
        limits,  # noqa: ANN001
    ) -> None:
        limits(rate_limit_upload_per_hour=1)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Uploads")
        await add_text(db_client, process, workspace, owner, "one.txt", "First document text.\n")

        refused = await db_client.post(
            f"{API}/workspaces/{workspace}/documents",
            files={"file": ("two.txt", b"Second document text.\n", "text/plain")},
            headers=auth(owner),
        )

        assert (refused.status_code, refused.json()["error"]["code"]) == (429, "rate_limited")

    async def test_documents_waiting_for_ingestion_are_capped_per_workspace(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        queue,
        limits,  # noqa: ANN001
    ) -> None:
        limits(max_ingestion_jobs_per_workspace=2)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Ingest")

        async def upload(index: int):  # noqa: ANN202
            return await db_client.post(
                f"{API}/workspaces/{workspace}/documents",
                files={
                    "file": (
                        f"doc{index}.txt",
                        f"Document number {index}.\n".encode(),
                        "text/plain",
                    )
                },
                headers=auth(owner),
            )

        responses = await asyncio.gather(*(upload(index) for index in range(5)))

        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 201, 429, 429, 429]
        assert {r.json()["error"]["code"] for r in responses if r.status_code == 429} == {
            "ingestion_busy"
        }
        assert (await rows(db_engine, "SELECT count(*) FROM documents"))[0][0] == 2


# --- Metering -----------------------------------------------------------------------------


class TestChatMetering:
    async def test_an_answer_is_one_completed_record_with_the_providers_counts(
        self, db_client: AsyncClient, db_engine: AsyncEngine, library: dict
    ) -> None:
        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        (row,) = await generation_rows(db_engine)
        assert row["operation"] == "chat"
        assert (row["status"], row["usage_source"]) == ("completed", "provider")
        assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (120, 30, 150)
        assert row["charged_tokens"] == 150
        assert row["estimated_input_tokens"] is None
        assert row["model"] == "fake-model-1"
        # 120 x $1/M + 30 x $2/M
        assert row["estimated_cost"] == row["charged_cost"] == Decimal("0.00018000")
        assert (row["currency"], row["pricing_version"]) == ("USD", "test-2026-01")
        assert str(row["message_id"]) == response.json()["assistant_message"]["id"]
        assert str(row["workspace_id"]) == library["workspace"]
        assert row["user_id"] == library["owner"]
        assert row["reserved_input_tokens"] + row["reserved_output_tokens"] > 150
        assert row["request_id"]

    async def test_the_question_embedding_is_recorded_separately(
        self, db_client: AsyncClient, db_engine: AsyncEngine, library: dict
    ) -> None:
        await ask(db_client, library["conversation"], library["owner"], "refund policy")

        (row,) = await usage_rows(db_engine, "operation = 'query_embedding'")
        assert (row["status"], row["usage_source"]) == ("completed", "provider")
        assert row["input_tokens"] == 2  # "refund policy", one token per word in the fake
        assert row["model"] == "text-embedding-3-small"
        assert str(row["conversation_id"]) == library["conversation"]

    async def test_without_provider_counts_the_embedding_is_estimated(
        self, db_client: AsyncClient, db_engine: AsyncEngine, embedder: FakeEmbedder, library: dict
    ) -> None:
        embedder.report_usage = False

        await ask(db_client, library["conversation"], library["owner"], "refund policy")

        (row,) = await usage_rows(db_engine, "operation = 'query_embedding'")
        assert row["usage_source"] == "estimated"
        assert row["input_tokens"] is None and row["estimated_input_tokens"] > 0

    async def test_a_streamed_answer_is_one_record_not_one_per_piece(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.reply = long_answer

        parsed = sse_events(
            await stream(db_client, library["conversation"], library["owner"], "refund")
        )

        assert sum(event["event"] == "text_delta" for event in parsed) > 3
        (row,) = await generation_rows(db_engine)
        assert (row["operation"], row["status"], row["usage_source"]) == (
            "chat_stream",
            "completed",
            "provider",
        )
        assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
        assert str(row["message_id"]) == parsed[-1]["data"]["assistant_message"]["id"]

    async def test_replays_make_no_new_record(
        self, db_client: AsyncClient, db_engine: AsyncEngine, library: dict
    ) -> None:
        client_id = str(uuid.uuid4())
        await ask(
            db_client,
            library["conversation"],
            library["owner"],
            "refund",
            client_message_id=client_id,
        )
        before = await usage_rows(db_engine)

        await ask(
            db_client,
            library["conversation"],
            library["owner"],
            "refund",
            client_message_id=client_id,
        )
        await stream(
            db_client,
            library["conversation"],
            library["owner"],
            "refund",
            client_message_id=client_id,
        )

        assert await usage_rows(db_engine) == before

    async def test_the_same_question_sent_twice_at_once_is_reserved_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.delay = 0.3
        client_id = str(uuid.uuid4())

        await asyncio.gather(
            *(
                stream(
                    db_client,
                    library["conversation"],
                    library["owner"],
                    "refund",
                    client_message_id=client_id,
                )
                for _ in range(2)
            )
        )

        assert len(await generation_rows(db_engine)) == 1

    async def test_no_evidence_answers_record_no_model_usage(
        self, db_client: AsyncClient, db_engine: AsyncEngine
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Empty")
        conversation = (await create_conversation(db_client, workspace, owner))["id"]

        await ask(db_client, conversation, owner, "anything?")

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"], row["charged_tokens"]) == (
            "completed",
            "none",
            0,
        )

    async def test_missing_provider_counts_are_estimated_not_invented(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = chat_model.generate

        async def without_usage(**kwargs: object):  # noqa: ANN202
            from dataclasses import replace

            return replace(await original(**kwargs), input_tokens=None, output_tokens=None)

        monkeypatch.setattr(chat_model, "generate", without_usage)

        await ask(db_client, library["conversation"], library["owner"], "refund policy")

        (row,) = await generation_rows(db_engine)
        # Phase 5.5: a whole response arrived without counts. The prompt sent and the text
        # received are known, so they are counted here and labelled an estimate, never as
        # provider usage, and never the inflated reservation.
        assert (row["usage_source"], row["outcome"]) == ("estimated", "executed_without_usage")
        assert row["input_tokens"] is None and row["total_tokens"] is None
        assert row["estimated_input_tokens"] == row["prompt_tokens_sent"] > 0
        assert row["estimated_output_tokens"] > 0
        assert (
            row["charged_tokens"] == row["estimated_input_tokens"] + row["estimated_output_tokens"]
        )
        assert row["charged_tokens"] < row["reserved_input_tokens"] + row["reserved_output_tokens"]


class TestFailureMetering:
    @pytest.mark.parametrize(
        ("error", "source", "outcome"),
        [
            (LLMError("x", code="unavailable", retryable=True), "unavailable", "unknown"),
            (
                LLMError("x", code="unavailable", retryable=True, maybe_executed=False),
                "none",
                "rejected",
            ),
            (LLMError("x", code="misconfigured", retryable=False), "none", "rejected"),
            (LLMError("x", code="rejected", retryable=False), "none", "rejected"),
            (
                LLMError(
                    "x", code="too_long", retryable=False, input_tokens=900, output_tokens=1000
                ),
                "provider",
                "executed_with_usage",
            ),
        ],
        ids=[
            "timeout: may have run",
            "rate limited: did not run",
            "authentication",
            "rejected",
            "cut off by the output limit",
        ],
    )
    async def test_a_failed_answer_records_what_is_known(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
        error: LLMError,
        source: str,
        outcome: str,
    ) -> None:
        chat_model.fail = error

        await ask(db_client, library["conversation"], library["owner"], "refund policy")

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"], row["outcome"]) == ("failed", source, outcome)
        assert row["provider_called_at"] is not None
        assert row["error_code"].startswith("ai_")
        if source == "provider":
            assert (row["input_tokens"], row["output_tokens"], row["charged_tokens"]) == (
                900,
                1000,
                1900,
            )
        if source == "none":
            assert row["charged_tokens"] == 0
        if source == "unavailable":
            # Phase 5.5: unknown, so an upper bound is held: the prompt actually sent plus
            # the most output it could have produced. Never written as provider usage.
            assert row["input_tokens"] is None and row["estimated_input_tokens"] is None
            assert (
                row["charged_tokens"] == row["prompt_tokens_sent"] + row["reserved_output_tokens"]
            )
            assert (
                row["charged_tokens"] < row["reserved_input_tokens"] + row["reserved_output_tokens"]
            )

    async def test_a_stream_that_fails_part_way_records_an_estimate(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.reply = long_answer
        chat_model.fail, chat_model.fail_after = (
            LLMError("x", code="unavailable", retryable=True),
            5,
        )

        await stream(db_client, library["conversation"], library["owner"], "refund")

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"]) == ("failed", "estimated")
        assert row["estimated_input_tokens"] > 100
        assert 0 < row["estimated_output_tokens"] < 40  # five pieces of seven characters
        assert row["input_tokens"] is None

    async def test_an_answer_that_cannot_be_saved_still_records_its_usage(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.reply = lambda call: json.dumps(
            {"found_answer": True, "answer": "Refunds \u0000 [S1].", "citations": []}
        )

        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.json()["error"]["code"] == "answer_not_saved"
        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"], row["charged_tokens"]) == (
            "failed",
            "provider",
            150,
        )
        assert row["message_id"] is None

    async def test_a_stream_stopped_by_the_client_records_an_estimate(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        chat_model: FakeChatModel,
        embedder: FakeEmbedder,
        library: dict,
    ) -> None:
        chat_model.reply = long_answer
        session = session_factory()
        turn = await chat_service.begin(
            session,
            uuid.UUID(library["conversation"]),
            library["owner"],
            "refund policy",
            None,
            operation=UsageOperation.CHAT_STREAM,
        )
        answer = chat_stream.AnswerStream(
            session,
            embedder,
            chat_model,
            turn,
            library["owner"],
            "refund policy",
            5,
            [],
            get_settings(),
        )
        iterator = answer.events()
        while not isinstance(await anext(iterator), chat_stream.Delta):
            pass
        await iterator.aclose()
        await session.close()

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"], row["error_code"]) == (
            "cancelled",
            "estimated",
            "client_disconnected",
        )
        assert row["estimated_output_tokens"] > 0
        assert (
            await rows(db_engine, "SELECT count(*) FROM usage_events WHERE status = 'reserved'")
        )[0][0] == 0

    async def test_a_stream_stopped_before_the_model_was_called_charges_nothing(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        chat_model: FakeChatModel,
        embedder: FakeEmbedder,
        library: dict,
    ) -> None:
        session = session_factory()
        turn = await chat_service.begin(
            session, uuid.UUID(library["conversation"]), library["owner"], "refund policy", None
        )
        answer = chat_stream.AnswerStream(
            session,
            embedder,
            chat_model,
            turn,
            library["owner"],
            "refund policy",
            5,
            [],
            get_settings(),
        )
        await answer.close()
        await session.close()

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"], row["charged_tokens"]) == (
            "cancelled",
            "none",
            0,
        )


class TestLedgerRules:
    async def test_one_operation_cannot_be_reserved_twice(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, library: dict
    ) -> None:
        async def reserve():  # noqa: ANN202
            async with session_factory() as session:
                return await usage.reserve(
                    session,
                    user_id=library["owner"],
                    workspace_id=uuid.UUID(library["workspace"]),
                    operation=UsageOperation.CHAT,
                    model="fake-model",
                    input_tokens=10,
                    output_tokens=10,
                    idempotency_key="chat:same-operation",
                    ttl_seconds=600,
                )

        await reserve()
        with pytest.raises(AppError) as raised:
            await reserve()

        assert (raised.value.status_code, raised.value.code) == (409, "duplicate_operation")

    async def test_an_operation_is_finalised_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
    ) -> None:
        async with session_factory() as session:
            reservation = await usage.reserve(
                session,
                user_id=library["owner"],
                workspace_id=uuid.UUID(library["workspace"]),
                operation=UsageOperation.CHAT,
                model="fake-model",
                input_tokens=100,
                output_tokens=100,
                idempotency_key="chat:once",
                ttl_seconds=600,
            )
            done = usage.Outcome(usage.UsageStatus.COMPLETED, usage.UsageSource.PROVIDER, 10, 5)
            again = usage.Outcome(usage.UsageStatus.FAILED, usage.UsageSource.UNAVAILABLE)

            assert await usage.finalize(session, reservation.id, done) is True
            assert await usage.finalize(session, reservation.id, again) is False

        (row,) = await usage_rows(db_engine, "idempotency_key = 'chat:once'")
        assert (row["status"], row["charged_tokens"]) == ("completed", 15)

    async def test_concurrent_finalisations_record_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
    ) -> None:
        async with session_factory() as session:
            reservation = await usage.reserve(
                session,
                user_id=library["owner"],
                workspace_id=uuid.UUID(library["workspace"]),
                operation=UsageOperation.CHAT,
                model="fake-model",
                input_tokens=100,
                output_tokens=100,
                idempotency_key="chat:race",
                ttl_seconds=600,
            )

        async def finish(tokens: int) -> bool:
            async with session_factory() as session:
                outcome = usage.Outcome(
                    usage.UsageStatus.COMPLETED, usage.UsageSource.PROVIDER, tokens, 0
                )
                return await usage.finalize(session, reservation.id, outcome)

        results = await asyncio.gather(*(finish(tokens) for tokens in range(1, 11)))

        assert results.count(True) == 1
        (row,) = await usage_rows(db_engine, "idempotency_key = 'chat:race'")
        assert row["charged_tokens"] == results.index(True) + 1

    async def test_negative_token_counts_are_refused(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, library: dict
    ) -> None:
        async with session_factory() as session:
            with pytest.raises(ValueError):
                await usage.reserve(
                    session,
                    user_id=library["owner"],
                    workspace_id=uuid.UUID(library["workspace"]),
                    operation=UsageOperation.CHAT,
                    model="fake-model",
                    input_tokens=-1,
                    output_tokens=0,
                    idempotency_key="chat:negative",
                    ttl_seconds=600,
                )

    async def test_the_database_rejects_malformed_records(self, db_engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            async with db_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO usage_events (user_id, operation, provider, model, status, "
                        "reserved_input_tokens, reserved_output_tokens, charged_tokens, "
                        "charged_cost, "
                        "currency, pricing_version, idempotency_key, expires_at) VALUES "
                        "(gen_random_uuid(), 'mining', 'openai', 'm', 'reserved', 0, 0, -5, 0, "
                        "'USD', 'v', 'k', now())"
                    )
                )


class TestIngestionCap:
    async def test_two_uploads_cannot_both_take_the_last_place(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        limits,  # noqa: ANN001
    ) -> None:
        from app.models.document import Document
        from app.services import documents as document_service

        limits(max_ingestion_jobs_per_workspace=1)
        owner = uuid.uuid4()
        workspace = uuid.UUID(await create_workspace(db_client, owner, "Race"))

        async def upload(index: int) -> str:
            async with session_factory() as session:
                try:
                    await document_service.ensure_ingestion_capacity(session, workspace)
                except AppError as error:
                    return error.code
                document_id = uuid.uuid4()
                session.add(
                    Document(
                        workspace_id=workspace,
                        uploaded_by=owner,
                        filename=f"d{index}.txt",
                        id=document_id,
                        storage_path=f"{workspace}/{document_id}/d{index}.txt",
                        mime_type="text/plain",
                        size_bytes=1,
                        sha256=f"{index:064d}",
                        status="uploaded",
                    )
                )
                # The upload itself takes time, while the place is held.
                await asyncio.sleep(0.05)
                await session.commit()
                return "stored"

        results = await asyncio.gather(*(upload(index) for index in range(4)))

        assert sorted(results) == ["ingestion_busy"] * 3 + ["stored"]


# --- Ingestion ----------------------------------------------------------------------------


class TestIngestionMetering:
    async def test_a_documents_embeddings_are_one_reserved_and_reconciled_record(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        process,  # noqa: ANN001
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Ingest")
        document = await add_text(
            db_client, process, workspace, owner, "policy.txt", "Refund policy. " * 50 + "\n"
        )

        (row,) = await usage_rows(db_engine, "operation = 'document_embedding'")
        assert (row["status"], row["usage_source"]) == ("completed", "provider")
        assert str(row["document_id"]) == document
        assert row["user_id"] == owner
        assert row["input_tokens"] == 100  # 100 words, one token per word in the fake
        assert row["reserved_input_tokens"] >= row["input_tokens"]
        assert row["charged_cost"] == Decimal("0.00001000")

    async def test_a_document_over_the_quota_fails_without_calling_the_provider(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        process,
        limits,  # noqa: ANN001
    ) -> None:
        limits(workspace_monthly_token_limit=10)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Small")

        document = await add_text(
            db_client, process, workspace, owner, "big.txt", "words " * 200 + "\n"
        )

        status = await db_client.get(f"{API}/documents/{document}/status", headers=auth(owner))
        assert status.json()["status"] == "failed"
        assert "monthly AI usage limit" in status.json()["error"]
        assert embedder.calls == []
        assert await usage_rows(db_engine, "operation = 'document_embedding'") == []

    async def test_a_failed_embedding_records_an_estimate(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        process,  # noqa: ANN001
    ) -> None:
        embedder.fail = EmbeddingError("down", retryable=True)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Failing")

        await add_text(db_client, process, workspace, owner, "doc.txt", "Some text to index.\n")

        (row,) = await usage_rows(db_engine, "operation = 'document_embedding'")
        assert (row["status"], row["usage_source"]) == ("failed", "estimated")
        assert row["estimated_input_tokens"] > 0


# --- Quotas -------------------------------------------------------------------------------


class TestQuotas:
    async def test_a_question_over_the_users_daily_tokens_is_refused_before_anything(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        embedder: FakeEmbedder,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(user_daily_token_limit=1000)
        calls_before = len(embedder.calls)

        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "user_quota_exceeded"
        assert 0 < error["retry_after_seconds"] <= 86_400
        assert await message_count(db_engine) == 0
        assert chat_model.calls == [] and len(embedder.calls) == calls_before
        assert await generation_rows(db_engine) == []

    async def test_the_daily_question_limit(
        self,
        db_client: AsyncClient,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(user_daily_chat_limit=1)

        first = await ask(db_client, library["conversation"], library["owner"], "refund policy")
        second = await stream(db_client, library["conversation"], library["owner"], "shipping")

        assert first.status_code == 201
        assert (second.status_code, second.json()["error"]["code"]) == (429, "user_quota_exceeded")

    async def test_the_workspace_monthly_token_quota(
        self,
        db_client: AsyncClient,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(workspace_monthly_token_limit=5000)

        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.status_code == 429
        assert response.json()["error"]["code"] == "workspace_quota_exceeded"
        assert response.json()["error"]["retry_after_seconds"] <= 31 * 86_400

    async def test_the_workspace_spending_limit(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        # One answer reserves about $0.012 at the test prices (about 10,000 tokens in, 1,000 out).
        limits(workspace_monthly_cost_limit=Decimal("0.015"))

        first = await ask(db_client, library["conversation"], library["owner"], "refund policy")
        second = await ask(db_client, library["conversation"], library["owner"], "shipping")
        # After reconciliation the first answer cost far less than it reserved, so another fits.
        assert (first.status_code, second.status_code) == (201, 201)

        limits(workspace_monthly_cost_limit=Decimal("0.0005"))
        third = await ask(db_client, library["conversation"], library["owner"], "holidays")
        assert (third.status_code, third.json()["error"]["code"]) == (
            429,
            "workspace_quota_exceeded",
        )
        assert "spending" in third.json()["error"]["message"]

    async def test_concurrent_generations_per_user_are_capped(
        self,
        db_client: AsyncClient,
        chat_model: FakeChatModel,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(max_concurrent_generations_per_user=1)
        chat_model.delay = 0.4
        other = (await create_conversation(db_client, library["workspace"], library["owner"]))["id"]

        first, second = await asyncio.gather(
            stream(db_client, library["conversation"], library["owner"], "refund"),
            stream(db_client, other, library["owner"], "shipping"),
        )

        codes = sorted(
            (r.status_code, r.json()["error"]["code"] if r.status_code != 200 else None)
            for r in (first, second)
        )
        assert codes == [(200, None), (429, "concurrent_generation_limit")]

        # Once the first finished, its place is free again.
        after = await stream(db_client, other, library["owner"], "shipping")
        assert after.status_code == 200

    async def test_a_failed_answer_frees_its_place(
        self,
        db_client: AsyncClient,
        chat_model: FakeChatModel,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(max_concurrent_generations_per_user=1)
        chat_model.fail = LLMError("x", code="unavailable", retryable=True)
        await stream(db_client, library["conversation"], library["owner"], "refund")
        chat_model.fail = None

        response = await stream(
            db_client, library["conversation"], library["owner"], "refund again"
        )

        assert sse_events(response)[-1]["event"] == "message_completed"

    async def test_concurrent_reservations_cannot_overspend(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(workspace_monthly_token_limit=1000)

        async def reserve(index: int) -> str:
            async with session_factory() as session:
                try:
                    await usage.reserve(
                        session,
                        user_id=uuid.uuid4(),
                        workspace_id=uuid.UUID(library["workspace"]),
                        operation=UsageOperation.DOCUMENT_EMBEDDING,
                        model="text-embedding-3-small",
                        input_tokens=300,
                        output_tokens=0,
                        idempotency_key=f"doc:{index}",
                        ttl_seconds=600,
                    )
                except AppError as error:
                    return error.code
                return "reserved"

        results = await asyncio.gather(*(reserve(index) for index in range(12)))

        assert results.count("reserved") == 3
        assert set(results) == {"reserved", "workspace_quota_exceeded"}
        total = (
            await rows(
                db_engine,
                "SELECT sum(charged_tokens) FROM usage_events WHERE idempotency_key LIKE 'doc:%'",
            )
        )[0][0]
        assert total == 900

    async def test_abandoned_reservations_expire_and_stop_blocking(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(max_concurrent_generations_per_user=1)
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO usage_events (workspace_id, user_id, operation, provider, model, "
                    "status, reserved_input_tokens, reserved_output_tokens, charged_tokens, "
                    "charged_cost, currency, pricing_version, idempotency_key, created_at, "
                    "expires_at) "
                    "VALUES (:w, :u, 'chat', 'openai', 'fake-model', 'reserved', 900, 100, "
                    "1000, 0, "
                    "'USD', 'v', 'crashed', now() - interval '20 minutes', "
                    "now() - interval '10 minutes')"
                ),
                {"w": uuid.UUID(library["workspace"]), "u": library["owner"]},
            )
            # Another, whose provider call had begun (its prompt of 300 tokens was sent).
            await connection.execute(
                text(
                    "INSERT INTO usage_events (workspace_id, user_id, operation, provider, model, "
                    "status, reserved_input_tokens, reserved_output_tokens, charged_tokens, "
                    "charged_cost, currency, pricing_version, idempotency_key, created_at, "
                    "expires_at, provider_called_at, prompt_tokens_sent) "
                    "VALUES (:w, :u, 'chat', 'openai', 'fake-model', 'reserved', 900, 100, "
                    "1000, 0, 'USD', 'v', 'crashed-mid-call', now() - interval '20 minutes', "
                    "now() - interval '10 minutes', now() - interval '19 minutes', 300)"
                ),
                {"w": uuid.UUID(library["workspace"]), "u": library["owner"]},
            )

        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.status_code == 201
        (crashed,) = await usage_rows(db_engine, "idempotency_key = 'crashed'")
        assert (crashed["status"], crashed["outcome"], crashed["error_code"]) == (
            "expired",
            "abandoned",
            "reservation_expired",
        )
        assert crashed["expired_at"] is not None
        # Phase 5.5: never sent to the provider, so nothing is charged: a crash before the
        # call no longer consumes quota for the rest of the period.
        assert (crashed["usage_source"], crashed["charged_tokens"]) == ("none", 0)
        (mid_call,) = await usage_rows(db_engine, "idempotency_key = 'crashed-mid-call'")
        # Sent, outcome unknown: an upper bound is held (prompt sent + maximum output).
        assert (mid_call["usage_source"], mid_call["charged_tokens"]) == ("unavailable", 400)
        assert mid_call["input_tokens"] is None

    async def test_search_is_refused_once_the_allowance_is_spent(
        self,
        db_client: AsyncClient,
        embedder: FakeEmbedder,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        await ask(db_client, library["conversation"], library["owner"], "refund policy")
        limits(workspace_monthly_token_limit=100)
        calls = len(embedder.calls)

        response = await db_client.post(
            f"{API}/workspaces/{library['workspace']}/search",
            json={"query": "refund"},
            headers=auth(library["owner"]),
        )

        assert (response.status_code, response.json()["error"]["code"]) == (
            429,
            "workspace_quota_exceeded",
        )
        assert len(embedder.calls) == calls

    async def test_deleting_a_workspace_does_not_reset_a_users_daily_quota(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        await ask(db_client, library["conversation"], library["owner"], "refund policy")
        await db_client.delete(
            f"{API}/workspaces/{library['workspace']}", headers=auth(library["owner"])
        )

        remaining = await usage_rows(db_engine, "user_id = :u", u=library["owner"])

        assert remaining and all(row["workspace_id"] is None for row in remaining)
        me = await db_client.get(f"{API}/usage/me", headers=auth(library["owner"]))
        assert me.json()["allowance"]["daily_tokens_used"] == sum(
            r["charged_tokens"] for r in remaining
        )


# --- Usage APIs and authorization ---------------------------------------------------------


class TestUsageApis:
    @pytest.fixture
    async def team(self, db_client: AsyncClient, library: dict) -> dict:
        admin = uuid.uuid4()
        await add_member(db_client, library["workspace"], library["owner"], admin, "admin")
        await ask(db_client, library["conversation"], library["owner"], "refund policy")
        theirs = (await create_conversation(db_client, library["workspace"], library["member"]))[
            "id"
        ]
        await ask(db_client, theirs, library["member"], "shipping policy")
        return {**library, "admin": admin}

    async def test_owners_and_admins_see_the_whole_workspace(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        for user in (team["owner"], team["admin"]):
            summary = (
                await db_client.get(
                    f"{API}/workspaces/{team['workspace']}/usage", headers=auth(user)
                )
            ).json()

            assert summary["scope"] == "workspace"
            # Three indexed documents, two answers and their two question embeddings.
            assert summary["totals"]["operations"] == 7
            assert {row["operation"] for row in summary["by_operation"]} == {
                "chat",
                "query_embedding",
                "document_embedding",
            }
            assert {row["model"] for row in summary["by_model"]} == {
                "fake-model-1",
                "text-embedding-3-small",
            }
            assert summary["daily"][0]["chat_tokens"] == 300
            assert summary["currency"] == "USD" and summary["pricing_version"] == "test-2026-01"
            assert Decimal(summary["totals"]["estimated_cost"]) > 0

            history = (
                await db_client.get(
                    f"{API}/workspaces/{team['workspace']}/usage/history", headers=auth(user)
                )
            ).json()
            assert history["total"] == 7
            assert {item["user_id"] for item in history["items"]} == {
                str(team["owner"]),
                str(team["member"]),
            }

    async def test_members_see_only_their_own_usage(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        summary = (
            await db_client.get(
                f"{API}/workspaces/{team['workspace']}/usage", headers=auth(team["member"])
            )
        ).json()
        history = (
            await db_client.get(
                f"{API}/workspaces/{team['workspace']}/usage/history", headers=auth(team["member"])
            )
        ).json()

        assert summary["scope"] == "own"
        assert summary["totals"]["operations"] == 2
        assert history["total"] == 2
        assert all(item["user_id"] is None for item in history["items"])
        # The workspace's remaining allowance applies to everyone, so everyone sees it.
        assert summary["allowance"]["monthly_tokens_used"] > summary["totals"]["tokens"]

    async def test_history_filters_and_pages(self, db_client: AsyncClient, team: dict) -> None:
        url = f"{API}/workspaces/{team['workspace']}/usage/history"
        filtered = (
            await db_client.get(url, params={"operation": "chat"}, headers=auth(team["owner"]))
        ).json()
        page = (
            await db_client.get(url, params={"limit": 1, "offset": 1}, headers=auth(team["owner"]))
        ).json()

        assert filtered["total"] == 2 and {i["operation"] for i in filtered["items"]} == {"chat"}
        assert (len(page["items"]), page["total"], page["offset"]) == (1, 7, 1)
        for bad in (
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
            {"operation": "mining"},
            {"status": "x"},
        ):
            assert (
                await db_client.get(url, params=bad, headers=auth(team["owner"]))
            ).status_code == 422

    async def test_no_content_is_ever_returned(self, db_client: AsyncClient, team: dict) -> None:
        body = (
            await db_client.get(
                f"{API}/workspaces/{team['workspace']}/usage/history", headers=auth(team["owner"])
            )
        ).text

        assert "refund" not in body.lower() and "shipping" not in body.lower()
        assert "idempotency_key" not in body and "request_id" not in body

    async def test_outsiders_and_strangers_get_nothing(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        outsider = uuid.uuid4()
        own = await create_workspace(db_client, outsider, "Mine")
        for path in ("usage", "usage/history", "quotas"):
            response = await db_client.get(
                f"{API}/workspaces/{team['workspace']}/{path}", headers=auth(outsider)
            )
            assert (response.status_code, response.json()["error"]["code"]) == (
                404,
                "workspace_not_found",
            )
            assert (
                await db_client.get(f"{API}/workspaces/{team['workspace']}/{path}")
            ).status_code == 401
            assert (
                await db_client.get(
                    f"{API}/workspaces/{uuid.uuid4()}/{path}", headers=auth(team["owner"])
                )
            ).status_code == 404

        # Their own workspace shows none of the other's usage.
        summary = (
            await db_client.get(f"{API}/workspaces/{own}/usage", headers=auth(outsider))
        ).json()
        assert summary["totals"]["operations"] == 0

    async def test_usage_is_separate_per_workspace_for_the_same_user(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        second = await create_workspace(db_client, team["owner"], "Second")

        summary = (
            await db_client.get(f"{API}/workspaces/{second}/usage", headers=auth(team["owner"]))
        ).json()
        me = (await db_client.get(f"{API}/usage/me", headers=auth(team["owner"]))).json()

        assert summary["totals"]["operations"] == 0
        # The owner's own: three document indexings, one answer and its question embedding.
        assert me["today"]["operations"] == 5

    async def test_only_owners_change_quotas_and_only_to_tighten_them(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        url = f"{API}/workspaces/{team['workspace']}/quotas"

        for user in (team["admin"], team["member"]):
            response = await db_client.patch(
                url, json={"monthly_token_limit": 10}, headers=auth(user)
            )
            assert (response.status_code, response.json()["error"]["code"]) == (403, "forbidden")
        assert (
            await db_client.patch(url, json={"monthly_token_limit": 10}, headers=auth(uuid.uuid4()))
        ).status_code == 404

        too_high = await db_client.patch(
            url, json={"monthly_token_limit": 10**12}, headers=auth(team["owner"])
        )
        assert (too_high.status_code, too_high.json()["error"]["code"]) == (
            422,
            "quota_above_system_limit",
        )
        for bad in (
            {"monthly_token_limit": -1},
            {"max_concurrent_operations": 0},
            {"monthly_cost_limit": "abc"},
            {"unknown": 1},
        ):
            assert (
                await db_client.patch(url, json=bad, headers=auth(team["owner"]))
            ).status_code == 422

        updated = await db_client.patch(
            url,
            json={"monthly_token_limit": 10, "monthly_cost_limit": "1.5"},
            headers=auth(team["owner"]),
        )
        assert updated.status_code == 200
        body = updated.json()
        assert body["effective"]["monthly_token_limit"] == 10
        assert Decimal(body["effective"]["monthly_cost_limit"]) == Decimal("1.5")
        assert body["can_edit"] is True

        # The tighter limit applies at once.
        refused = await ask(db_client, team["conversation"], team["owner"], "holidays")
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            429,
            "workspace_quota_exceeded",
        )

        # Null goes back to the system limit.
        reset = await db_client.patch(
            url, json={"monthly_token_limit": None}, headers=auth(team["owner"])
        )
        assert (
            reset.json()["effective"]["monthly_token_limit"]
            == reset.json()["system"]["monthly_token_limit"]
        )
        viewed = (await db_client.get(url, headers=auth(team["member"]))).json()
        assert viewed["can_edit"] is False

    async def test_my_usage_needs_sign_in(self, db_client: AsyncClient) -> None:
        assert (await db_client.get(f"{API}/usage/me")).status_code == 401


async def test_usage_logs_hold_no_content_keys_or_tokens(
    db_client: AsyncClient,
    chat_model: FakeChatModel,
    library: dict,
    caplog: pytest.LogCaptureFixture,
    limits,  # noqa: ANN001
) -> None:
    caplog.set_level(logging.DEBUG)
    chat_model.reply = long_answer

    await stream(db_client, library["conversation"], library["owner"], "What is the refund policy?")
    limits(user_daily_token_limit=10)
    await ask(db_client, library["conversation"], library["owner"], "What is the refund policy?")

    logged = caplog.text.lower()
    assert "refund policy?" not in logged
    assert "thirty days" not in logged
    assert "bearer" not in logged and "eyj" not in logged
    assert sources_in  # the fake's passages are never logged either
