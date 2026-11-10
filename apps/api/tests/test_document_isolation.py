"""Cross-tenant isolation for documents: one workspace's files are unreachable from another."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.auth import auth
from tests.fakes import FakeStorage, RecordingQueue
from tests.files import PDF_MIME, make_pdf
from tests.test_documents import API, upload, upload_ok
from tests.test_workspaces import WORKSPACES, create_workspace


@pytest.fixture
async def tenants(db_client: AsyncClient) -> dict:
    """Alice and Bob, each owning a workspace; Bob's holds one document."""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    alices = await create_workspace(db_client, alice, "Alice's")
    bobs = await create_workspace(db_client, bob, "Bob's")
    document = await upload_ok(
        db_client, bobs, bob, name="bobs-contract.pdf", content=make_pdf(["Bob's secret terms"])
    )
    return {"alice": alice, "bob": bob, "alices": alices, "bobs": bobs, "document": document}


async def test_user_a_cannot_upload_to_user_bs_workspace(
    db_client: AsyncClient, storage: FakeStorage, queue: RecordingQueue, tenants: dict
) -> None:
    before = dict(storage.objects)

    response = await upload(db_client, tenants["bobs"], tenants["alice"], "intruder.pdf")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"
    assert storage.objects == before  # nothing was stored
    assert len(queue.enqueued) == 1  # only Bob's own upload


async def test_an_outsiders_upload_body_is_not_even_read(
    db_client: AsyncClient, tenants: dict
) -> None:
    received = 0

    async def body():  # noqa: ANN202
        nonlocal received
        for _ in range(20):
            received += 1
            yield b"x" * (1024 * 1024)

    response = await db_client.post(
        f"{WORKSPACES}/{tenants['bobs']}/documents",
        content=body(),
        headers={**auth(tenants["alice"]), "Content-Type": "multipart/form-data; boundary=b"},
    )

    # Membership is checked before the body is read, so an outsider cannot make the
    # server take in megabytes of data.
    assert response.status_code == 404
    assert received <= 1


async def test_user_a_cannot_view_list_or_poll_user_bs_document(
    db_client: AsyncClient, tenants: dict
) -> None:
    document_id = tenants["document"]["id"]
    as_alice = auth(tenants["alice"])

    attempts = [
        await db_client.get(f"{API}/documents/{document_id}", headers=as_alice),
        await db_client.get(f"{API}/documents/{document_id}/status", headers=as_alice),
        await db_client.get(f"{WORKSPACES}/{tenants['bobs']}/documents", headers=as_alice),
    ]

    # 404 each time, exactly as for an ID that does not exist.
    assert [response.status_code for response in attempts] == [404, 404, 404]
    assert "bobs-contract" not in "".join(response.text for response in attempts)


async def test_user_a_cannot_download_user_bs_file(
    db_client: AsyncClient, storage: FakeStorage, tenants: dict
) -> None:
    response = await db_client.get(
        f"{API}/documents/{tenants['document']['id']}/download", headers=auth(tenants["alice"])
    )

    assert response.status_code == 404
    assert "url" not in response.text
    assert storage.signed == []  # no link was ever created


async def test_user_a_cannot_delete_or_retry_user_bs_document(
    db_client: AsyncClient, storage: FakeStorage, tenants: dict
) -> None:
    document_id = tenants["document"]["id"]
    as_alice = auth(tenants["alice"])

    deleted = await db_client.delete(f"{API}/documents/{document_id}", headers=as_alice)
    retried = await db_client.post(f"{API}/documents/{document_id}/retry", headers=as_alice)

    assert (deleted.status_code, retried.status_code) == (404, 404)
    assert len(storage.objects) == 1 and storage.deleted == []
    intact = await db_client.get(f"{API}/documents/{document_id}", headers=auth(tenants["bob"]))
    assert intact.status_code == 200


async def test_being_an_owner_elsewhere_grants_nothing_here(
    db_client: AsyncClient, tenants: dict
) -> None:
    # Alice owns her own workspace and has a document in it; that gives her no rights in Bob's.
    mine = await upload_ok(db_client, tenants["alices"], tenants["alice"])

    response = await db_client.delete(
        f"{API}/documents/{tenants['document']['id']}", headers=auth(tenants["alice"])
    )
    alices_list = await db_client.get(
        f"{WORKSPACES}/{tenants['alices']}/documents", headers=auth(tenants["alice"])
    )

    assert response.status_code == 404
    assert [item["id"] for item in alices_list.json()["items"]] == [mine["id"]]


async def test_documents_of_one_workspace_never_appear_in_anothers_listing(
    db_client: AsyncClient, tenants: dict
) -> None:
    # Alice joins Bob's workspace as a member; her own workspace's listing must not change.
    await db_client.post(
        f"{WORKSPACES}/{tenants['bobs']}/members",
        json={"user_id": str(tenants["alice"]), "role": "member"},
        headers=auth(tenants["bob"]),
    )

    alices = await db_client.get(
        f"{WORKSPACES}/{tenants['alices']}/documents", headers=auth(tenants["alice"])
    )
    bobs = await db_client.get(
        f"{WORKSPACES}/{tenants['bobs']}/documents", headers=auth(tenants["alice"])
    )

    assert alices.json()["items"] == []
    assert [item["id"] for item in bobs.json()["items"]] == [tenants["document"]["id"]]


async def test_a_download_link_is_only_ever_signed_for_the_documents_own_path(
    db_client: AsyncClient, storage: FakeStorage, tenants: dict
) -> None:
    mine = await upload_ok(db_client, tenants["alices"], tenants["alice"])

    await db_client.get(f"{API}/documents/{mine['id']}/download", headers=auth(tenants["alice"]))

    ((path, _ttl, _name),) = storage.signed
    assert path == f"{tenants['alices']}/{mine['id']}/report.pdf"
    assert tenants["bobs"] not in path


class TestStoragePathCannotBeManipulated:
    """The database refuses a document row that points outside its own folder."""

    async def set_path(self, engine: AsyncEngine, document_id: str, path: str) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE documents SET storage_path = :path WHERE id = :id"),
                {"path": path, "id": uuid.UUID(document_id)},
            )

    @pytest.mark.parametrize(
        "template",
        [
            "{bobs}/{bobs_document}/bobs-contract.pdf",  # another tenant's file
            "{alices}/{bobs_document}/x.pdf",  # right workspace, another document's folder
            "{bobs}/{mine}/x.pdf",  # right document, another workspace's folder
            "x.pdf",
            "../{alices}/{mine}/x.pdf",
        ],
    )
    async def test_row_cannot_be_repointed_at_another_location(
        self, db_client: AsyncClient, db_engine: AsyncEngine, tenants: dict, template: str
    ) -> None:
        mine = await upload_ok(db_client, tenants["alices"], tenants["alice"])
        path = template.format(
            alices=tenants["alices"],
            bobs=tenants["bobs"],
            mine=mine["id"],
            bobs_document=tenants["document"]["id"],
        )

        with pytest.raises(IntegrityError, match="storage_path"):
            await self.set_path(db_engine, mine["id"], path)

    async def test_two_documents_cannot_share_one_file(
        self, db_client: AsyncClient, db_engine: AsyncEngine, tenants: dict
    ) -> None:
        async with db_engine.connect() as connection:
            path = await connection.scalar(
                text("SELECT storage_path FROM documents WHERE id = :id"),
                {"id": uuid.UUID(tenants["document"]["id"])},
            )

        with pytest.raises(IntegrityError):
            async with db_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO documents (workspace_id, uploaded_by, filename, storage_path, "
                        "mime_type, size_bytes, sha256) VALUES (:w, :u, 'x.pdf', :p, :m, 1, :h)"
                    ),
                    {
                        "w": uuid.UUID(tenants["bobs"]),
                        "u": tenants["bob"],
                        "p": path,
                        "m": PDF_MIME,
                        "h": "0" * 64,
                    },
                )

    @pytest.mark.parametrize(
        ("column", "value", "constraint"),
        [
            ("status", "'deleted'", "ck_documents_status_valid"),
            ("size_bytes", "0", "ck_documents_size_bytes_range"),
            ("size_bytes", str(25 * 1024 * 1024 + 1), "ck_documents_size_bytes_range"),
        ],
    )
    async def test_invalid_status_or_size_is_refused_by_the_database(
        self, db_engine: AsyncEngine, tenants: dict, column: str, value: str, constraint: str
    ) -> None:
        with pytest.raises(IntegrityError, match=constraint):
            async with db_engine.begin() as connection:
                await connection.execute(
                    text(f"UPDATE documents SET {column} = {value} WHERE id = :id"),
                    {"id": uuid.UUID(tenants["document"]["id"])},
                )
