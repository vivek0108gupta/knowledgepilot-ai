"""Phase 5.5, usage reliability: reserved query embeddings, provider outcomes, reservation
recovery, durable ingestion jobs, member quotas, alerts and the audit log. Real PostgreSQL;
the provider is a fake.
"""

# `library` is a fixture shared with test_chat; tests take it as an argument.
# ruff: noqa: F811

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.models.usage import UsageOperation
from app.services import chat as chat_service
from app.services import chat_stream, jobs, processing, usage
from app.services.embeddings import EmbeddingError
from app.services.llm import LLMError
from app.services.processing import ProcessingResult
from tests.auth import auth
from tests.fakes import FakeChatModel, FakeEmbedder, FakeStorage
from tests.files import TXT_MIME
from tests.test_chat import ask, library, rows  # noqa: F401
from tests.test_chat_stream import events as sse_events
from tests.test_chat_stream import long_answer, stream
from tests.test_conversations import API, create_conversation
from tests.test_cost_controls import TEST_PRICING, generation_rows, usage_rows
from tests.test_documents import upload_ok
from tests.test_workspaces import add_member, create_workspace


@pytest.fixture(autouse=True)
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import usage_reports

    for module in (usage, usage_reports):
        monkeypatch.setattr(module, "get_pricing", lambda: TEST_PRICING)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    settings = get_settings()

    def apply(**values: object) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return apply


async def search(client: AsyncClient, workspace: str, user: uuid.UUID, query: str = "refund"):  # noqa: ANN201
    return await client.post(
        f"{API}/workspaces/{workspace}/search", json={"query": query}, headers=auth(user)
    )


# --- Query embeddings ---------------------------------------------------------------------


class TestQueryEmbeddingReservations:
    async def test_a_search_is_reserved_then_reconciled_to_the_providers_count(
        self, db_client: AsyncClient, db_engine: AsyncEngine, library: dict
    ) -> None:
        response = await search(db_client, library["workspace"], library["owner"], "refund policy")

        assert response.status_code == 200
        (row,) = await usage_rows(
            db_engine, "operation = 'query_embedding' AND conversation_id IS NULL"
        )
        assert (row["status"], row["usage_source"], row["outcome"]) == (
            "completed",
            "provider",
            "executed_with_usage",
        )
        assert row["reserved_input_tokens"] >= row["input_tokens"] == 2
        assert row["charged_tokens"] == 2
        assert row["provider_called_at"] is not None and row["completed_at"] is not None

    async def test_an_exhausted_quota_stops_the_search_before_the_provider(
        self,
        db_client: AsyncClient,
        embedder: FakeEmbedder,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(user_daily_token_limit=1)
        calls = len(embedder.calls)

        response = await search(db_client, library["workspace"], library["owner"])

        assert (response.status_code, response.json()["error"]["code"]) == (
            429,
            "user_quota_exceeded",
        )
        assert len(embedder.calls) == calls

    async def test_concurrent_searches_cannot_overspend(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        # Indexing the library already used some of this month's tokens.
        used = (await rows(db_engine, "SELECT sum(charged_tokens) FROM usage_events"))[0][0]
        limits(workspace_monthly_token_limit=int(used) + 6)
        calls = len(embedder.calls)

        responses = await asyncio.gather(
            *(search(db_client, library["workspace"], library["owner"]) for _ in range(10))
        )

        allowed = [r for r in responses if r.status_code == 200]
        refused = [r for r in responses if r.status_code == 429]
        assert len(allowed) + len(refused) == 10 and refused and allowed
        assert len(embedder.calls) - calls == len(allowed)  # no refused search reached it
        total = (await rows(db_engine, "SELECT sum(charged_tokens) FROM usage_events"))[0][0]
        assert total <= used + 6

    @pytest.mark.parametrize(
        ("retryable", "source", "outcome"),
        [(True, "unavailable", "unknown"), (False, "none", "rejected")],
        ids=["failed after retries: may have run", "refused: did not run"],
    )
    async def test_a_failed_search_embedding_records_what_is_known(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        library: dict,
        retryable: bool,
        source: str,
        outcome: str,
    ) -> None:
        embedder.fail = EmbeddingError("down", retryable=retryable)

        response = await search(db_client, library["workspace"], library["owner"], "refund policy")

        assert response.status_code == 503
        (row,) = await usage_rows(
            db_engine, "operation = 'query_embedding' AND conversation_id IS NULL"
        )
        assert (row["status"], row["usage_source"], row["outcome"]) == ("failed", source, outcome)
        assert row["input_tokens"] is None  # never presented as provider usage
        assert row["charged_tokens"] == (row["prompt_tokens_sent"] if retryable else 0)

    async def test_an_answers_question_embedding_is_tied_to_the_answer(
        self, db_client: AsyncClient, db_engine: AsyncEngine, library: dict
    ) -> None:
        await ask(db_client, library["conversation"], library["owner"], "refund policy")

        (answer,) = await generation_rows(db_engine)
        (embedding,) = await usage_rows(db_engine, "operation = 'query_embedding'")
        assert embedding["idempotency_key"] == f"query_embedding:{answer['id']}"

    async def test_question_embeddings_take_no_concurrency_slot(
        self,
        db_client: AsyncClient,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        # One slot: the answer holds it while its own question is embedded.
        limits(max_concurrent_ai_operations_per_workspace=1)

        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.status_code == 201


# --- Provider outcomes --------------------------------------------------------------------


class TestOutcomes:
    async def test_a_streamed_answer_without_usage_is_estimated_from_what_was_seen(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = chat_model.stream

        async def no_usage(**kwargs: object):  # noqa: ANN202
            from dataclasses import replace

            from app.services.llm import Generation

            async for item in original(**kwargs):
                yield (
                    replace(item, input_tokens=None, output_tokens=None)
                    if isinstance(item, Generation)
                    else item
                )

        monkeypatch.setattr(chat_model, "stream", no_usage)

        await stream(db_client, library["conversation"], library["owner"], "refund")

        (row,) = await generation_rows(db_engine)
        assert (row["usage_source"], row["outcome"]) == ("estimated", "executed_without_usage")
        assert row["input_tokens"] is None
        assert row["estimated_output_tokens"] > 0

    async def test_a_stream_that_never_produced_text_and_timed_out_holds_an_upper_bound(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.fail = LLMError("x", code="unavailable", retryable=True)

        await stream(db_client, library["conversation"], library["owner"], "refund")

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["usage_source"], row["outcome"]) == (
            "failed",
            "unavailable",
            "unknown",
        )
        assert row["charged_tokens"] == row["prompt_tokens_sent"] + row["reserved_output_tokens"]
        assert row["input_tokens"] is None and row["estimated_input_tokens"] is None

    async def test_a_rate_limited_stream_did_not_run_and_costs_nothing(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.fail = LLMError("x", code="unavailable", retryable=True, maybe_executed=False)

        await stream(db_client, library["conversation"], library["owner"], "refund")

        (row,) = await generation_rows(db_engine)
        assert (row["usage_source"], row["outcome"], row["charged_tokens"]) == (
            "none",
            "rejected",
            0,
        )

    async def test_a_stream_failing_after_text_is_a_partial_stream(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        chat_model: FakeChatModel,
        library: dict,
    ) -> None:
        chat_model.reply = long_answer
        chat_model.fail, chat_model.fail_after = (
            LLMError("x", code="unavailable", retryable=True),
            4,
        )

        await stream(db_client, library["conversation"], library["owner"], "refund")

        (row,) = await generation_rows(db_engine)
        assert (row["usage_source"], row["outcome"]) == ("estimated", "partial_stream")

    async def _abandon(
        self,
        session_factory: async_sessionmaker,
        library: dict,
        chat_model: FakeChatModel,
        embedder: FakeEmbedder,
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
        return iterator, session

    async def test_stop_with_a_cancel_request_is_recorded_as_the_users_choice(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        chat_model: FakeChatModel,
        embedder: FakeEmbedder,
        library: dict,
    ) -> None:
        iterator, session = await self._abandon(session_factory, library, chat_model, embedder)

        cancel = await db_client.post(
            f"{API}/conversations/{library['conversation']}/chat/cancel",
            headers=auth(library["owner"]),
        )
        await iterator.aclose()
        await session.close()

        assert (cancel.status_code, cancel.json()) == (202, {"cancel_requested": True})
        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["outcome"]) == ("cancelled", "cancelled_by_user")
        assert row["cancel_requested_at"] is not None

    async def test_a_dropped_connection_is_recorded_as_a_disconnect(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        chat_model: FakeChatModel,
        embedder: FakeEmbedder,
        library: dict,
    ) -> None:
        iterator, session = await self._abandon(session_factory, library, chat_model, embedder)
        await iterator.aclose()
        await session.close()

        (row,) = await generation_rows(db_engine)
        assert (row["status"], row["outcome"], row["usage_source"]) == (
            "cancelled",
            "client_disconnected",
            "estimated",
        )

    async def test_only_the_conversations_owner_can_ask_to_cancel(
        self, db_client: AsyncClient, library: dict
    ) -> None:
        url = f"{API}/conversations/{library['conversation']}/chat/cancel"

        member = await db_client.post(url, headers=auth(library["member"]))
        outsider = await db_client.post(url, headers=auth(uuid.uuid4()))
        nothing_running = await db_client.post(url, headers=auth(library["owner"]))

        assert (member.status_code, outsider.status_code) == (404, 404)
        assert nothing_running.json() == {"cancel_requested": False}
        assert (await db_client.post(url)).status_code == 401


# --- Reservation recovery -----------------------------------------------------------------


async def reserve(
    session_factory: async_sessionmaker,
    library: dict,
    key: str,
    *,
    operation: UsageOperation = UsageOperation.CHAT,
    called_with: int | None = None,
) -> usage.Reservation:
    async with session_factory() as session:
        reservation = await usage.reserve(
            session,
            user_id=library["owner"],
            workspace_id=uuid.UUID(library["workspace"]),
            operation=operation,
            model="fake-model",
            input_tokens=900,
            output_tokens=100,
            idempotency_key=key,
            ttl_seconds=600,
        )
        if called_with is not None:
            await usage.mark_called(session, reservation.id, called_with)
    return reservation


async def age(engine: AsyncEngine, reservation_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE usage_events SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": reservation_id},
        )


class TestRecovery:
    async def test_the_sweep_recovers_reservations_nobody_is_using(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
    ) -> None:
        never_called = await reserve(session_factory, library, "a")
        mid_call = await reserve(session_factory, library, "b", called_with=250)
        alive = await reserve(session_factory, library, "c", called_with=250)
        await age(db_engine, never_called.id)
        await age(db_engine, mid_call.id)

        async with session_factory() as session:
            assert await usage.sweep_expired(session) == 2

        rows_ = {r["idempotency_key"]: r for r in await usage_rows(db_engine)}
        assert (rows_["a"]["status"], rows_["a"]["outcome"], rows_["a"]["charged_tokens"]) == (
            "expired",
            "abandoned",
            0,
        )
        assert (rows_["b"]["usage_source"], rows_["b"]["charged_tokens"]) == ("unavailable", 350)
        assert rows_["b"]["expired_at"] is not None and rows_["b"]["input_tokens"] is None
        assert rows_["c"]["status"] == "reserved"
        assert alive.id == rows_["c"]["id"]

    async def test_a_heartbeat_keeps_a_long_operation_from_being_expired(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
    ) -> None:
        reservation = await reserve(session_factory, library, "long", called_with=100)
        await age(db_engine, reservation.id)

        async with session_factory() as session:
            await usage.heartbeat(session, reservation.id, 600)
            assert await usage.sweep_expired(session) == 0

        (row,) = await usage_rows(db_engine, "idempotency_key = 'long'")
        assert row["status"] == "reserved" and row["heartbeat_at"] is not None

    async def test_concurrent_sweeps_expire_each_reservation_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
    ) -> None:
        # All reserved first: each reservation sweeps its user's stale ones as it goes.
        made = [await reserve(session_factory, library, f"s{i}", called_with=10) for i in range(6)]
        for reservation in made:
            await age(db_engine, reservation.id)

        async def sweep() -> int:
            async with session_factory() as session:
                return await usage.sweep_expired(session)

        results = await asyncio.gather(*(sweep() for _ in range(4)))

        assert sum(results) == 6

    async def test_a_late_result_replaces_the_held_charge_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
    ) -> None:
        reservation = await reserve(session_factory, library, "late", called_with=400)
        await age(db_engine, reservation.id)
        async with session_factory() as session:
            await usage.sweep_expired(session)
            real = usage.Outcome(usage.UsageStatus.COMPLETED, usage.UsageSource.PROVIDER, 380, 60)

            assert await usage.finalize(session, reservation.id, real) is True
            assert await usage.finalize(session, reservation.id, real) is False

        (row,) = await usage_rows(db_engine, "idempotency_key = 'late'")
        assert (row["status"], row["usage_source"], row["charged_tokens"]) == (
            "completed",
            "provider",
            440,
        )
        assert row["expired_at"] is not None and row["late_reconciled_at"] is not None

    async def test_an_abandoned_slot_is_freed_by_the_sweep(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(max_concurrent_generations_per_user=1)
        stuck = await reserve(session_factory, library, "stuck", called_with=10)
        blocked = await ask(db_client, library["conversation"], library["owner"], "refund policy")
        await age(db_engine, stuck.id)

        freed = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert blocked.json()["error"]["code"] == "concurrent_generation_limit"
        assert freed.status_code == 201


# --- Durable ingestion jobs ---------------------------------------------------------------


async def upload(client: AsyncClient, workspace: str, user: uuid.UUID, name: str, words: int = 30):  # noqa: ANN201
    content = (f"{name} " + "policy text " * words + "\n").encode()
    return (
        await upload_ok(client, workspace, user, name=name, content=content, content_type=TXT_MIME)
    )["id"]


async def job_rows(engine: AsyncEngine) -> list[dict]:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT * FROM ingestion_jobs ORDER BY created_at"))
        return [dict(row._mapping) for row in result]


class Workers:
    def __init__(self, factory: async_sessionmaker, storage: FakeStorage, embedder: FakeEmbedder):
        self.factory, self.storage, self.embedder = factory, storage, embedder

    async def claim(self, worker: str = "w1"):  # noqa: ANN201
        async with self.factory() as session:
            return await jobs.claim_next(session, worker)

    async def run(self, job, worker: str = "w1"):  # noqa: ANN001, ANN201
        return await jobs.run_job(
            job, worker, self.factory, lambda: self.storage, lambda: self.embedder
        )


@pytest.fixture
def workers(
    session_factory: async_sessionmaker, storage: FakeStorage, embedder: FakeEmbedder
) -> Workers:
    return Workers(session_factory, storage, embedder)


class TestJobs:
    async def test_an_upload_creates_its_job_in_the_same_transaction(
        self, db_client: AsyncClient, db_engine: AsyncEngine
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Jobs")

        document = await upload(db_client, workspace, owner, "a.txt")

        (job,) = await job_rows(db_engine)
        assert (str(job["document_id"]), str(job["workspace_id"]), job["status"]) == (
            document,
            workspace,
            "queued",
        )
        assert job["max_attempts"] == get_settings().ingestion_max_attempts

    async def test_a_claimed_job_is_processed_to_ready(
        self, db_client: AsyncClient, db_engine: AsyncEngine, workers: Workers
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Jobs")
        document = await upload(db_client, workspace, owner, "a.txt")

        job = await workers.claim()
        status = await workers.run(job)

        assert status == "succeeded"
        (row,) = await job_rows(db_engine)
        assert (row["status"], row["attempts"], row["lease_owner"]) == ("succeeded", 1, None)
        state = await db_client.get(f"{API}/documents/{document}/status", headers=auth(owner))
        assert state.json()["status"] == "ready"
        (embedding,) = await usage_rows(db_engine, "operation = 'document_embedding'")
        assert embedding["idempotency_key"] == f"document_embedding:{job.id}:1"

    async def test_two_workers_never_take_the_same_job(
        self, db_client: AsyncClient, workers: Workers
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Jobs")
        await upload(db_client, workspace, owner, "a.txt")

        claimed = await asyncio.gather(*(workers.claim(f"w{index}") for index in range(8)))

        assert sum(job is not None for job in claimed) == 1

    async def test_enqueueing_twice_keeps_one_active_job(
        self, db_client: AsyncClient, db_engine: AsyncEngine, session_factory: async_sessionmaker
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Jobs")
        document = uuid.UUID(await upload(db_client, workspace, owner, "a.txt"))

        async with session_factory() as session:
            assert await jobs.enqueue(session, document) is False
            assert await jobs.enqueue(session, uuid.uuid4()) is False  # no such document

        assert len(await job_rows(db_engine)) == 1

    async def test_each_workspace_runs_at_most_its_share_at_once(
        self,
        db_client: AsyncClient,
        workers: Workers,
        limits,  # noqa: ANN001
    ) -> None:
        limits(ingestion_workers_per_workspace=1)
        owner = uuid.uuid4()
        busy = await create_workspace(db_client, owner, "Busy")
        quiet = await create_workspace(db_client, owner, "Quiet")
        for index in range(3):
            await upload(db_client, busy, owner, f"b{index}.txt")
        await upload(db_client, quiet, owner, "q.txt")

        claimed = await asyncio.gather(*(workers.claim(f"w{index}") for index in range(6)))

        taken = [job for job in claimed if job is not None]
        assert sorted(str(job.workspace_id) for job in taken) == sorted([busy, quiet])

    async def test_a_transient_failure_is_retried_and_then_succeeds_without_duplicates(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workers: Workers,
        limits,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        limits(ingestion_retry_delay_seconds=0)
        # Two chunks per embedding call, so the failure lands after a batch was written.
        monkeypatch.setattr(processing, "CHUNK_BATCH", 2)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Retry")
        document = await upload(db_client, workspace, owner, "a.txt", words=900)
        embedder.fail, embedder.fail_after = EmbeddingError("down", retryable=True), 1

        first = await workers.run(await workers.claim())
        embedder.fail = None
        second = await workers.run(await workers.claim())

        assert (first, second) == ("queued", "succeeded")
        (job,) = await job_rows(db_engine)
        assert job["attempts"] == 2
        chunks = (
            await rows(
                db_engine, "SELECT count(*), count(DISTINCT chunk_index) FROM document_chunks"
            )
        )[0]
        assert chunks[0] == chunks[1] > 1  # no chunk written twice
        attempts = await usage_rows(db_engine, "operation = 'document_embedding'")
        assert [(r["status"], r["idempotency_key"][-2:]) for r in attempts] == [
            ("failed", ":1"),
            ("completed", ":2"),
        ]
        state = await db_client.get(f"{API}/documents/{document}/status", headers=auth(owner))
        assert state.json()["status"] == "ready"

    async def test_retries_stop_at_the_limit_and_the_document_fails(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workers: Workers,
        limits,  # noqa: ANN001
    ) -> None:
        limits(ingestion_retry_delay_seconds=0, ingestion_max_attempts=2)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Fails")
        document = await upload(db_client, workspace, owner, "a.txt")
        embedder.fail = EmbeddingError("down", retryable=True)

        results = [await workers.run(await workers.claim()) for _ in range(2)]

        assert results == ["queued", "failed"]
        assert await workers.claim() is None
        state = await db_client.get(f"{API}/documents/{document}/status", headers=auth(owner))
        assert state.json()["status"] == "failed"

    async def test_a_permanent_failure_is_not_retried(
        self, db_client: AsyncClient, embedder: FakeEmbedder, workers: Workers
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Fails")
        await upload(db_client, workspace, owner, "a.txt")
        embedder.fail = EmbeddingError("rejected", retryable=False)

        assert await workers.run(await workers.claim()) == "failed"
        assert await workers.claim() is None

    async def test_a_job_whose_worker_stopped_is_recovered_and_finished_by_another(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        workers: Workers,
        limits,  # noqa: ANN001
    ) -> None:
        limits(ingestion_retry_delay_seconds=0)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Restart")
        document = await upload(db_client, workspace, owner, "a.txt")
        lost = await workers.claim("crashed-process")
        # The process stops: its lease runs out, and it never reports a result.
        async with db_engine.begin() as connection:
            await connection.execute(
                text("UPDATE ingestion_jobs SET leased_until = now() - interval '1 second'")
            )
            await connection.execute(
                text("UPDATE documents SET status = 'processing' WHERE id = :d"),
                {"d": uuid.UUID(document)},
            )

        async with session_factory() as session:
            assert await jobs.recover_expired(session) == 1
        finished = await workers.run(await workers.claim("new-process"), "new-process")
        # The old worker coming back cannot overwrite the new attempt's result.
        async with session_factory() as session:
            late = await jobs.complete(session, lost, "crashed-process", ProcessingResult.READY)

        assert (finished, late) == ("succeeded", None)
        (job,) = await job_rows(db_engine)
        assert (job["status"], job["attempts"]) == ("succeeded", 2)

    async def test_the_startup_maintenance_recovers_jobs_and_reservations(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
        embedder: FakeEmbedder,
        workers: Workers,
        library: dict,
    ) -> None:
        await upload(db_client, library["workspace"], library["owner"], "extra.txt")
        await workers.claim("crashed-process")
        stuck = await reserve(session_factory, library, "stuck", called_with=50)
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE ingestion_jobs SET leased_until = now() - interval '1 second' "
                    "WHERE status = 'running'"
                )
            )
        await age(db_engine, stuck.id)

        queue = jobs.JobQueue(session_factory, lambda: storage, lambda: embedder, 2)
        await queue.maintain()

        assert {job["status"] for job in await job_rows(db_engine)} >= {"queued"}
        (row,) = await usage_rows(db_engine, "idempotency_key = 'stuck'")
        assert row["status"] == "expired"

    async def test_the_worker_loop_processes_queued_documents(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
        embedder: FakeEmbedder,
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Loop")
        for index in range(3):
            await upload(db_client, workspace, owner, f"d{index}.txt")
        queue = jobs.JobQueue(session_factory, lambda: storage, lambda: embedder, 2)

        while await queue.run_available():
            await queue.drain()
        await queue.drain()

        assert [job["status"] for job in await job_rows(db_engine)] == ["succeeded"] * 3

    async def test_deleting_a_document_removes_its_job(
        self, db_client: AsyncClient, db_engine: AsyncEngine
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Delete")
        document = await upload(db_client, workspace, owner, "a.txt")

        await db_client.delete(f"{API}/documents/{document}", headers=auth(owner))

        assert await job_rows(db_engine) == []


# --- Member quotas, alerts, audit ---------------------------------------------------------


class TestAdminControls:
    @pytest.fixture
    async def team(self, db_client: AsyncClient, library: dict) -> dict:
        admin = uuid.uuid4()
        await add_member(db_client, library["workspace"], library["owner"], admin, "admin")
        mine = (await create_conversation(db_client, library["workspace"], library["member"]))["id"]
        return {**library, "admin": admin, "member_conversation": mine}

    async def test_an_owner_can_tighten_one_members_monthly_tokens(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        url = f"{API}/workspaces/{team['workspace']}/members/{team['member']}/quota"

        set_ = await db_client.put(
            url, json={"monthly_token_limit": 100}, headers=auth(team["owner"])
        )
        refused = await ask(db_client, team["member_conversation"], team["member"], "refund")
        owner_ok = await ask(db_client, team["conversation"], team["owner"], "refund")

        assert (set_.status_code, set_.json()["monthly_token_limit"]) == (200, 100)
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            429,
            "member_quota_exceeded",
        )
        assert owner_ok.status_code == 201

    async def test_a_members_daily_question_limit(self, db_client: AsyncClient, team: dict) -> None:
        url = f"{API}/workspaces/{team['workspace']}/members/{team['member']}/quota"
        await db_client.put(url, json={"daily_question_limit": 1}, headers=auth(team["owner"]))

        first = await ask(db_client, team["member_conversation"], team["member"], "refund")
        second = await ask(db_client, team["member_conversation"], team["member"], "shipping")

        assert first.status_code == 201
        assert second.json()["error"]["code"] == "member_quota_exceeded"

    async def test_only_owners_set_member_quotas_within_the_workspace_limit(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        url = f"{API}/workspaces/{team['workspace']}/members/{team['member']}/quota"
        body = {"monthly_token_limit": 10}

        assert (await db_client.put(url, json=body, headers=auth(team["admin"]))).status_code == 403
        assert (
            await db_client.put(url, json=body, headers=auth(team["member"]))
        ).status_code == 403
        assert (await db_client.put(url, json=body, headers=auth(uuid.uuid4()))).status_code == 404
        stranger = f"{API}/workspaces/{team['workspace']}/members/{uuid.uuid4()}/quota"
        missing = await db_client.put(stranger, json=body, headers=auth(team["owner"]))
        assert (missing.status_code, missing.json()["error"]["code"]) == (404, "member_not_found")
        too_high = await db_client.put(
            url, json={"monthly_token_limit": 10**12}, headers=auth(team["owner"])
        )
        assert too_high.json()["error"]["code"] == "quota_above_workspace_limit"
        for bad in ({"monthly_token_limit": -1}, {"extra": 1}):
            assert (
                await db_client.put(url, json=bad, headers=auth(team["owner"]))
            ).status_code == 422

    async def test_member_quotas_are_listed_for_managers_only(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        url = f"{API}/workspaces/{team['workspace']}/members/quotas"
        await ask(db_client, team["conversation"], team["owner"], "refund")

        listed = await db_client.get(url, headers=auth(team["admin"]))

        assert listed.status_code == 200
        by_user = {row["user_id"]: row for row in listed.json()}
        assert set(by_user) == {str(team["owner"]), str(team["member"]), str(team["admin"])}
        assert by_user[str(team["owner"])]["monthly_tokens_used"] > 0
        assert (await db_client.get(url, headers=auth(team["member"]))).status_code == 403

    async def test_policy_changes_are_audited_with_before_and_after(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        base = f"{API}/workspaces/{team['workspace']}"
        await db_client.patch(
            f"{base}/quotas", json={"monthly_token_limit": 5000}, headers=auth(team["owner"])
        )
        await db_client.patch(
            f"{base}/quotas", json={"monthly_token_limit": 4000}, headers=auth(team["owner"])
        )
        await db_client.put(
            f"{base}/members/{team['member']}/quota",
            json={"daily_question_limit": 3},
            headers=auth(team["owner"]),
        )

        log = (await db_client.get(f"{base}/audit", headers=auth(team["admin"]))).json()

        assert log["total"] == 3
        member_change, second, first = log["items"]
        assert (member_change["action"], member_change["target_user_id"]) == (
            "member_quota.updated",
            str(team["member"]),
        )
        assert member_change["details"] == {
            "before": {"daily_question_limit": None},
            "after": {"daily_question_limit": 3},
        }
        assert second["details"] == {
            "before": {"monthly_token_limit": 5000},
            "after": {"monthly_token_limit": 4000},
        }
        assert first["actor_id"] == str(team["owner"])
        assert (
            await db_client.get(f"{base}/audit", headers=auth(team["member"]))
        ).status_code == 403
        assert (await db_client.get(f"{base}/audit", headers=auth(uuid.uuid4()))).status_code == 404

    async def test_crossing_a_threshold_records_one_alert_per_period(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        team: dict,
        limits,  # noqa: ANN001
    ) -> None:
        # Realistic limits (the suite's are huge): 1% of 100,000 tokens is crossed, 100% is not.
        limits(workspace_monthly_token_limit=100_000, user_daily_token_limit=10_000_000)
        base = f"{API}/workspaces/{team['workspace']}"
        changed = await db_client.patch(
            f"{base}/quotas", json={"alert_threshold_percent": 1}, headers=auth(team["owner"])
        )
        assert changed.json()["alert_threshold_percent"] == 1

        await ask(db_client, team["conversation"], team["owner"], "refund")
        await ask(db_client, team["conversation"], team["owner"], "shipping")

        alerts = (await db_client.get(f"{base}/usage/alerts", headers=auth(team["member"]))).json()
        kinds = sorted((a["kind"], a["threshold_percent"]) for a in alerts)
        # The default workspace limits are large: 1% is crossed, 100% is not. Once each.
        assert ("workspace_tokens", 1) in kinds
        assert all(level == 1 for _, level in kinds)
        assert len(kinds) == len(set(kinds))
        stored = (await rows(db_engine, "SELECT count(*) FROM usage_alerts"))[0][0]
        assert stored == len(alerts)

    async def test_a_users_daily_warning_appears_in_their_own_usage(
        self,
        db_client: AsyncClient,
        team: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(usage_alert_threshold_percent=1, user_daily_token_limit=100_000)

        await ask(db_client, team["conversation"], team["owner"], "refund")

        mine = (await db_client.get(f"{API}/usage/me", headers=auth(team["owner"]))).json()
        theirs = (await db_client.get(f"{API}/usage/me", headers=auth(team["member"]))).json()
        assert [a["kind"] for a in mine["alerts"]] == ["user_daily_tokens"]
        assert theirs["alerts"] == []

    async def test_alerts_stay_in_their_workspace(self, db_client: AsyncClient, team: dict) -> None:
        await db_client.patch(
            f"{API}/workspaces/{team['workspace']}/quotas",
            json={"alert_threshold_percent": 1},
            headers=auth(team["owner"]),
        )
        await ask(db_client, team["conversation"], team["owner"], "refund")
        other = await create_workspace(db_client, team["owner"], "Other")

        elsewhere = await db_client.get(
            f"{API}/workspaces/{other}/usage/alerts", headers=auth(team["owner"])
        )
        outsider = await db_client.get(
            f"{API}/workspaces/{team['workspace']}/usage/alerts", headers=auth(uuid.uuid4())
        )

        assert [a["kind"] for a in elsewhere.json() if a["kind"] != "user_daily_tokens"] == []
        assert outsider.status_code == 404

    async def test_the_summary_says_how_much_was_reported_estimated_or_held(
        self,
        db_client: AsyncClient,
        chat_model: FakeChatModel,
        team: dict,
    ) -> None:
        await ask(db_client, team["conversation"], team["owner"], "refund")
        chat_model.fail = LLMError("x", code="unavailable", retryable=True)
        await ask(db_client, team["conversation"], team["owner"], "shipping")

        summary = (
            await db_client.get(
                f"{API}/workspaces/{team['workspace']}/usage", headers=auth(team["owner"])
            )
        ).json()

        accounting = summary["accounting"]
        assert accounting["held_tokens"] > 0 and accounting["provider_reported_tokens"] > 0
        assert (
            accounting["provider_reported_tokens"]
            + accounting["estimated_tokens"]
            + accounting["held_tokens"]
            == summary["totals"]["tokens"]
        )
        assert summary["outcomes"]["unknown"] == 1
        assert summary["outcomes"]["executed_with_usage"] >= 1
        history = (
            await db_client.get(
                f"{API}/workspaces/{team['workspace']}/usage/history",
                headers=auth(team["owner"]),
            )
        ).json()
        assert "unknown" in {item["outcome"] for item in history["items"]}


async def test_the_workspace_concurrency_slot_is_shared_with_ingestion(
    db_client: AsyncClient,
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker,
    library: dict,
    limits,  # noqa: ANN001
) -> None:
    limits(max_concurrent_ai_operations_per_workspace=1)
    await reserve(session_factory, library, "indexing", operation=UsageOperation.DOCUMENT_EMBEDDING)

    response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

    assert response.json()["error"]["code"] == "workspace_busy"
    assert sse_events  # imported for parity with the streaming tests
    _ = datetime.now(UTC) - timedelta(seconds=1)
