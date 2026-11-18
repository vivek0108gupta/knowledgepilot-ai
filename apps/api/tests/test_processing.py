"""The processing pipeline end to end: real extraction, real Postgres, fake storage."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.services import processing
from app.services.processing import (
    InProcessQueue,
    owns_storage_path,
    process_document,
    recover_interrupted,
)
from tests.auth import auth
from tests.fakes import FakeEmbedder, FakeStorage, RecordingQueue
from tests.files import DOCX_MIME, TXT_MIME, make_docx, make_encrypted_pdf, make_pdf
from tests.test_documents import API, upload_ok
from tests.test_workspaces import add_member, create_workspace


@pytest.fixture
async def workspace(db_client: AsyncClient) -> dict:
    owner, member, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace_id = await create_workspace(db_client, owner)
    await add_member(db_client, workspace_id, owner, member, "member")
    await add_member(db_client, workspace_id, owner, other, "member")
    return {"id": workspace_id, "owner": owner, "member": member, "other": other}


async def fetch(client: AsyncClient, document_id: str, user: uuid.UUID) -> dict:
    response = await client.get(f"{API}/documents/{document_id}", headers=auth(user))
    assert response.status_code == 200, response.text
    return response.json()


async def segments(engine: AsyncEngine, document_id: str) -> list[dict]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT position, kind, page_number, paragraph_index, line_start, line_end, "
                "section, content FROM document_segments WHERE document_id = :id ORDER BY position"
            ),
            {"id": uuid.UUID(document_id)},
        )
        return [dict(row._mapping) for row in rows]


async def set_status(engine: AsyncEngine, document_id: str, status: str, age: timedelta) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE documents SET status = :status, updated_at = :at WHERE id = :id"),
            {"status": status, "at": datetime.now(UTC) - age, "id": uuid.UUID(document_id)},
        )


class TestLifecycle:
    async def test_pdf_goes_from_uploaded_to_ready_with_page_references(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["One", "", "Three"])
        )
        assert document["status"] == "uploaded"

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["page_count"], after["error"]) == ("ready", 3, None)
        assert after["updated_at"] > document["updated_at"]
        stored = await segments(db_engine, document["id"])
        assert [(s["position"], s["kind"], s["page_number"], s["content"]) for s in stored] == [
            (0, "page", 1, "One"),
            (1, "page", 3, "Three"),
        ]

    async def test_docx_keeps_paragraph_and_section_references(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="a.docx",
            content=make_docx(),
            content_type=DOCX_MIME,
        )

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["page_count"]) == ("ready", None)
        stored = await segments(db_engine, document["id"])
        assert [(s["kind"], s["paragraph_index"], s["section"], s["content"]) for s in stored] == [
            ("paragraph", 0, "Introduction", "Introduction"),
            ("paragraph", 1, "Introduction", "First paragraph."),
            ("paragraph", 2, "Introduction", "Second paragraph."),
        ]

    async def test_txt_keeps_line_references(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="a.txt",
            content=b"Title\n\nBody line one\nBody line two\n",
            content_type=TXT_MIME,
        )

        await process(document["id"])

        stored = await segments(db_engine, document["id"])
        assert [(s["kind"], s["line_start"], s["line_end"], s["content"]) for s in stored] == [
            ("lines", 1, 1, "Title"),
            ("lines", 3, 4, "Body line one\nBody line two"),
        ]

    async def test_status_endpoint_reports_each_stage(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        url = f"{API}/documents/{document['id']}/status"

        async def status() -> str:
            return (await db_client.get(url, headers=auth(workspace["member"]))).json()["status"]

        assert await status() == "uploaded"
        await set_status(db_engine, document["id"], "processing", timedelta(0))
        assert await status() == "processing"
        await set_status(db_engine, document["id"], "uploaded", timedelta(0))
        await process(document["id"])
        assert await status() == "ready"


class TestFailures:
    async def test_scanned_pdf_fails_with_an_ocr_message(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["", ""])
        )

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert after["status"] == "failed"
        assert "OCR" in after["error"] and "not supported" in after["error"]
        assert await segments(db_engine, document["id"]) == []

    async def test_password_protected_pdf_fails_with_a_reason(
        self,
        db_client: AsyncClient,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_encrypted_pdf()
        )

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], "password-protected" in after["error"]) == ("failed", True)

    async def test_storage_failure_is_recorded_without_internals(
        self,
        db_client: AsyncClient,
        storage: FakeStorage,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        storage.fail_download = True

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert after["status"] == "failed"
        assert after["error"] == "The file could not be read from storage. Try again."

    async def test_unexpected_crash_is_recorded_with_a_generic_message(
        self,
        db_client: AsyncClient,
        workspace: dict,
        process,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def explode(*_args: object) -> None:
            raise RuntimeError("secret internal detail /var/lib/keys")

        monkeypatch.setattr(processing, "extract", explode)
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        await process(document["id"])  # must not raise

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert after["status"] == "failed"
        assert after["error"] == "Something went wrong while processing this document."
        assert "secret internal detail" not in after["error"]
        assert "secret internal detail" in caplog.text  # the cause is logged for operators

    async def test_document_text_is_never_logged(
        self,
        db_client: AsyncClient,
        workspace: dict,
        process,  # noqa: ANN001
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="payroll-2026.pdf",
            content=make_pdf(["Confidential salary figures"]),
        )

        with caplog.at_level("DEBUG"):
            await process(document["id"])

        assert "Confidential salary figures" not in caplog.text
        assert "payroll-2026" not in caplog.text
        assert document["id"] in caplog.text


class TestDuplicateProcessing:
    async def test_two_workers_on_the_same_document_extract_it_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        embedder: FakeEmbedder,
        session_factory: async_sessionmaker,
        workspace: dict,
    ) -> None:
        document = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["Once", "Only"])
        )
        document_id = uuid.UUID(document["id"])

        await asyncio.gather(
            *(
                process_document(document_id, session_factory, lambda: storage, lambda: embedder)
                for _ in range(4)
            )
        )

        assert len(storage.downloads) == 1  # only one worker claimed it
        assert len(await segments(db_engine, document["id"])) == 2
        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "ready"

    async def test_processing_a_ready_document_again_does_nothing(
        self,
        db_client: AsyncClient,
        storage: FakeStorage,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        await process(document["id"])

        await process(document["id"])

        assert len(storage.downloads) == 1

    async def test_processing_an_unknown_document_does_nothing(self, process) -> None:  # noqa: ANN001
        await process(uuid.uuid4())

    async def test_document_deleted_while_processing_leaves_nothing_behind(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        original_download = storage.download_to

        async def delete_midway(path, destination) -> None:  # noqa: ANN001
            await original_download(path, destination)
            response = await db_client.delete(
                f"{API}/documents/{document['id']}", headers=auth(workspace["owner"])
            )
            assert response.status_code == 204

        storage.download_to = delete_midway  # type: ignore[method-assign]

        await process(document["id"])  # must not raise or resurrect the document

        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM documents")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM document_segments")) == 0


class TestRetry:
    async def test_failed_document_can_be_retried_and_then_succeeds(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        queue: RecordingQueue,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        storage.fail_download = True
        await process(document["id"])
        storage.fail_download = False
        queue.enqueued.clear()

        response = await db_client.post(
            f"{API}/documents/{document['id']}/retry", headers=auth(workspace["owner"])
        )

        assert response.status_code == 202
        assert (response.json()["status"], response.json()["error"]) == ("uploaded", None)
        assert queue.enqueued == [uuid.UUID(document["id"])]

        await process(document["id"])
        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "ready"
        assert len(await segments(db_engine, document["id"])) == 1

    @pytest.mark.parametrize("state", ["uploaded", "ready"])
    async def test_only_a_failed_document_can_be_retried(
        self,
        db_client: AsyncClient,
        queue: RecordingQueue,
        workspace: dict,
        process,
        state: str,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        if state == "ready":
            await process(document["id"])
        queue.enqueued.clear()

        response = await db_client.post(
            f"{API}/documents/{document['id']}/retry", headers=auth(workspace["owner"])
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_retryable"
        assert queue.enqueued == []

    async def test_document_stuck_in_processing_can_be_retried_once_stale(
        self, db_client: AsyncClient, db_engine: AsyncEngine, workspace: dict
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        url = f"{API}/documents/{document['id']}/retry"

        await set_status(db_engine, document["id"], "processing", timedelta(minutes=1))
        still_running = await db_client.post(url, headers=auth(workspace["owner"]))
        await set_status(db_engine, document["id"], "processing", timedelta(minutes=30))
        interrupted = await db_client.post(url, headers=auth(workspace["owner"]))

        assert (still_running.status_code, interrupted.status_code) == (409, 202)

    async def test_retry_follows_the_same_permissions_as_delete(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["member"])
        url = f"{API}/documents/{document['id']}/retry"

        async def retry_as(user: uuid.UUID) -> int:
            await set_status(db_engine, document["id"], "failed", timedelta(0))
            return (await db_client.post(url, headers=auth(user))).status_code

        assert await retry_as(workspace["other"]) == 403  # another regular member
        assert await retry_as(workspace["member"]) == 202  # the uploader
        assert await retry_as(workspace["owner"]) == 202
        assert await retry_as(uuid.uuid4()) == 404  # not in the workspace at all


class TestRecoveryAndQueue:
    async def test_startup_requeues_waiting_and_interrupted_documents_only(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        async def new(label: str) -> str:
            return (
                await upload_ok(
                    db_client, workspace["id"], workspace["owner"], content=make_pdf([label])
                )
            )["id"]

        waiting, interrupted, running, done, failed = [await new(n) for n in "abcde"]
        await set_status(db_engine, interrupted, "processing", timedelta(minutes=30))
        await set_status(db_engine, running, "processing", timedelta(seconds=5))
        await process(done)
        await set_status(db_engine, failed, "failed", timedelta(0))
        recovery_queue = RecordingQueue()

        count = await recover_interrupted(session_factory, recovery_queue)

        assert count == 2
        assert set(recovery_queue.enqueued) == {uuid.UUID(waiting), uuid.UUID(interrupted)}

    async def test_in_process_queue_runs_documents_and_survives_a_crash(
        self,
        db_client: AsyncClient,
        storage: FakeStorage,
        embedder: FakeEmbedder,
        session_factory: async_sessionmaker,
        workspace: dict,
    ) -> None:
        good = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["a"])
        )
        bad = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf([""])
        )
        real_queue = InProcessQueue(
            session_factory, lambda: storage, lambda: embedder, concurrency=1
        )

        real_queue.enqueue(uuid.UUID(bad["id"]))
        real_queue.enqueue(uuid.UUID(good["id"]))
        real_queue.enqueue(uuid.uuid4())  # unknown: ignored
        await real_queue.drain()

        assert (await fetch(db_client, good["id"], workspace["owner"]))["status"] == "ready"
        assert (await fetch(db_client, bad["id"], workspace["owner"]))["status"] == "failed"


class TestStoragePathOwnership:
    WORKSPACE = uuid.UUID("11111111-1111-4111-8111-111111111111")
    DOCUMENT = uuid.UUID("22222222-2222-4222-8222-222222222222")
    OTHER = uuid.UUID("33333333-3333-4333-8333-333333333333")

    def test_a_path_in_the_documents_own_folder_is_owned(self) -> None:
        assert owns_storage_path(
            self.WORKSPACE, self.DOCUMENT, f"{self.WORKSPACE}/{self.DOCUMENT}/report.pdf"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "{other}/{document}/report.pdf",
            "{workspace}/{other}/report.pdf",
            "{workspace}/{document}/../{other}/report.pdf",
            "{workspace}/{document}/sub/report.pdf",
            "{workspace}/{document}/..",
            "{workspace}/{document}/",
            "{workspace}/{document}",
            "/{workspace}/{document}/report.pdf",
            "report.pdf",
            "",
        ],
    )
    def test_any_other_path_is_not(self, path: str) -> None:
        candidate = path.format(workspace=self.WORKSPACE, document=self.DOCUMENT, other=self.OTHER)

        assert not owns_storage_path(self.WORKSPACE, self.DOCUMENT, candidate)
