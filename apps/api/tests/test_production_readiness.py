"""Phase 5.6, production readiness: request size limits, production settings, migration
locking, graceful worker shutdown, storage consistency and the orphan audit, pricing history,
alert idempotency and quota periods. Real PostgreSQL; fakes for storage and the provider."""

# `library` is a fixture shared with test_chat; tests take it as an argument.
# ruff: noqa: F811

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app import main as main_module
from app.core.config import Settings, get_settings
from app.models.usage import UsageOperation
from app.services import documents as document_service
from app.services import jobs, pricing, usage
from app.tools import storage_audit
from tests.auth import auth
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import FakeEmbedder, FakeStorage
from tests.test_chat import ask, library, rows  # noqa: F401
from tests.test_conversations import API, create_conversation
from tests.test_cost_controls import TEST_PRICING, usage_rows
from tests.test_reliability import Workers, job_rows, reserve, upload
from tests.test_workspaces import create_workspace

API_ROOT = Path(__file__).resolve().parent.parent


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


@pytest.fixture
def workers(
    session_factory: async_sessionmaker, storage: FakeStorage, embedder: FakeEmbedder
) -> Workers:
    return Workers(session_factory, storage, embedder)


# --- H1: request size ---------------------------------------------------------------------


class TestRequestSize:
    url = f"{API}/conversations/{uuid.uuid4()}/chat"

    async def test_an_oversized_body_is_refused_before_authentication(
        self, db_client: AsyncClient
    ) -> None:
        body = b'{"message": "' + b"x" * 1_100_000 + b'"}'

        response = await db_client.post(
            self.url, content=body, headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "request_too_large"

    async def test_a_body_without_a_declared_length_is_refused(
        self, db_client: AsyncClient
    ) -> None:
        async def chunks():  # noqa: ANN202
            yield b'{"message": "hi"}'

        response = await db_client.post(
            self.url, content=chunks(), headers={"Content-Type": "application/json"}
        )

        assert (response.status_code, response.json()["error"]["code"]) == (411, "length_required")

    async def test_normal_requests_are_unaffected(
        self, db_client: AsyncClient, library: dict
    ) -> None:
        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.status_code == 201

    async def test_uploads_keep_their_own_larger_limit(self, db_client: AsyncClient) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Big")

        document = await upload(db_client, workspace, owner, "big.txt", words=200_000)  # ~2.4 MB

        assert document

    async def test_the_limit_is_configurable_and_validated(self) -> None:
        assert Settings().max_request_body_bytes == 1_048_576
        with pytest.raises(ValueError):
            Settings(max_request_body_bytes=10)


# --- M5: production settings --------------------------------------------------------------


class TestProductionSettings:
    async def openapi_status(self, monkeypatch: pytest.MonkeyPatch, **values: object) -> int:
        monkeypatch.setattr(main_module, "get_settings", lambda: Settings(**values))
        app = main_module.create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return (await client.get("/openapi.json")).status_code

    def test_an_empty_docs_setting_means_the_default(self) -> None:
        # As Docker Compose passes an unset variable.
        assert Settings(api_docs_enabled="", environment="production").docs_enabled is False
        assert Settings(api_docs_enabled="", environment="development").docs_enabled is True

    async def test_api_docs_are_off_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self.openapi_status(monkeypatch, environment="production") == 404
        assert await self.openapi_status(monkeypatch, environment="development") == 200
        assert (
            await self.openapi_status(monkeypatch, environment="production", api_docs_enabled=True)
            == 200
        )


# --- H4: concurrent migrations ------------------------------------------------------------


async def test_replicas_migrating_at_once_take_turns() -> None:
    """Three `alembic upgrade head` at once on an empty database all succeed, and the schema
    is applied exactly once."""
    url = make_url(TEST_DATABASE_URL)
    name = f"{url.database}_migrate"
    admin = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, database="postgres"
    )
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.execute(f'CREATE DATABASE "{name}"')
        env = {
            **os.environ,
            "DATABASE_URL": str(url.set(database=name).render_as_string(hide_password=False)),
        }
        processes = [
            await asyncio.create_subprocess_exec(
                "uv",
                "run",
                "alembic",
                "upgrade",
                "head",
                cwd=API_ROOT,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            for _ in range(3)
        ]
        results = await asyncio.gather(*(process.communicate() for process in processes))
        codes = [process.returncode for process in processes]
        assert codes == [0, 0, 0], [output.decode()[-500:] for output, _ in results]
        check = await asyncpg.connect(
            host=url.host, port=url.port, user=url.username, password=url.password, database=name
        )
        try:
            assert await check.fetchval("SELECT version_num FROM alembic_version") == "0008"
            assert await check.fetchval("SELECT count(*) FROM alembic_version") == 1
        finally:
            await check.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
    assert subprocess  # (asyncio subprocesses above)


# --- H3: graceful shutdown ----------------------------------------------------------------


class SlowEmbedder(FakeEmbedder):
    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(self.seconds)
        return await super().embed(texts)


class TestGracefulShutdown:
    async def test_unfinished_jobs_are_handed_back_without_losing_an_attempt(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Shutdown")
        document = await upload(db_client, workspace, owner, "slow.txt")
        queue = jobs.JobQueue(session_factory, lambda: storage, lambda: SlowEmbedder(30), 2)

        assert await queue.run_available() == 1
        await asyncio.sleep(0.3)  # the job is embedding
        await queue.stop(grace_seconds=0.2)

        (job,) = await job_rows(db_engine)
        assert (job["status"], job["attempts"], job["max_attempts"]) == ("queued", 1, 4)
        assert (job["last_error"], job["lease_owner"]) == ("worker_shutdown", None)
        state = await db_client.get(f"{API}/documents/{document}/status", headers=auth(owner))
        assert state.json()["status"] == "uploaded"

        # Another process picks it up at once and finishes it.
        other = jobs.JobQueue(session_factory, lambda: storage, lambda: FakeEmbedder(), 2)
        await other.run_available()
        await other.drain()
        (job,) = await job_rows(db_engine)
        assert (job["status"], job["attempts"]) == ("succeeded", 2)
        # The interrupted attempt and the finished one each have their own usage record.
        attempts = await usage_rows(db_engine, "operation = 'document_embedding'")
        assert sorted(row["status"] for row in attempts) == ["completed", "failed"]
        (count,) = (
            await rows(
                db_engine, "SELECT count(DISTINCT chunk_index) = count(*) FROM document_chunks"
            )
        )[0]
        assert count is True

    async def test_jobs_that_finish_within_the_grace_period_complete(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Shutdown")
        await upload(db_client, workspace, owner, "quick.txt")
        queue = jobs.JobQueue(session_factory, lambda: storage, lambda: SlowEmbedder(0.2), 2)

        await queue.run_available()
        await queue.stop(grace_seconds=5)

        (job,) = await job_rows(db_engine)
        assert job["status"] == "succeeded"

    async def test_a_stopping_worker_claims_nothing_more(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, storage: FakeStorage
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Shutdown")
        await upload(db_client, workspace, owner, "a.txt")
        queue = jobs.JobQueue(session_factory, lambda: storage, lambda: FakeEmbedder(), 2)

        await queue.stop()

        assert await queue.run_available() == 0


# --- M7: documents without a job ----------------------------------------------------------


async def test_a_waiting_document_without_a_job_is_queued_by_maintenance(
    db_client: AsyncClient, db_engine: AsyncEngine, session_factory: async_sessionmaker
) -> None:
    owner = uuid.uuid4()
    workspace = await create_workspace(db_client, owner, "Legacy")
    await upload(db_client, workspace, owner, "legacy.txt")
    async with db_engine.begin() as connection:
        await connection.execute(text("DELETE FROM ingestion_jobs"))
        await connection.execute(
            text("UPDATE documents SET updated_at = now() - interval '5 minutes'")
        )

    async with session_factory() as session:
        first = await jobs.enqueue_missing(session)
        second = await jobs.enqueue_missing(session)

    assert (first, second) == (1, 0)
    assert [job["status"] for job in await job_rows(db_engine)] == ["queued"]


# --- Document lifecycle and storage consistency -------------------------------------------


class TestDocumentLifecycle:
    async def test_a_successful_upload_stores_the_file_the_document_and_its_job(
        self, db_client: AsyncClient, db_engine: AsyncEngine, storage: FakeStorage
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Life")

        document = await upload(db_client, workspace, owner, "a.txt")

        path = (await rows(db_engine, "SELECT storage_path FROM documents"))[0][0]
        assert path in storage.objects and path.split("/")[1] == document
        assert len(await job_rows(db_engine)) == 1

    async def test_a_failed_commit_leaves_no_file_document_or_job(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("job could not be written")

        monkeypatch.setattr(document_service.jobs, "enqueue", broken)
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Life")

        response = await db_client.post(
            f"{API}/workspaces/{workspace}/documents",
            files={"file": ("a.txt", b"Some text here.\n", "text/plain")},
            headers=auth(owner),
        )

        assert (response.status_code, response.json()["error"]["code"]) == (500, "internal_error")
        assert storage.objects == {} and len(storage.deleted) == 1
        assert (await rows(db_engine, "SELECT count(*) FROM documents"))[0][0] == 0
        assert await job_rows(db_engine) == []

    async def test_a_failed_storage_upload_leaves_nothing(
        self, db_client: AsyncClient, db_engine: AsyncEngine, storage: FakeStorage
    ) -> None:
        storage.fail_upload = True
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Life")

        response = await db_client.post(
            f"{API}/workspaces/{workspace}/documents",
            files={"file": ("a.txt", b"Some text here.\n", "text/plain")},
            headers=auth(owner),
        )

        assert (response.status_code, response.json()["error"]["code"]) == (
            502,
            "storage_unavailable",
        )
        assert (await rows(db_engine, "SELECT count(*) FROM documents"))[0][0] == 0
        assert await job_rows(db_engine) == []

    async def test_a_failed_clean_up_leaves_an_orphan_that_the_audit_finds(
        self,
        db_client: AsyncClient,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("job could not be written")

        monkeypatch.setattr(document_service.jobs, "enqueue", broken)
        storage.fail_delete = True
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Life")
        response = await db_client.post(
            f"{API}/workspaces/{workspace}/documents",
            files={"file": ("a.txt", b"Some text here.\n", "text/plain")},
            headers=auth(owner),
        )
        assert response.status_code == 500
        storage.fail_delete = False

        report = await storage_audit.audit(session_factory, storage, delete=True)

        assert len(report.orphans) == 1 and report.deleted == report.orphans
        assert storage.objects == {}

    @pytest.mark.parametrize("what", ["document", "workspace"])
    async def test_deletion_during_processing_leaves_no_chunks_or_open_usage(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
        what: str,
    ) -> None:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Life")
        document = await upload(db_client, workspace, owner, "a.txt")
        queue = jobs.JobQueue(session_factory, lambda: storage, lambda: SlowEmbedder(0.5), 1)
        await queue.run_available()
        await asyncio.sleep(0.2)

        target = (
            f"{API}/documents/{document}" if what == "document" else f"{API}/workspaces/{workspace}"
        )
        deleted = await db_client.delete(target, headers=auth(owner))
        await queue.drain()

        assert deleted.status_code == 204
        assert (await rows(db_engine, "SELECT count(*) FROM document_chunks"))[0][0] == 0
        assert await job_rows(db_engine) == []
        assert storage.objects == {}
        open_ = await usage_rows(db_engine, "status = 'reserved'")
        assert open_ == []


class TestStorageAudit:
    async def setup(self, db_client: AsyncClient, storage: FakeStorage) -> dict:
        owner = uuid.uuid4()
        workspace = await create_workspace(db_client, owner, "Audit")
        document = await upload(db_client, workspace, owner, "kept.txt")
        orphan = f"{workspace}/{uuid.uuid4()}/lost.txt"
        recent = f"{workspace}/{uuid.uuid4()}/in-flight.txt"
        storage.objects[orphan] = b"x"
        storage.objects[recent] = b"x"
        storage.created[recent] = datetime.now(UTC)
        return {"workspace": workspace, "document": document, "orphan": orphan, "recent": recent}

    async def test_a_report_changes_nothing(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, storage: FakeStorage
    ) -> None:
        data = await self.setup(db_client, storage)
        before = dict(storage.objects)

        report = await storage_audit.audit(session_factory, storage)

        assert report.orphans == [data["orphan"]]
        assert report.too_recent == [data["recent"]]
        assert (report.objects, report.referenced, report.deleted) == (3, 1, [])
        assert storage.objects == before

    async def test_delete_removes_only_old_unreferenced_objects(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, storage: FakeStorage
    ) -> None:
        data = await self.setup(db_client, storage)

        report = await storage_audit.audit(session_factory, storage, delete=True)

        assert report.deleted == [data["orphan"]]
        assert data["recent"] in storage.objects
        assert len([p for p in storage.objects if p.endswith("kept.txt")]) == 1

    async def test_the_prefix_limits_what_is_audited(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, storage: FakeStorage
    ) -> None:
        data = await self.setup(db_client, storage)
        elsewhere = f"{uuid.uuid4()}/{uuid.uuid4()}/other.txt"
        storage.objects[elsewhere] = b"x"

        report = await storage_audit.audit(
            session_factory, storage, prefix=f"{data['workspace']}/", delete=True
        )

        assert report.deleted == [data["orphan"]]
        assert elsewhere in storage.objects

    async def test_a_document_whose_file_is_missing_is_reported(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, storage: FakeStorage
    ) -> None:
        data = await self.setup(db_client, storage)
        storage.objects = {k: v for k, v in storage.objects.items() if not k.endswith("kept.txt")}

        report = await storage_audit.audit(session_factory, storage)

        assert report.missing_files == [data["document"]]

    async def test_nothing_is_deleted_if_the_bucket_cannot_be_listed(
        self, db_client: AsyncClient, session_factory: async_sessionmaker, storage: FakeStorage
    ) -> None:
        await self.setup(db_client, storage)
        storage.fail_list = True
        before = dict(storage.objects)

        with pytest.raises(Exception):  # noqa: B017, PT011 - StorageError
            await storage_audit.audit(session_factory, storage, delete=True)

        assert storage.objects == before

    async def test_a_document_committed_during_the_audit_keeps_its_file(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        storage: FakeStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = await self.setup(db_client, storage)
        original = storage_audit.referenced_paths
        calls = {"n": 0}

        async def racing(session, paths):  # noqa: ANN001, ANN202
            calls["n"] += 1
            if calls["n"] == 2:  # the re-check before deleting: a document appeared meanwhile
                workspace_id, document_id, name = data["orphan"].split("/")
                async with db_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO documents (id, workspace_id, uploaded_by, filename, "
                            "storage_path, mime_type, size_bytes, sha256, status) VALUES "
                            "(:d, :w, gen_random_uuid(), :n, :p, 'text/plain', 1, :h, 'uploaded')"
                        ),
                        {
                            "d": uuid.UUID(document_id),
                            "w": uuid.UUID(workspace_id),
                            "n": name,
                            "p": data["orphan"],
                            "h": "f" * 64,
                        },
                    )
            return await original(session, paths)

        monkeypatch.setattr(storage_audit, "referenced_paths", racing)

        report = await storage_audit.audit(session_factory, storage, delete=True)

        assert report.deleted == []
        assert data["orphan"] in storage.objects

    def test_the_command_refuses_a_negative_age(self) -> None:
        with pytest.raises(SystemExit):
            storage_audit.main(["--min-age-minutes", "-1"])


# --- Pricing ------------------------------------------------------------------------------


class TestPricingHistory:
    async def test_finalisation_records_the_prices_it_actually_used(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        library: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reservation = await reserve(session_factory, library, "repriced", called_with=100)
        newer = pricing.parse_pricing(
            {
                "version": "test-2026-12",
                "currency": "USD",
                "models": {
                    "fake-model": {"input_per_million": "2.00", "output_per_million": "4.00"}
                },
            }
        )
        monkeypatch.setattr(usage, "get_pricing", lambda: newer)

        async with session_factory() as session:
            await usage.finalize(
                session,
                reservation.id,
                usage.Outcome(
                    usage.UsageStatus.COMPLETED, usage.UsageSource.PROVIDER, 1_000_000, 0
                ),
            )

        (row,) = await usage_rows(db_engine, "idempotency_key = 'repriced'")
        assert (row["pricing_version"], row["charged_cost"]) == (
            "test-2026-12",
            Decimal("2.00000000"),
        )

    async def test_changing_prices_never_reprices_history(
        self, db_client: AsyncClient, library: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await ask(db_client, library["conversation"], library["owner"], "refund policy")
        url = f"{API}/workspaces/{library['workspace']}/usage"
        before = (await db_client.get(url, headers=auth(library["owner"]))).json()["totals"]

        free = pricing.parse_pricing(
            {"version": "free", "currency": "USD", "models": {"fake": {"input_per_million": "0"}}}
        )
        from app.services import usage_reports

        monkeypatch.setattr(usage, "get_pricing", lambda: free)
        monkeypatch.setattr(usage_reports, "get_pricing", lambda: free)
        after = (await db_client.get(url, headers=auth(library["owner"]))).json()["totals"]

        assert after == before and Decimal(before["estimated_cost"]) > 0

    def test_the_price_table_carries_its_dates(self) -> None:
        from app.core.config import DEFAULT_PRICING

        table = pricing.parse_pricing(DEFAULT_PRICING)
        assert (table.effective_from, table.verified_on) == ("2025-04-14", "2026-11-12")
        with pytest.raises(pricing.PricingError):
            pricing.parse_pricing({**DEFAULT_PRICING, "verified_on": "last week"})

    async def test_the_summary_says_how_current_the_prices_are(
        self, db_client: AsyncClient, library: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import DEFAULT_PRICING
        from app.services import usage_reports

        monkeypatch.setattr(
            usage_reports, "get_pricing", lambda: pricing.parse_pricing(DEFAULT_PRICING)
        )

        summary = (
            await db_client.get(
                f"{API}/workspaces/{library['workspace']}/usage", headers=auth(library["owner"])
            )
        ).json()

        assert (summary["pricing_effective_from"], summary["pricing_verified_on"]) == (
            "2025-04-14",
            "2026-11-12",
        )


# --- Alerts and quota periods -------------------------------------------------------------


class TestAlertsAndPeriods:
    async def test_polling_and_repeated_requests_do_not_multiply_alerts(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(
            usage_alert_threshold_percent=1,
            user_daily_token_limit=100_000,
            workspace_monthly_token_limit=100_000,
        )
        for question in ("refund", "shipping", "holidays"):
            await ask(db_client, library["conversation"], library["owner"], question)
        url = f"{API}/workspaces/{library['workspace']}/usage/alerts"
        for _ in range(5):
            await db_client.get(url, headers=auth(library["owner"]))
            await db_client.get(f"{API}/usage/me", headers=auth(library["owner"]))

        stored = await rows(
            db_engine, "SELECT kind, threshold_percent, count(*) FROM usage_alerts GROUP BY 1, 2"
        )
        assert stored and all(count == 1 for _, _, count in stored)

    async def test_a_new_day_starts_a_new_daily_alert(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(usage_alert_threshold_percent=1, user_daily_token_limit=100_000)
        yesterday = usage.day_start(datetime.now(UTC)) - timedelta(days=1)
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO usage_alerts (kind, subject_id, user_id, threshold_percent, "
                    "period_start, used, limit_value) "
                    "VALUES ('user_daily_tokens', :u, :u, 1, :p, 5000, 100000)"
                ),
                {"u": library["owner"], "p": yesterday},
            )

        await ask(db_client, library["conversation"], library["owner"], "refund")

        periods = await rows(
            db_engine,
            "SELECT period_start FROM usage_alerts WHERE kind = 'user_daily_tokens' ORDER BY 1",
        )
        assert len(periods) == 2 and periods[1][0] > periods[0][0]
        me = (await db_client.get(f"{API}/usage/me", headers=auth(library["owner"]))).json()
        assert len(me["alerts"]) == 1  # only today's

    async def test_last_months_usage_does_not_count_against_this_month(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        limits(workspace_monthly_token_limit=50_000)
        last_month = usage.month_start(datetime.now(UTC)) - timedelta(days=3)
        async with db_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO usage_events (workspace_id, user_id, operation, provider, "
                    "model, status, usage_source, reserved_input_tokens, reserved_output_tokens, "
                    "charged_tokens, charged_cost, currency, pricing_version, idempotency_key, "
                    "created_at, completed_at, expires_at) VALUES (:w, :u, 'chat', 'openai', 'm', "
                    "'completed', 'provider', 0, 0, 10000000, 0, 'USD', 'v', 'last-month', "
                    ":t, :t, :t)"
                ),
                {"w": uuid.UUID(library["workspace"]), "u": library["owner"], "t": last_month},
            )

        response = await ask(db_client, library["conversation"], library["owner"], "refund policy")

        assert response.status_code == 201

    async def test_the_daily_limit_applies_across_all_of_a_users_workspaces(
        self,
        db_client: AsyncClient,
        library: dict,
        limits,  # noqa: ANN001
    ) -> None:
        await ask(db_client, library["conversation"], library["owner"], "refund policy")
        used = (await db_client.get(f"{API}/usage/me", headers=auth(library["owner"]))).json()
        limits(user_daily_token_limit=used["allowance"]["daily_tokens_used"] + 100)
        other = await create_workspace(db_client, library["owner"], "Elsewhere")
        conversation = (await create_conversation(db_client, other, library["owner"]))["id"]

        response = await ask(db_client, conversation, library["owner"], "anything")

        assert (response.status_code, response.json()["error"]["code"]) == (
            429,
            "user_quota_exceeded",
        )


async def test_duplicate_usage_keys_are_refused_across_operations(
    db_client: AsyncClient, session_factory: async_sessionmaker, library: dict
) -> None:
    await reserve(session_factory, library, "same-key")
    with pytest.raises(Exception) as raised:  # noqa: PT011
        await reserve(
            session_factory, library, "same-key", operation=UsageOperation.DOCUMENT_EMBEDDING
        )

    assert getattr(raised.value, "code", None) == "duplicate_operation"
