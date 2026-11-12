"""Chunking and embedding inside the processing pipeline.

Real extraction, real chunking, real PostgreSQL with pgvector. Only OpenAI and file storage
are replaced by fakes, so vectors really are written to and read from a `vector(1536)` column.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.errors import AppError
from app.services import processing
from app.services.embeddings import EmbeddingError
from app.services.processing import process_document, recover_interrupted
from tests.auth import auth
from tests.fakes import FakeEmbedder, FakeStorage, RecordingQueue, fake_vector
from tests.files import DOCX_MIME, TXT_MIME, make_docx, make_pdf
from tests.test_documents import API, upload_ok
from tests.test_processing import fetch, set_status
from tests.test_workspaces import WORKSPACES, create_workspace


@pytest.fixture
async def workspace(db_client: AsyncClient) -> dict:
    owner = uuid.uuid4()
    return {"id": await create_workspace(db_client, owner), "owner": owner}


async def chunks(engine: AsyncEngine, document_id: str) -> list[dict]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT chunk_index, content, token_count, page_number, paragraph_index, "
                "line_start, line_end, metadata, workspace_id::text AS workspace_id, "
                "vector_dims(embedding) AS dimensions "
                "FROM document_chunks WHERE document_id = :id ORDER BY chunk_index"
            ),
            {"id": uuid.UUID(document_id)},
        )
        return [dict(row._mapping) for row in rows]


async def chunk_total(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return await connection.scalar(text("SELECT count(*) FROM document_chunks"))


def long_text(lines: int = 4000) -> bytes:
    """A text file big enough to need several embedding batches (more than 64 chunks)."""
    return "\n".join(
        f"Line {n}: the quick brown fox jumps over the lazy dog." for n in range(lines)
    ).encode()


class TestSuccessfulPipeline:
    async def test_pdf_is_chunked_embedded_and_marked_ready(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            content=make_pdf(["Refunds are issued within 30 days.", "", "Shipping takes a week."]),
        )

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["page_count"], after["error"]) == ("ready", 3, None)
        (chunk,) = await chunks(db_engine, document["id"])
        assert chunk["chunk_index"] == 0
        assert chunk["content"] == "Refunds are issued within 30 days.\n\nShipping takes a week."
        assert chunk["dimensions"] == 1536
        assert chunk["workspace_id"] == workspace["id"]
        assert chunk["token_count"] > 0
        assert (chunk["page_number"], chunk["metadata"]) == (1, {"kind": "page", "page_end": 3})
        assert (chunk["paragraph_index"], chunk["line_start"], chunk["line_end"]) == (
            None,
            None,
            None,
        )
        # Exactly the chunk text was sent for embedding, once.
        assert embedder.calls == [[chunk["content"]]]

    async def test_docx_chunks_have_paragraph_references_and_no_page_number(
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

        (chunk,) = await chunks(db_engine, document["id"])
        assert (chunk["paragraph_index"], chunk["page_number"]) == (0, None)
        assert chunk["metadata"] == {
            "kind": "paragraph",
            "paragraph_end": 2,
            "section": "Introduction",
        }

    async def test_txt_chunks_have_line_ranges(
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

        (chunk,) = await chunks(db_engine, document["id"])
        assert (chunk["line_start"], chunk["line_end"]) == (1, 4)
        assert (chunk["page_number"], chunk["paragraph_index"]) == (None, None)

    async def test_a_large_document_is_embedded_in_batches_with_ordered_chunks(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="big.txt",
            content=long_text(),
            content_type=TXT_MIME,
        )

        await process(document["id"])

        stored = await chunks(db_engine, document["id"])
        assert len(stored) > 64
        assert [row["chunk_index"] for row in stored] == list(range(len(stored)))
        assert all(row["token_count"] <= 800 and row["dimensions"] == 1536 for row in stored)
        assert stored[0]["line_start"] == 1 and stored[-1]["line_end"] == 4000
        starts = [row["line_start"] for row in stored]
        assert starts == sorted(starts)
        # Sent in batches of at most 64, never all at once.
        assert len(embedder.calls) > 1
        assert all(len(call) <= 64 for call in embedder.calls)
        assert sum(len(call) for call in embedder.calls) == len(stored)
        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "ready"

    async def test_extracted_segments_are_still_saved(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        await process(document["id"])

        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM document_segments")) == 1

    async def test_document_text_is_not_logged(
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
            content=make_pdf(["Confidential merger terms"]),
        )

        with caplog.at_level("DEBUG"):
            await process(document["id"])

        assert "Confidential merger terms" not in caplog.text
        assert "1 chunks" in caplog.text


class TestFailures:
    async def test_extraction_failure_never_reaches_the_embedder(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["", ""])
        )

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert after["status"] == "failed" and "OCR" in after["error"]
        assert embedder.calls == []
        assert await chunk_total(db_engine) == 0

    @pytest.mark.parametrize(
        ("retryable", "message"),
        [
            (True, "The document's text could not be indexed for search. Try again."),
            (False, "The document's text could not be indexed for search."),
        ],
    )
    async def test_embedding_failure_fails_the_document_and_leaves_it_unready(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
        retryable: bool,
        message: str,
    ) -> None:
        embedder.fail = EmbeddingError("upstream said: secret-internal-detail", retryable=retryable)
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        await process(document["id"])

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["error"]) == ("failed", message)
        assert "secret-internal-detail" not in after["error"]
        assert await chunk_total(db_engine) == 0
        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM document_segments")) == 0

    async def test_failure_part_way_through_removes_the_chunks_already_written(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="big.txt",
            content=long_text(),
            content_type=TXT_MIME,
        )
        embedder.fail = EmbeddingError("rate limited", retryable=True)
        embedder.fail_after = 1  # the first batch succeeds and is written, the second fails
        written_before_failure: list[int] = []
        real_mark_failed = processing.mark_failed

        async def spy(session, document_id, message):  # noqa: ANN001, ANN202
            written_before_failure.append(await chunk_total(db_engine))
            await real_mark_failed(session, document_id, message)

        monkeypatch.setattr(processing, "mark_failed", spy)

        await process(document["id"])

        assert written_before_failure == [64]  # a batch really had been saved...
        assert await chunk_total(db_engine) == 0  # ...and was cleaned up
        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "failed"

    async def test_missing_api_key_fails_the_document_with_a_clear_reason(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        session_factory: async_sessionmaker,
        workspace: dict,
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        def no_key():  # noqa: ANN202
            raise AppError(
                503, "embeddings_not_configured", "Search is not configured. Set OPENAI_API_KEY."
            )

        await process_document(uuid.UUID(document["id"]), session_factory, lambda: storage, no_key)

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["error"]) == (
            "failed",
            "Search indexing is not set up on the server yet.",
        )
        assert await chunk_total(db_engine) == 0

    async def test_wrong_number_of_vectors_fails_the_document(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        async def short(texts: list[str]) -> list[list[float]]:
            return []

        embedder.embed = short  # type: ignore[method-assign]
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        await process(document["id"])

        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "failed"
        assert await chunk_total(db_engine) == 0

    async def test_database_failure_while_saving_chunks_fails_the_document_cleanly(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        async def wrong_size(texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3] for _ in texts]  # the column only accepts 1536 dimensions

        embedder.embed = wrong_size  # type: ignore[method-assign]
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        await process(document["id"])  # must not raise

        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["error"]) == (
            "failed",
            "Something went wrong while processing this document.",
        )
        assert await chunk_total(db_engine) == 0

    async def test_database_failure_at_the_final_step_does_not_leave_the_document_ready(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken_finish(*_args: object) -> bool:
            raise ConnectionError("database connection lost")

        monkeypatch.setattr(processing, "finish", broken_finish)
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        await process(document["id"])

        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "failed"
        assert await chunk_total(db_engine) == 0

    async def test_a_document_is_never_ready_with_fewer_chunks_than_were_produced(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = processing.embed_and_store

        async def loses_one(*args: object) -> int:
            return await real(*args) + 1  # claims one more chunk than was actually stored

        monkeypatch.setattr(processing, "embed_and_store", loses_one)
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        await process(document["id"])

        assert (await fetch(db_client, document["id"], workspace["owner"]))["status"] == "failed"
        assert await chunk_total(db_engine) == 0


class TestRetryAndDuplicates:
    async def test_retry_after_an_embedding_failure_succeeds_without_duplicates(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="big.txt",
            content=long_text(),
            content_type=TXT_MIME,
        )
        embedder.fail = EmbeddingError("rate limited", retryable=True)
        embedder.fail_after = 1
        await process(document["id"])
        embedder.fail = None

        retried = await db_client.post(
            f"{API}/documents/{document['id']}/retry", headers=auth(workspace["owner"])
        )
        await process(document["id"])

        assert retried.status_code == 202
        after = await fetch(db_client, document["id"], workspace["owner"])
        assert (after["status"], after["error"]) == ("ready", None)
        stored = await chunks(db_engine, document["id"])
        assert [row["chunk_index"] for row in stored] == list(range(len(stored)))
        assert len(stored) == len({row["chunk_index"] for row in stored}) > 64

    async def test_concurrent_workers_embed_a_document_once(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        embedder: FakeEmbedder,
        session_factory: async_sessionmaker,
        workspace: dict,
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        document_id = uuid.UUID(document["id"])

        await asyncio.gather(
            *(
                process_document(document_id, session_factory, lambda: storage, lambda: embedder)
                for _ in range(5)
            )
        )

        assert len(embedder.calls) == 1
        assert len(await chunks(db_engine, document["id"])) == 1

    async def test_a_ready_document_is_not_embedded_again(
        self,
        db_client: AsyncClient,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        await process(document["id"])

        await process(document["id"])
        await process(document["id"])

        assert len(embedder.calls) == 1

    async def test_chunks_left_by_an_interrupted_attempt_are_replaced_not_duplicated(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        # As if the API had stopped after writing some chunks: stale rows, document still waiting.
        async with db_engine.begin() as connection:
            for index in (0, 1, 2):
                await connection.execute(
                    text(
                        "INSERT INTO document_chunks "
                        "(document_id, workspace_id, chunk_index, content, "
                        "token_count, embedding) VALUES (:d, :w, :i, 'stale', 1, :e)"
                    ),
                    {
                        "d": uuid.UUID(document["id"]),
                        "w": uuid.UUID(workspace["id"]),
                        "i": index,
                        "e": str(fake_vector("stale")),
                    },
                )

        await process(document["id"])

        stored = await chunks(db_engine, document["id"])
        assert len(stored) == 1
        assert stored[0]["content"] != "stale"

    async def test_document_deleted_while_embedding_leaves_no_chunks(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        embedder: FakeEmbedder,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(
            db_client,
            workspace["id"],
            workspace["owner"],
            name="big.txt",
            content=long_text(),
            content_type=TXT_MIME,
        )
        real_embed = embedder.embed

        async def delete_after_first_batch(texts: list[str]) -> list[list[float]]:
            vectors = await real_embed(texts)
            if len(embedder.calls) == 2:
                response = await db_client.delete(
                    f"{API}/documents/{document['id']}", headers=auth(workspace["owner"])
                )
                assert response.status_code == 204
            return vectors

        embedder.embed = delete_after_first_batch  # type: ignore[method-assign]

        await process(document["id"])  # must not raise or resurrect anything

        assert await chunk_total(db_engine) == 0
        # It stopped as soon as it noticed, instead of embedding the rest of a deleted document.
        assert len(embedder.calls) <= 3

    async def test_documents_processed_before_search_existed_are_reindexed_on_request(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        session_factory: async_sessionmaker,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        indexed = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["a"])
        )
        legacy = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["b"])
        )
        await process(indexed["id"])
        await set_status(db_engine, legacy["id"], "ready", timedelta(0))  # ready, but has no chunks

        without_flag, with_flag = RecordingQueue(), RecordingQueue()
        await recover_interrupted(session_factory, without_flag)
        await recover_interrupted(session_factory, with_flag, reindex_unchunked=True)

        assert without_flag.enqueued == []  # untouched when embeddings are not configured
        assert with_flag.enqueued == [uuid.UUID(legacy["id"])]

        await process(legacy["id"])
        assert len(await chunks(db_engine, legacy["id"])) == 1


class TestCascadesAndConstraints:
    async def insert(
        self, engine: AsyncEngine, document: str, workspace: str, index: int, vector: list[float]
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO document_chunks (document_id, workspace_id, chunk_index, content, "
                    "token_count, embedding) VALUES (:d, :w, :i, 'x', 1, :e)"
                ),
                {"d": uuid.UUID(document), "w": uuid.UUID(workspace), "i": index, "e": str(vector)},
            )

    async def test_deleting_a_document_removes_its_chunks(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        kept = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["keep"])
        )
        gone = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["drop"])
        )
        await process(kept["id"])
        await process(gone["id"])

        await db_client.delete(f"{API}/documents/{gone['id']}", headers=auth(workspace["owner"]))

        assert await chunks(db_engine, gone["id"]) == []
        assert len(await chunks(db_engine, kept["id"])) == 1

    async def test_deleting_a_workspace_removes_its_chunks(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        await process(document["id"])

        await db_client.delete(f"{WORKSPACES}/{workspace['id']}", headers=auth(workspace["owner"]))

        assert await chunk_total(db_engine) == 0

    async def test_a_chunk_cannot_claim_a_workspace_its_document_is_not_in(
        self, db_client: AsyncClient, db_engine: AsyncEngine, workspace: dict
    ) -> None:
        other = await create_workspace(db_client, uuid.uuid4(), "Other tenant")
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        with pytest.raises(
            IntegrityError, match="fk_document_chunks_document_id_workspace_id_documents"
        ):
            await self.insert(db_engine, document["id"], other, 0, fake_vector("x"))

    async def test_two_chunks_cannot_share_a_position(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])
        await process(document["id"])

        with pytest.raises(IntegrityError, match="uq_document_chunks_document_id_chunk_index"):
            await self.insert(db_engine, document["id"], workspace["id"], 0, fake_vector("x"))

    async def test_a_vector_of_the_wrong_size_is_refused_by_the_database(
        self, db_client: AsyncClient, db_engine: AsyncEngine, workspace: dict
    ) -> None:
        document = await upload_ok(db_client, workspace["id"], workspace["owner"])

        with pytest.raises(DBAPIError, match="expected 1536 dimensions"):
            await self.insert(db_engine, document["id"], workspace["id"], 0, [0.1] * 512)

    async def test_the_similarity_index_exists_and_uses_cosine_distance(
        self, db_engine: AsyncEngine
    ) -> None:
        async with db_engine.connect() as connection:
            definition = await connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_document_chunks_embedding_hnsw'"
                )
            )
        assert "USING hnsw" in definition and "vector_cosine_ops" in definition


class TestRecoveryAfterAKeyIsAdded:
    async def test_documents_that_failed_for_lack_of_a_key_are_retried_once_one_is_set(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        storage: FakeStorage,
        session_factory: async_sessionmaker,
        workspace: dict,
        process,  # noqa: ANN001
    ) -> None:
        def no_key():  # noqa: ANN202
            raise AppError(503, "embeddings_not_configured", "Search is not configured.")

        waiting_for_key = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["needs a key"])
        )
        scanned = await upload_ok(
            db_client, workspace["id"], workspace["owner"], content=make_pdf(["", ""])
        )
        await process_document(
            uuid.UUID(waiting_for_key["id"]), session_factory, lambda: storage, no_key
        )
        await process(scanned["id"])  # fails for a real reason: no text in the file

        still_no_key, key_added = RecordingQueue(), RecordingQueue()
        await recover_interrupted(session_factory, still_no_key)
        await recover_interrupted(session_factory, key_added, reindex_unchunked=True)

        assert still_no_key.enqueued == []
        # Only the document that was waiting for a key; a genuinely bad file is left failed.
        assert key_added.enqueued == [uuid.UUID(waiting_for_key["id"])]

        await process(waiting_for_key["id"])
        after = await fetch(db_client, waiting_for_key["id"], workspace["owner"])
        assert (after["status"], after["error"]) == ("ready", None)
        assert len(await chunks(db_engine, waiting_for_key["id"])) == 1
        assert (await fetch(db_client, scanned["id"], workspace["owner"]))["status"] == "failed"


def test_outgoing_request_urls_are_not_logged_at_info() -> None:
    """Storage URLs contain file names, so the HTTP client must not log each request."""
    import logging

    from app.core.logging import configure_logging

    configure_logging("INFO")

    for name in ("httpx", "httpx2", "httpcore", "openai"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
