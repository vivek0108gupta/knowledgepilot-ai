"""Conversation management and its authorization, against the real test database."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.conversations import title_from_message
from tests.auth import auth
from tests.test_workspaces import WORKSPACES, add_member, create_workspace

API = "/api/v1"


async def create_conversation(
    client: AsyncClient, workspace: str, user: uuid.UUID, title: str | None = None
) -> dict:
    body = {} if title is None else {"title": title}
    response = await client.post(
        f"{WORKSPACES}/{workspace}/conversations", json=body, headers=auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def send(
    client: AsyncClient, conversation: str, user: uuid.UUID, content: str, **body: object
) -> Response:
    return await client.post(
        f"{API}/conversations/{conversation}/messages",
        json={"content": content, **body},
        headers=auth(user),
    )


async def listing(client: AsyncClient, workspace: str, user: uuid.UUID, **params: object) -> dict:
    response = await client.get(
        f"{WORKSPACES}/{workspace}/conversations", params=params, headers=auth(user)
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def team(db_client: AsyncClient) -> dict:
    """One workspace shared by Alice (owner) and Bob (member), plus Carol who is outside it."""
    alice, bob, carol = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = await create_workspace(db_client, alice)
    await add_member(db_client, workspace, alice, bob, "member")
    return {"workspace": workspace, "alice": alice, "bob": bob, "carol": carol}


class TestCreate:
    async def test_creates_a_private_active_conversation_with_the_default_title(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/conversations", json={}, headers=auth(team["alice"])
        )

        assert response.status_code == 201
        body = response.json()
        assert body["title"] == "New conversation"
        assert body["status"] == "active"
        assert body["workspace_id"] == team["workspace"]
        assert body["message_count"] == 0
        assert body["created_at"] == body["updated_at"]
        assert body["created_at"].endswith(("Z", "+00:00"))  # UTC
        assert set(body) == {
            "id",
            "workspace_id",
            "title",
            "status",
            "message_count",
            "created_at",
            "updated_at",
        }

    async def test_a_request_with_no_body_at_all_is_accepted(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/conversations", headers=auth(team["alice"])
        )

        assert response.status_code == 201
        assert response.json()["title"] == "New conversation"

    async def test_a_chosen_title_is_trimmed_and_kept(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        body = await create_conversation(
            db_client, team["workspace"], team["alice"], "  Refund policy  "
        )

        assert body["title"] == "Refund policy"

    @pytest.mark.parametrize("title", ["", "   ", "x" * 121, "bad\x00title", 5, None], ids=str)
    async def test_invalid_titles_are_rejected(
        self, db_client: AsyncClient, team: dict, title: object
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/conversations",
            json={"title": title},
            headers=auth(team["alice"]),
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_a_title_of_exactly_120_characters_is_accepted(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        body = await create_conversation(db_client, team["workspace"], team["alice"], "x" * 120)

        assert len(body["title"]) == 120

    @pytest.mark.parametrize(
        "extra",
        [
            {"created_by": str(uuid.uuid4())},
            {"workspace_id": str(uuid.uuid4())},
            {"status": "archived"},
            {"id": str(uuid.uuid4())},
        ],
        ids=["created_by", "workspace_id", "status", "id"],
    )
    async def test_fields_the_server_controls_cannot_be_sent(
        self, db_client: AsyncClient, team: dict, extra: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/conversations",
            json={"title": "Mine", **extra},
            headers=auth(team["alice"]),
        )

        assert response.status_code == 422

    async def test_every_member_role_can_create(self, db_client: AsyncClient, team: dict) -> None:
        assert (await create_conversation(db_client, team["workspace"], team["bob"]))["id"]

    async def test_a_non_member_cannot_create(
        self, db_client: AsyncClient, db_engine: AsyncEngine, team: dict
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{team['workspace']}/conversations", json={}, headers=auth(team["carol"])
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "workspace_not_found"
        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM conversations")) == 0

    @pytest.mark.parametrize("workspace", [str(uuid.uuid4()), "not-a-uuid"])
    async def test_an_unknown_or_malformed_workspace_is_rejected(
        self, db_client: AsyncClient, team: dict, workspace: str
    ) -> None:
        response = await db_client.post(
            f"{WORKSPACES}/{workspace}/conversations", json={}, headers=auth(team["alice"])
        )

        assert response.status_code in (404, 422)


class TestListAndGet:
    async def test_lists_only_my_conversations_most_recently_active_first(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        first = await create_conversation(db_client, team["workspace"], team["alice"], "First")
        second = await create_conversation(db_client, team["workspace"], team["alice"], "Second")
        third = await create_conversation(db_client, team["workspace"], team["alice"], "Third")
        await create_conversation(db_client, team["workspace"], team["bob"], "Bob's")
        # Activity moves a conversation to the top.
        await send(db_client, first["id"], team["alice"], "hello")

        body = await listing(db_client, team["workspace"], team["alice"])

        assert [item["id"] for item in body["items"]] == [first["id"], third["id"], second["id"]]
        assert (body["total"], body["limit"], body["offset"]) == (3, 20, 0)
        assert [item["message_count"] for item in body["items"]] == [1, 0, 0]

    async def test_the_list_never_contains_message_text(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(
            db_client, team["workspace"], team["alice"], "Chosen title"
        )
        await send(db_client, conversation["id"], team["alice"], "the secret launch date is in May")

        response = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/conversations", headers=auth(team["alice"])
        )

        assert "secret launch date" not in response.text
        assert "content" not in response.json()["items"][0]

    async def test_pagination_covers_every_conversation_once(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        created = [
            (await create_conversation(db_client, team["workspace"], team["alice"], f"C{n}"))["id"]
            for n in range(5)
        ]

        pages = [
            await listing(db_client, team["workspace"], team["alice"], limit=2, offset=offset)
            for offset in (0, 2, 4, 6)
        ]

        assert [len(page["items"]) for page in pages] == [2, 2, 1, 0]
        assert all(page["total"] == 5 for page in pages)
        seen = [item["id"] for page in pages for item in page["items"]]
        assert seen == list(reversed(created))

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "deleted"}]
    )
    async def test_invalid_list_parameters(
        self, db_client: AsyncClient, team: dict, params: dict
    ) -> None:
        response = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/conversations",
            params=params,
            headers=auth(team["alice"]),
        )

        assert response.status_code == 422

    async def test_a_new_member_starts_with_an_empty_list(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        await create_conversation(db_client, team["workspace"], team["alice"])

        body = await listing(db_client, team["workspace"], team["bob"])

        assert (body["items"], body["total"]) == ([], 0)

    async def test_get_returns_metadata_without_messages(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(
            db_client, team["workspace"], team["alice"], "Notes"
        )
        await send(db_client, conversation["id"], team["alice"], "private thought")

        response = await db_client.get(
            f"{API}/conversations/{conversation['id']}", headers=auth(team["alice"])
        )

        assert response.status_code == 200
        body = response.json()
        assert (body["id"], body["title"], body["message_count"]) == (
            conversation["id"],
            "Notes",
            1,
        )
        assert "messages" not in body and "private thought" not in response.text

    async def test_get_unknown_conversation_is_404(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await db_client.get(
            f"{API}/conversations/{uuid.uuid4()}", headers=auth(team["alice"])
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"

    async def test_malformed_conversation_id_is_422(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        response = await db_client.get(
            f"{API}/conversations/not-a-uuid", headers=auth(team["alice"])
        )

        assert response.status_code == 422


class TestRename:
    async def test_rename_updates_the_title_and_the_activity_time(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])
        await asyncio.sleep(0.01)

        response = await db_client.patch(
            f"{API}/conversations/{conversation['id']}",
            json={"title": "  Refund policy discussion "},
            headers=auth(team["alice"]),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Refund policy discussion"
        assert body["updated_at"] > conversation["updated_at"]
        assert body["created_at"] == conversation["created_at"]

    @pytest.mark.parametrize("body", [{}, {"title": ""}, {"title": "  "}, {"title": "x" * 121}])
    async def test_rename_with_an_invalid_title_is_rejected_and_changes_nothing(
        self, db_client: AsyncClient, team: dict, body: dict
    ) -> None:
        conversation = await create_conversation(
            db_client, team["workspace"], team["alice"], "Keep"
        )

        response = await db_client.patch(
            f"{API}/conversations/{conversation['id']}", json=body, headers=auth(team["alice"])
        )

        assert response.status_code == 422
        current = await db_client.get(
            f"{API}/conversations/{conversation['id']}", headers=auth(team["alice"])
        )
        assert current.json()["title"] == "Keep"

    @pytest.mark.parametrize(
        "extra",
        [
            {"workspace_id": str(uuid.uuid4())},
            {"created_by": str(uuid.uuid4())},
            {"status": "archived"},
        ],
        ids=["workspace_id", "created_by", "status"],
    )
    async def test_a_conversation_cannot_be_moved_or_reassigned_through_rename(
        self, db_client: AsyncClient, db_engine: AsyncEngine, team: dict, extra: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])

        response = await db_client.patch(
            f"{API}/conversations/{conversation['id']}",
            json={"title": "Moved", **extra},
            headers=auth(team["alice"]),
        )

        assert response.status_code == 422
        async with db_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT workspace_id::text, created_by::text, status, title "
                        "FROM conversations"
                    )
                )
            ).one()
        assert tuple(row) == (team["workspace"], str(team["alice"]), "active", "New conversation")


class TestDelete:
    async def test_delete_removes_the_conversation_and_every_message(
        self, db_client: AsyncClient, db_engine: AsyncEngine, team: dict
    ) -> None:
        doomed = await create_conversation(db_client, team["workspace"], team["alice"], "Doomed")
        kept = await create_conversation(db_client, team["workspace"], team["alice"], "Kept")
        for n in range(3):
            await send(db_client, doomed["id"], team["alice"], f"doomed {n}")
        await send(db_client, kept["id"], team["alice"], "kept")

        response = await db_client.delete(
            f"{API}/conversations/{doomed['id']}", headers=auth(team["alice"])
        )

        assert response.status_code == 204
        assert response.content == b""
        gone = await db_client.get(
            f"{API}/conversations/{doomed['id']}", headers=auth(team["alice"])
        )
        assert gone.status_code == 404
        async with db_engine.connect() as connection:
            remaining = await connection.execute(text("SELECT content FROM messages"))
            assert [row[0] for row in remaining] == ["kept"]  # no orphans, nothing else touched

    async def test_deleting_twice_is_404_the_second_time(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])
        url = f"{API}/conversations/{conversation['id']}"

        first = await db_client.delete(url, headers=auth(team["alice"]))
        second = await db_client.delete(url, headers=auth(team["alice"]))

        assert (first.status_code, second.status_code) == (204, 404)

    async def test_deleting_a_workspace_removes_its_conversations_and_messages(
        self, db_client: AsyncClient, db_engine: AsyncEngine, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["bob"])
        await send(db_client, conversation["id"], team["bob"], "hello")

        await db_client.delete(f"{WORKSPACES}/{team['workspace']}", headers=auth(team["alice"]))

        async with db_engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM conversations")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM messages")) == 0


class TestArchive:
    async def test_archive_hides_from_the_default_list_and_restore_brings_it_back(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"], "Old")
        url = f"{API}/conversations/{conversation['id']}"

        archived = await db_client.post(f"{url}/archive", headers=auth(team["alice"]))
        default_list = await listing(db_client, team["workspace"], team["alice"])
        archived_list = await listing(
            db_client, team["workspace"], team["alice"], status="archived"
        )
        restored = await db_client.post(f"{url}/restore", headers=auth(team["alice"]))
        after = await listing(db_client, team["workspace"], team["alice"])

        assert (archived.status_code, archived.json()["status"]) == (200, "archived")
        assert default_list["items"] == []
        assert [item["id"] for item in archived_list["items"]] == [conversation["id"]]
        assert (restored.status_code, restored.json()["status"]) == (200, "active")
        assert [item["id"] for item in after["items"]] == [conversation["id"]]

    async def test_an_archived_conversation_keeps_its_messages_but_accepts_no_new_ones(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])
        await send(db_client, conversation["id"], team["alice"], "before archiving")
        await db_client.post(
            f"{API}/conversations/{conversation['id']}/archive", headers=auth(team["alice"])
        )

        refused = await send(db_client, conversation["id"], team["alice"], "after archiving")
        history = await db_client.get(
            f"{API}/conversations/{conversation['id']}/messages", headers=auth(team["alice"])
        )

        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "conversation_archived"
        assert [m["content"] for m in history.json()["items"]] == ["before archiving"]

    async def test_archiving_twice_is_harmless(self, db_client: AsyncClient, team: dict) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])
        url = f"{API}/conversations/{conversation['id']}/archive"

        first = await db_client.post(url, headers=auth(team["alice"]))
        second = await db_client.post(url, headers=auth(team["alice"]))

        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["updated_at"] == first.json()["updated_at"]


class TestAuthorization:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", f"{WORKSPACES}/{uuid.uuid4()}/conversations"),
            ("GET", f"{WORKSPACES}/{uuid.uuid4()}/conversations"),
            ("GET", f"{API}/conversations/{uuid.uuid4()}"),
            ("PATCH", f"{API}/conversations/{uuid.uuid4()}"),
            ("DELETE", f"{API}/conversations/{uuid.uuid4()}"),
            ("POST", f"{API}/conversations/{uuid.uuid4()}/archive"),
            ("POST", f"{API}/conversations/{uuid.uuid4()}/restore"),
            ("GET", f"{API}/conversations/{uuid.uuid4()}/messages"),
            ("POST", f"{API}/conversations/{uuid.uuid4()}/messages"),
        ],
    )
    async def test_every_endpoint_requires_authentication(
        self, db_client: AsyncClient, method: str, path: str
    ) -> None:
        response = await db_client.request(method, path, json={"title": "x", "content": "x"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    async def test_an_invalid_token_is_401(self, db_client: AsyncClient, team: dict) -> None:
        response = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/conversations",
            headers={"Authorization": "Bearer not.a.jwt"},
        )

        assert response.status_code == 401

    async def test_a_member_of_the_same_workspace_cannot_reach_my_conversation_by_any_endpoint(
        self, db_client: AsyncClient, db_engine: AsyncEngine, team: dict
    ) -> None:
        """Alice and Bob share a workspace. Bob's conversation is still invisible to Alice,
        even though she owns the workspace."""
        conversation = await create_conversation(db_client, team["workspace"], team["bob"], "Bob's")
        await send(db_client, conversation["id"], team["bob"], "Bob's private question")
        url = f"{API}/conversations/{conversation['id']}"
        as_alice = auth(team["alice"])

        attempts = [
            await db_client.get(url, headers=as_alice),
            await db_client.patch(url, json={"title": "Hijacked"}, headers=as_alice),
            await db_client.delete(url, headers=as_alice),
            await db_client.post(f"{url}/archive", headers=as_alice),
            await db_client.post(f"{url}/restore", headers=as_alice),
            await db_client.get(f"{url}/messages", headers=as_alice),
            await db_client.post(f"{url}/messages", json={"content": "injected"}, headers=as_alice),
        ]

        # 404 every time, exactly as for an ID that does not exist.
        assert [response.status_code for response in attempts] == [404] * len(attempts)
        assert {r.json()["error"]["code"] for r in attempts} == {"conversation_not_found"}
        assert "private question" not in "".join(r.text for r in attempts)
        async with db_engine.connect() as connection:
            row = (await connection.execute(text("SELECT title, status FROM conversations"))).one()
            messages = await connection.execute(text("SELECT content FROM messages"))
            assert tuple(row) == ("Bob's", "active")
            assert [m[0] for m in messages] == ["Bob's private question"]

    async def test_user_a_cannot_list_user_bs_conversations(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        await create_conversation(db_client, team["workspace"], team["bob"], "Bob's plans")
        mine = await create_conversation(db_client, team["workspace"], team["alice"], "Alice's")

        for status in ("active", "archived"):
            body = await listing(db_client, team["workspace"], team["alice"], status=status)
            assert "Bob's plans" not in str(body)
        active = await listing(db_client, team["workspace"], team["alice"])
        assert [item["id"] for item in active["items"]] == [mine["id"]]

    async def test_a_non_member_cannot_list_a_workspaces_conversations(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        await create_conversation(db_client, team["workspace"], team["alice"])

        response = await db_client.get(
            f"{WORKSPACES}/{team['workspace']}/conversations", headers=auth(team["carol"])
        )

        assert response.status_code == 404

    async def test_conversations_do_not_cross_workspaces(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        other = await create_workspace(db_client, team["alice"], "Second workspace")
        in_first = await create_conversation(
            db_client, team["workspace"], team["alice"], "In first"
        )
        in_second = await create_conversation(db_client, other, team["alice"], "In second")

        first = await listing(db_client, team["workspace"], team["alice"])
        second = await listing(db_client, other, team["alice"])

        assert [item["id"] for item in first["items"]] == [in_first["id"]]
        assert [item["id"] for item in second["items"]] == [in_second["id"]]

    async def test_leaving_a_workspace_ends_access_to_my_own_conversations_there(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["bob"])
        await send(db_client, conversation["id"], team["bob"], "while a member")
        url = f"{API}/conversations/{conversation['id']}"

        await db_client.delete(
            f"{WORKSPACES}/{team['workspace']}/members/{team['bob']}", headers=auth(team["alice"])
        )

        attempts = [
            await db_client.get(url, headers=auth(team["bob"])),
            await db_client.get(f"{url}/messages", headers=auth(team["bob"])),
            await send(db_client, conversation["id"], team["bob"], "after removal"),
            await db_client.delete(url, headers=auth(team["bob"])),
        ]
        assert [response.status_code for response in attempts] == [404, 404, 404, 404]

    async def test_isolation_does_not_depend_on_the_workspace_check_alone(
        self, db_client: AsyncClient, team: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The list query filters on the creator itself, so it holds even if the earlier
        membership check were skipped."""
        from app.services import conversations as service

        async def skipped(*_args: object, **_kwargs: object) -> None:
            return None

        await create_conversation(db_client, team["workspace"], team["bob"], "Bob's")
        monkeypatch.setattr(service.workspace_service, "get_access", skipped)

        body = await listing(db_client, team["workspace"], team["carol"])

        assert body["items"] == []


class TestDatabaseConstraints:
    async def test_status_and_title_are_checked_by_the_database(
        self, db_client: AsyncClient, db_engine: AsyncEngine, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])

        for column, value, constraint in [
            ("status", "'deleted'", "ck_conversations_status_valid"),
            ("title", "''", "ck_conversations_title_length"),
        ]:
            with pytest.raises(IntegrityError, match=constraint):
                async with db_engine.begin() as connection:
                    await connection.execute(
                        text(f"UPDATE conversations SET {column} = {value} WHERE id = :id"),
                        {"id": uuid.UUID(conversation["id"])},
                    )

    async def test_a_conversation_needs_a_real_workspace(self, db_engine: AsyncEngine) -> None:
        with pytest.raises(IntegrityError, match="fk_conversations_workspace_id_workspaces"):
            async with db_engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO conversations (workspace_id, created_by) VALUES (:w, :u)"),
                    {"w": uuid.uuid4(), "u": uuid.uuid4()},
                )


class TestAutomaticTitle:
    @pytest.mark.parametrize(
        ("message", "title"),
        [
            ("What is the refund policy?", "What is the refund policy?"),
            ("  What   is\n\nthe refund\tpolicy?  ", "What is the refund policy?"),
            ("x" * 60, "x" * 60),
            (
                "Can you summarise the main points of the quarterly finance report for me please",
                "Can you summarise the main points of the quarterly finance…",
            ),
            ("word " * 40, "word word word word word word word word word word word word…"),
            (
                "What is the refund policy for sale items bought online last month?",
                "What is the refund policy for sale items bought online last…",
            ),
            ("a" * 200, "a" * 59 + "…"),
            ("Hello, world. " * 10, "Hello, world. Hello, world. Hello, world. Hello, world…"),
            ("\x00\x07", "New conversation"),
        ],
        ids=[
            "short",
            "whitespace",
            "exactly 60",
            "long",
            "repeated",
            "cut falls between words",
            "one long word",
            "punctuation",
            "unprintable",
        ],
    )
    def test_title_from_a_first_message(self, message: str, title: str) -> None:
        result = title_from_message(message)

        assert result == title
        assert len(result) <= 60

    def test_a_long_title_ends_on_a_whole_word(self) -> None:
        title = title_from_message(
            "Please explain the difference between a refund and a chargeback in detail"
        )

        assert title.endswith("…")
        assert (
            title[:-1]
            in "Please explain the difference between a refund and a chargeback in detail"
        )
        assert not title[:-1].endswith(" ")
        # The cut falls between words, not inside one.
        assert (
            "Please explain the difference between a refund and a chargeback in detail"[
                len(title) - 1
            ]
            == " "
        )

    async def test_the_first_message_names_an_untitled_conversation(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])

        await send(db_client, conversation["id"], team["alice"], "  What is the refund policy?  ")

        current = await db_client.get(
            f"{API}/conversations/{conversation['id']}", headers=auth(team["alice"])
        )
        assert current.json()["title"] == "What is the refund policy?"

    async def test_a_title_the_user_chose_is_never_overwritten(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        chosen = await create_conversation(db_client, team["workspace"], team["alice"], "My title")
        renamed = await create_conversation(db_client, team["workspace"], team["alice"])
        await db_client.patch(
            f"{API}/conversations/{renamed['id']}",
            json={"title": "Renamed first"},
            headers=auth(team["alice"]),
        )

        await send(db_client, chosen["id"], team["alice"], "What is the refund policy?")
        await send(db_client, renamed["id"], team["alice"], "What is the refund policy?")

        titles = {
            item["title"]
            for item in (await listing(db_client, team["workspace"], team["alice"]))["items"]
        }
        assert titles == {"My title", "Renamed first"}

    async def test_only_the_first_message_sets_the_title(
        self, db_client: AsyncClient, team: dict
    ) -> None:
        conversation = await create_conversation(db_client, team["workspace"], team["alice"])
        await send(db_client, conversation["id"], team["alice"], "First question")
        await db_client.patch(
            f"{API}/conversations/{conversation['id']}",
            json={"title": "New conversation"},
            headers=auth(team["alice"]),
        )

        await send(db_client, conversation["id"], team["alice"], "Second question")

        current = await db_client.get(
            f"{API}/conversations/{conversation['id']}", headers=auth(team["alice"])
        )
        assert current.json()["title"] == "New conversation"
