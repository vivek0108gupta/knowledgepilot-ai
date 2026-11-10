"""Document upload, listing, download and deletion against the real test database."""

import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.auth import auth
from tests.fakes import FakeStorage, RecordingQueue
from tests.files import DOCX_MIME, EXE_BYTES, PDF_MIME, TXT_MIME, make_docx, make_pdf
from tests.test_workspaces import WORKSPACES, add_member, create_workspace

API = "/api/v1"
MAX_BYTES = 25 * 1024 * 1024


async def upload(
    client: AsyncClient,
    workspace: str,
    user: uuid.UUID,
    name: str = "report.pdf",
    content: bytes | None = None,
    content_type: str = PDF_MIME,
) -> Response:
    content = make_pdf(["Hello"]) if content is None else content
    return await client.post(
        f"{WORKSPACES}/{workspace}/documents",
        files={"file": (name, content, content_type)},
        headers=auth(user),
    )


async def upload_ok(client: AsyncClient, workspace: str, user: uuid.UUID, **kwargs) -> dict:  # noqa: ANN003
    response = await upload(client, workspace, user, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def team(db_client: AsyncClient) -> dict:
    """A workspace with one owner, one admin and two regular members."""
    owner, admin, member, other = (uuid.uuid4() for _ in range(4))
    workspace = await create_workspace(db_client, owner)
    await add_member(db_client, workspace, owner, admin, "admin")
    await add_member(db_client, workspace, owner, member, "member")
    await add_member(db_client, workspace, owner, other, "member")
    return {
        "workspace": workspace,
        "owner": owner,
        "admin": admin,
        "member": member,
        "other": other,
    }


class TestUpload:
    @pytest.mark.parametrize(
        ("name", "content", "mime"),
        [
            ("report.pdf", make_pdf(["Hello"]), PDF_MIME),
            ("notes.docx", make_docx(), DOCX_MIME),
            ("readme.txt", b"Plain text.\n", TXT_MIME),
        ],
    )
    async def test_stores_the_file_and_records_it(
        self,
        db_client: AsyncClient,
        storage: FakeStorage,
        queue: RecordingQueue,
        team: dict,
        name: str,
        content: bytes,
        mime: str,
    ) -> None:
        response = await upload(db_client, team["workspace"], team["member"], name, content, mime)

        assert response.status_code == 201
        body = response.json()
        assert body["filename"] == name
        assert body["mime_type"] == mime
        assert body["size_bytes"] == len(content)
        assert body["status"] == "uploaded"
        assert body["uploaded_by"] == str(team["member"])
        assert body["workspace_id"] == team["workspace"]
        assert (body["page_count"], body["error"]) == (None, None)

        # One object, at workspace_id/document_id/filename, holding exactly the bytes sent.
        assert storage.objects == {f"{team['workspace']}/{body['id']}/{name}": content}
        assert storage.content_types == {f"{team['workspace']}/{body['id']}/{name}": mime}
        assert queue.enqueued == [uuid.UUID(body["id"])]

    async def test_response_never_reveals_the_storage_path_or_hash(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        body = await upload_ok(db_client, team["workspace"], team["owner"])

        assert "storage_path" not in body
        assert "sha256" not in body

    @pytest.mark.parametrize("role", ["owner", "admin", "member"])
    async def test_every_role_can_upload(
        self, db_client: AsyncClient, team: dict, role: str
    ) -> None:
        response = await upload(db_client, team["workspace"], team[role])

        assert response.status_code == 201

    async def test_requires_authentication(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/documents",
            files={"file": ("a.pdf", make_pdf(["x"]), PDF_MIME)},
        )

        assert response.status_code == 401
        assert storage.objects == {}

    async def test_malicious_extension_is_rejected_and_nothing_is_stored(
        self, db_client: AsyncClient, storage: FakeStorage, queue: RecordingQueue, team: dict
    ) -> None:
        response = await upload(
            db_client, team["workspace"], team["owner"], "malware.exe", EXE_BYTES, PDF_MIME
        )

        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_file_type"
        assert (storage.objects, queue.enqueued) == ({}, [])
        listing = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/documents", headers=auth(team["owner"])
        )
        assert listing.json()["total"] == 0

    async def test_mime_spoofing_is_rejected(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        # An executable, named and labelled as a PDF.
        response = await upload(
            db_client, team["workspace"], team["owner"], "invoice.pdf", EXE_BYTES, PDF_MIME
        )

        assert response.status_code == 415
        assert response.json()["error"]["code"] == "content_mismatch"
        assert storage.objects == {}

    async def test_contradicting_content_type_is_rejected(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await upload(
            db_client, team["workspace"], team["owner"], "a.pdf", make_pdf(["x"]), "text/html"
        )

        assert response.status_code == 415
        assert response.json()["error"]["code"] == "mime_mismatch"

    async def test_empty_file_is_rejected(self, db_client: AsyncClient, team: dict) -> None:
        response = await upload(db_client, team["workspace"], team["owner"], "empty.pdf", b"")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_file"

    async def test_missing_file_field(self, db_client: AsyncClient, team: dict) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/documents",
            files={"attachment": ("a.pdf", make_pdf(["x"]), PDF_MIME)},
            headers=auth(team["owner"]),
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "missing_file"

    async def test_non_multipart_body(self, db_client: AsyncClient, team: dict) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/documents",
            json={"file": "not a file"},
            headers=auth(team["owner"]),
        )

        assert response.status_code == 415
        assert response.json()["error"]["code"] == "invalid_upload"

    async def test_path_traversal_in_the_filename_cannot_move_the_file(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        other_workspace = await create_workspace(db_client, uuid.uuid4(), "Victim")

        body = await upload_ok(
            db_client,
            team["workspace"],
            team["owner"],
            name=f"../../{other_workspace}/stolen/../../evil.pdf",
        )

        assert body["filename"] == "evil.pdf"
        (path,) = storage.objects
        assert path == f"{team['workspace']}/{body['id']}/evil.pdf"
        assert other_workspace not in path and ".." not in path

    async def test_unicode_filename_is_kept_for_display_but_stored_as_ascii(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        body = await upload_ok(db_client, team["workspace"], team["owner"], name="résumé 報告.pdf")

        assert body["filename"] == "résumé 報告.pdf"
        (path,) = storage.objects
        assert path.isascii()
        assert path.endswith(".pdf")

    async def test_workspace_id_in_the_form_cannot_redirect_the_upload(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/documents",
            files={"file": ("a.pdf", make_pdf(["x"]), PDF_MIME)},
            data={"workspace_id": str(uuid.uuid4()), "uploaded_by": str(uuid.uuid4())},
            headers=auth(team["owner"]),
        )

        # Extra fields are refused outright rather than read.
        assert response.status_code == 400


class TestUploadSizeLimit:
    async def test_declared_oversize_is_refused(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        response = await upload(
            db_client, team["workspace"], team["owner"], "big.txt", b"a" * (MAX_BYTES + 1), TXT_MIME
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
        assert storage.objects == {}

    async def test_oversize_is_caught_while_streaming_when_no_length_is_declared(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        boundary = "test-boundary"
        sent = 0

        async def body():  # noqa: ANN202
            nonlocal sent
            yield (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode()
            chunk = b"a" * (1024 * 1024)
            for _ in range(40):  # 40 MB if nothing stops it
                sent += len(chunk)
                yield chunk
            yield f"\r\n--{boundary}--\r\n".encode()

        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/documents",
            content=body(),
            headers={
                **auth(team["owner"]),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
        assert storage.objects == {}
        # The server stopped reading soon after the limit instead of taking all 40 MB.
        assert sent <= MAX_BYTES + 2 * 1024 * 1024

    async def test_a_false_content_length_does_not_bypass_the_limit(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/documents",
            content=b"x",
            headers={
                **auth(team["owner"]),
                "Content-Type": "multipart/form-data; boundary=b",
                "Content-Length": "not-a-number",
            },
        )

        assert response.status_code == 413

    async def test_file_at_exactly_25_mb_is_accepted(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        response = await upload(
            db_client, team["workspace"], team["owner"], "max.txt", b"a" * MAX_BYTES, TXT_MIME
        )

        assert response.status_code == 201
        assert response.json()["size_bytes"] == MAX_BYTES


class TestDuplicates:
    async def test_same_content_twice_in_one_workspace_is_refused(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        content = make_pdf(["Same"])
        await upload_ok(
            db_client, team["workspace"], team["owner"], name="first.pdf", content=content
        )

        response = await upload(
            db_client, team["workspace"], team["member"], "renamed-copy.pdf", content
        )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "duplicate_document"
        assert "first.pdf" in error["message"]
        assert len(storage.objects) == 1

    async def test_same_name_with_different_content_is_allowed(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        first = await upload_ok(
            db_client, team["workspace"], team["owner"], content=make_pdf(["v1"])
        )
        second = await upload_ok(
            db_client, team["workspace"], team["owner"], content=make_pdf(["v2"])
        )

        assert first["filename"] == second["filename"] == "report.pdf"
        assert first["id"] != second["id"]
        assert len(storage.objects) == 2  # separate paths: neither overwrote the other

    async def test_same_content_in_another_workspace_is_allowed(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        content = make_pdf(["Shared"])
        other_workspace = await create_workspace(db_client, team["owner"], "Second")
        await upload_ok(db_client, team["workspace"], team["owner"], content=content)

        response = await upload(db_client, other_workspace, team["owner"], content=content)

        assert response.status_code == 201

    async def test_a_deleted_file_can_be_uploaded_again(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        content = make_pdf(["Again"])
        first = await upload_ok(db_client, team["workspace"], team["owner"], content=content)
        await db_client.delete(f"{API}/documents/{first['id']}", headers=auth(team["owner"]))

        response = await upload(db_client, team["workspace"], team["owner"], content=content)

        assert response.status_code == 201


class TestStorageFailures:
    async def test_failed_upload_leaves_no_database_record(
        self, db_client: AsyncClient, storage: FakeStorage, queue: RecordingQueue, team: dict
    ) -> None:
        storage.fail_upload = True

        response = await upload(db_client, team["workspace"], team["owner"])

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert queue.enqueued == []
        listing = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/documents", headers=auth(team["owner"])
        )
        assert listing.json()["total"] == 0

    async def test_the_same_file_can_be_retried_after_a_failed_upload(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        storage.fail_upload = True
        await upload(db_client, team["workspace"], team["owner"])
        storage.fail_upload = False

        response = await upload(db_client, team["workspace"], team["owner"])

        assert response.status_code == 201

    async def test_failed_storage_delete_keeps_the_record_so_it_can_be_retried(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])
        url = f"{API}/documents/{document['id']}"
        storage.fail_delete = True

        failed = await db_client.delete(url, headers=auth(team["owner"]))
        still_there = await db_client.get(url, headers=auth(team["owner"]))
        storage.fail_delete = False
        retried = await db_client.delete(url, headers=auth(team["owner"]))

        assert failed.status_code == 502
        assert still_there.status_code == 200
        assert retried.status_code == 204
        assert storage.objects == {}

    async def test_deleting_when_the_file_is_already_gone_still_removes_the_record(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])
        storage.objects.clear()  # the file vanished from storage on its own

        response = await db_client.delete(
            f"{API}/documents/{document['id']}", headers=auth(team["owner"])
        )

        assert response.status_code == 204

    async def test_failed_signing_is_a_502(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])
        storage.fail_sign = True

        response = await db_client.get(
            f"{API}/documents/{document['id']}/download", headers=auth(team["owner"])
        )

        assert response.status_code == 502


class TestListing:
    async def test_newest_first_with_pagination(self, db_client: AsyncClient, team: dict) -> None:
        ids = [
            (
                await upload_ok(
                    db_client, team["workspace"], team["owner"], content=make_pdf([f"doc {n}"])
                )
            )["id"]
            for n in range(5)
        ]
        url = f"{WORKSPACES}/{team['workspace']}/documents"

        first = (await db_client.get(url, params={"limit": 2}, headers=auth(team["member"]))).json()
        second = (
            await db_client.get(url, params={"limit": 2, "offset": 2}, headers=auth(team["member"]))
        ).json()
        last = (
            await db_client.get(url, params={"limit": 2, "offset": 4}, headers=auth(team["member"]))
        ).json()

        assert (first["total"], first["limit"], first["offset"]) == (5, 2, 0)
        seen = [d["id"] for page in (first, second, last) for d in page["items"]]
        assert seen == list(reversed(ids))  # every document once, newest first

    async def test_offset_past_the_end_is_an_empty_page(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        await upload_ok(db_client, team["workspace"], team["owner"])

        body = (
            await db_client.get(
                f"{WORKSPACES}/{team['workspace']}/documents",
                params={"offset": 50},
                headers=auth(team["owner"]),
            )
        ).json()

        assert (body["items"], body["total"]) == ([], 1)

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "deleted"}]
    )
    async def test_invalid_query_parameters(
        self, db_client: AsyncClient, team: dict, params: dict
    ) -> None:
        response = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/documents",
            params=params,
            headers=auth(team["owner"]),
        )

        assert response.status_code == 422

    async def test_filter_by_status(
        self,
        db_client: AsyncClient,
        team: dict,
        process,  # noqa: ANN001
    ) -> None:
        ready = await upload_ok(
            db_client, team["workspace"], team["owner"], content=make_pdf(["ok"])
        )
        failed = await upload_ok(
            db_client, team["workspace"], team["owner"], content=make_pdf([""])
        )
        waiting = await upload_ok(
            db_client, team["workspace"], team["owner"], content=make_pdf(["w"])
        )
        await process(ready["id"])
        await process(failed["id"])
        url = f"{WORKSPACES}/{team['workspace']}/documents"

        async def ids(status: str) -> list[str]:
            body = (
                await db_client.get(url, params={"status": status}, headers=auth(team["owner"]))
            ).json()
            assert body["total"] == len(body["items"])
            return [item["id"] for item in body["items"]]

        assert await ids("ready") == [ready["id"]]
        assert await ids("failed") == [failed["id"]]
        assert await ids("uploaded") == [waiting["id"]]
        assert await ids("processing") == []

    async def test_only_this_workspaces_documents_are_listed(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        other_workspace = await create_workspace(db_client, team["owner"], "Second")
        mine = await upload_ok(db_client, team["workspace"], team["owner"], content=make_pdf(["a"]))
        await upload_ok(db_client, other_workspace, team["owner"], content=make_pdf(["b"]))

        body = (
            await db_client.get(
                f"{WORKSPACES}/{team['workspace']}/documents", headers=auth(team["owner"])
            )
        ).json()

        assert [item["id"] for item in body["items"]] == [mine["id"]]


class TestReadAndDownload:
    async def test_get_and_status(self, db_client: AsyncClient, team: dict) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])

        detail = await db_client.get(
            f"{API}/documents/{document['id']}", headers=auth(team["member"])
        )
        status = await db_client.get(
            f"{API}/documents/{document['id']}/status", headers=auth(team["member"])
        )

        assert detail.json() == document
        assert status.json() == {
            "id": document["id"],
            "status": "uploaded",
            "page_count": None,
            "error": None,
            "updated_at": document["updated_at"],
        }

    @pytest.mark.parametrize("role", ["owner", "admin", "member", "other"])
    async def test_every_member_can_download(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict, role: str
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])

        response = await db_client.get(
            f"{API}/documents/{document['id']}/download", headers=auth(team[role])
        )

        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "report.pdf"
        assert "token=" in body["url"]
        assert storage.signed == [
            (f"{team['workspace']}/{document['id']}/report.pdf", 60, "report.pdf")
        ]

    async def test_download_link_is_short_lived_and_not_cacheable(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])

        response = await db_client.get(
            f"{API}/documents/{document['id']}/download", headers=auth(team["owner"])
        )

        # The link is asked for with a 60-second lifetime, and the client is told so.
        assert response.json()["expires_in"] == 60
        assert storage.signed[0][1] == 60
        assert response.headers["cache-control"] == "no-store"

    async def test_unknown_document_is_404(self, db_client: AsyncClient, team: dict) -> None:
        for suffix in ("", "/status", "/download"):
            response = await db_client.get(
                f"{API}/documents/{uuid.uuid4()}{suffix}", headers=auth(team["owner"])
            )
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "document_not_found"

    async def test_malformed_document_id_is_422(self, db_client: AsyncClient, team: dict) -> None:
        response = await db_client.get(
            f"{API}/documents/../../etc/passwd", headers=auth(team["owner"])
        )

        assert response.status_code in (404, 422)


class TestDeletePermissions:
    async def test_member_can_delete_their_own_upload(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["member"])

        response = await db_client.delete(
            f"{API}/documents/{document['id']}", headers=auth(team["member"])
        )

        assert response.status_code == 204
        assert storage.objects == {}

    async def test_member_cannot_delete_someone_elses_upload(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["member"])

        response = await db_client.delete(
            f"{API}/documents/{document['id']}", headers=auth(team["other"])
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert len(storage.objects) == 1

    @pytest.mark.parametrize("role", ["owner", "admin"])
    async def test_owner_and_admin_can_delete_any_upload(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict, role: str
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["member"])

        response = await db_client.delete(
            f"{API}/documents/{document['id']}", headers=auth(team[role])
        )

        assert response.status_code == 204
        assert storage.objects == {}
        gone = await db_client.get(f"{API}/documents/{document['id']}", headers=auth(team[role]))
        assert gone.status_code == 404

    async def test_a_removed_member_can_no_longer_reach_their_own_upload(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["member"])
        await db_client.delete(
            f"{WORKSPACES}/{team['workspace']}/members/{team['member']}",
            headers=auth(team["owner"]),
        )

        for method, suffix in [("GET", ""), ("GET", "/download"), ("DELETE", "")]:
            response = await db_client.request(
                method, f"{API}/documents/{document['id']}{suffix}", headers=auth(team["member"])
            )
            assert response.status_code == 404

    async def test_delete_removes_the_extracted_segments_too(
        self,
        db_client: AsyncClient,
        db_engine: AsyncEngine,
        team: dict,
        process,  # noqa: ANN001
    ) -> None:
        document = await upload_ok(db_client, team["workspace"], team["owner"])
        await process(document["id"])

        await db_client.delete(f"{API}/documents/{document['id']}", headers=auth(team["owner"]))

        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM document_segments")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM documents")) == 0


class TestWorkspaceDeletion:
    async def test_deleting_a_workspace_removes_its_documents_and_their_files(
        self, db_client: AsyncClient, storage: FakeStorage, db_engine: AsyncEngine, team: dict
    ) -> None:
        other_workspace = await create_workspace(db_client, team["owner"], "Kept")
        await upload_ok(db_client, team["workspace"], team["owner"], content=make_pdf(["a"]))
        await upload_ok(db_client, team["workspace"], team["member"], content=make_pdf(["b"]))
        kept = await upload_ok(db_client, other_workspace, team["owner"], content=make_pdf(["c"]))

        response = await db_client.delete(
            f"{WORKSPACES}/{team['workspace']}", headers=auth(team["owner"])
        )

        assert response.status_code == 204
        assert list(storage.objects) == [f"{other_workspace}/{kept['id']}/report.pdf"]
        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM documents")) == 1

    async def test_workspace_is_still_deleted_if_storage_cleanup_fails(
        self, db_client: AsyncClient, storage: FakeStorage, team: dict
    ) -> None:
        await upload_ok(db_client, team["workspace"], team["owner"])
        storage.fail_delete = True

        response = await db_client.delete(
            f"{WORKSPACES}/{team['workspace']}", headers=auth(team["owner"])
        )

        assert response.status_code == 204
        gone = await db_client.get(f"{WORKSPACES}/{team['workspace']}", headers=auth(team["owner"]))
        assert gone.status_code == 404
