"""Workspace CRUD against the real test database."""

import uuid

import pytest
from httpx import AsyncClient

from tests.auth import auth

WORKSPACES = "/api/v1/workspaces"


async def create_workspace(client: AsyncClient, user_id: uuid.UUID, name: str = "Research") -> str:
    response = await client.post(WORKSPACES, json={"name": name}, headers=auth(user_id))
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def add_member(
    client: AsyncClient, workspace_id: str, actor: uuid.UUID, user_id: uuid.UUID, role: str
) -> None:
    response = await client.post(
        f"{WORKSPACES}/{workspace_id}/members",
        json={"user_id": str(user_id), "role": role},
        headers=auth(actor),
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", WORKSPACES),
        ("POST", WORKSPACES),
        ("GET", f"{WORKSPACES}/{uuid.uuid4()}"),
        ("PATCH", f"{WORKSPACES}/{uuid.uuid4()}"),
        ("DELETE", f"{WORKSPACES}/{uuid.uuid4()}"),
        ("GET", f"{WORKSPACES}/{uuid.uuid4()}/members"),
        ("POST", f"{WORKSPACES}/{uuid.uuid4()}/members"),
        ("DELETE", f"{WORKSPACES}/{uuid.uuid4()}/members/{uuid.uuid4()}"),
    ],
)
async def test_every_endpoint_requires_authentication(
    db_client: AsyncClient, method: str, path: str
) -> None:
    response = await db_client.request(method, path, json={"name": "x"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_invalid_token_is_401_on_workspace_endpoints(db_client: AsyncClient) -> None:
    response = await db_client.get(WORKSPACES, headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_create_makes_the_caller_owner(db_client: AsyncClient) -> None:
    user = uuid.uuid4()

    response = await db_client.post(WORKSPACES, json={"name": "  Research  "}, headers=auth(user))

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Research"
    assert body["owner_id"] == str(user)
    assert body["role"] == "owner"

    members = await db_client.get(f"{WORKSPACES}/{body['id']}/members", headers=auth(user))
    assert [(m["user_id"], m["role"]) for m in members.json()] == [(str(user), "owner")]


async def test_create_ignores_an_owner_id_sent_by_the_client(db_client: AsyncClient) -> None:
    user, someone_else = uuid.uuid4(), uuid.uuid4()

    response = await db_client.post(
        WORKSPACES, json={"name": "Mine", "owner_id": str(someone_else)}, headers=auth(user)
    )

    # Unknown fields are rejected outright rather than silently dropped.
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [{}, {"name": ""}, {"name": "   "}, {"name": "x" * 101}, {"name": "bad\x00name"}, {"name": 5}],
    ids=["missing", "empty", "only spaces", "too long", "control character", "not a string"],
)
async def test_create_rejects_invalid_names(
    db_client: AsyncClient, body: dict[str, object]
) -> None:
    response = await db_client.post(WORKSPACES, json=body, headers=auth(uuid.uuid4()))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_name_of_exactly_100_characters_is_accepted(db_client: AsyncClient) -> None:
    response = await db_client.post(
        WORKSPACES, json={"name": "x" * 100}, headers=auth(uuid.uuid4())
    )

    assert response.status_code == 201


async def test_list_returns_only_the_callers_workspaces_with_their_role(
    db_client: AsyncClient,
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    first = await create_workspace(db_client, alice, "Alpha")
    shared = await create_workspace(db_client, bob, "Shared")
    await create_workspace(db_client, bob, "Bob only")
    await add_member(db_client, shared, bob, alice, "member")

    response = await db_client.get(WORKSPACES, headers=auth(alice))

    assert response.status_code == 200
    assert [(w["id"], w["role"]) for w in response.json()] == [(first, "owner"), (shared, "member")]


async def test_list_is_empty_for_a_new_user(db_client: AsyncClient) -> None:
    await create_workspace(db_client, uuid.uuid4())

    response = await db_client.get(WORKSPACES, headers=auth(uuid.uuid4()))

    assert response.json() == []


async def test_get_unknown_workspace_is_404(db_client: AsyncClient) -> None:
    response = await db_client.get(f"{WORKSPACES}/{uuid.uuid4()}", headers=auth(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"


async def test_malformed_workspace_id_is_422(db_client: AsyncClient) -> None:
    response = await db_client.get(f"{WORKSPACES}/not-a-uuid", headers=auth(uuid.uuid4()))

    assert response.status_code == 422


async def test_owner_and_admin_can_rename_but_member_cannot(db_client: AsyncClient) -> None:
    owner, admin, member = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = await create_workspace(db_client, owner)
    await add_member(db_client, workspace, owner, admin, "admin")
    await add_member(db_client, workspace, owner, member, "member")
    url = f"{WORKSPACES}/{workspace}"

    by_owner = await db_client.patch(url, json={"name": "By owner"}, headers=auth(owner))
    by_admin = await db_client.patch(url, json={"name": "By admin"}, headers=auth(admin))
    by_member = await db_client.patch(url, json={"name": "By member"}, headers=auth(member))

    assert (by_owner.status_code, by_admin.status_code) == (200, 200)
    assert by_member.status_code == 403
    assert by_member.json()["error"]["code"] == "forbidden"
    current = await db_client.get(url, headers=auth(member))
    assert current.json()["name"] == "By admin"
    assert current.json()["updated_at"] >= current.json()["created_at"]


async def test_only_an_owner_can_delete_a_workspace(db_client: AsyncClient) -> None:
    owner, admin, member = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = await create_workspace(db_client, owner)
    await add_member(db_client, workspace, owner, admin, "admin")
    await add_member(db_client, workspace, owner, member, "member")
    url = f"{WORKSPACES}/{workspace}"

    by_member = await db_client.delete(url, headers=auth(member))
    by_admin = await db_client.delete(url, headers=auth(admin))
    still_there = await db_client.get(url, headers=auth(owner))
    by_owner = await db_client.delete(url, headers=auth(owner))
    gone = await db_client.get(url, headers=auth(owner))

    assert (by_member.status_code, by_admin.status_code) == (403, 403)
    assert still_there.status_code == 200
    assert by_owner.status_code == 204
    assert by_owner.content == b""
    assert gone.status_code == 404
