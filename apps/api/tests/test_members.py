"""Membership and role rules against the real test database."""

import uuid

import pytest
from httpx import AsyncClient

from tests.auth import auth
from tests.test_workspaces import WORKSPACES, add_member, create_workspace


async def roles(client: AsyncClient, workspace: str, viewer: uuid.UUID) -> dict[str, str]:
    response = await client.get(f"{WORKSPACES}/{workspace}/members", headers=auth(viewer))
    assert response.status_code == 200, response.text
    return {member["user_id"]: member["role"] for member in response.json()}


async def post_member(
    client: AsyncClient, workspace: str, actor: uuid.UUID, user: uuid.UUID, role: str | None
):  # noqa: ANN201
    body: dict[str, str] = {"user_id": str(user)}
    if role is not None:
        body["role"] = role
    return await client.post(f"{WORKSPACES}/{workspace}/members", json=body, headers=auth(actor))


async def delete_member(client: AsyncClient, workspace: str, actor: uuid.UUID, user: uuid.UUID):  # noqa: ANN201
    return await client.delete(f"{WORKSPACES}/{workspace}/members/{user}", headers=auth(actor))


@pytest.fixture
async def team(db_client: AsyncClient) -> dict[str, object]:
    """A workspace with one owner, one admin and one regular member."""
    owner, admin, member = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace = await create_workspace(db_client, owner)
    await add_member(db_client, workspace, owner, admin, "admin")
    await add_member(db_client, workspace, owner, member, "member")
    return {"workspace": workspace, "owner": owner, "admin": admin, "member": member}


async def test_every_member_can_list_members(db_client: AsyncClient, team: dict) -> None:
    for viewer in ("owner", "admin", "member"):
        seen = await roles(db_client, team["workspace"], team[viewer])
        assert seen == {
            str(team["owner"]): "owner",
            str(team["admin"]): "admin",
            str(team["member"]): "member",
        }


async def test_new_member_defaults_to_the_member_role(db_client: AsyncClient, team: dict) -> None:
    newcomer = uuid.uuid4()

    response = await post_member(db_client, team["workspace"], team["owner"], newcomer, None)

    assert response.status_code == 201
    assert response.json()["role"] == "member"
    assert response.json()["user_id"] == str(newcomer)


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
async def test_owner_can_add_any_role(db_client: AsyncClient, team: dict, role: str) -> None:
    response = await post_member(db_client, team["workspace"], team["owner"], uuid.uuid4(), role)

    assert response.status_code == 201
    assert response.json()["role"] == role


async def test_admin_can_add_a_regular_member(db_client: AsyncClient, team: dict) -> None:
    response = await post_member(
        db_client, team["workspace"], team["admin"], uuid.uuid4(), "member"
    )

    assert response.status_code == 201


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_admin_cannot_grant_elevated_roles(
    db_client: AsyncClient, team: dict, role: str
) -> None:
    newcomer = uuid.uuid4()

    response = await post_member(db_client, team["workspace"], team["admin"], newcomer, role)

    assert response.status_code == 403
    assert str(newcomer) not in await roles(db_client, team["workspace"], team["owner"])


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
async def test_member_cannot_add_anyone(db_client: AsyncClient, team: dict, role: str) -> None:
    newcomer = uuid.uuid4()

    response = await post_member(db_client, team["workspace"], team["member"], newcomer, role)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert str(newcomer) not in await roles(db_client, team["workspace"], team["owner"])


async def test_unknown_role_is_rejected(db_client: AsyncClient, team: dict) -> None:
    response = await post_member(
        db_client, team["workspace"], team["owner"], uuid.uuid4(), "superuser"
    )

    assert response.status_code == 422


async def test_duplicate_membership_is_409_and_does_not_change_the_role(
    db_client: AsyncClient, team: dict
) -> None:
    response = await post_member(
        db_client, team["workspace"], team["owner"], team["member"], "admin"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_member"
    seen = await roles(db_client, team["workspace"], team["owner"])
    assert seen[str(team["member"])] == "member"


async def test_member_cannot_promote_themself_by_re_adding(
    db_client: AsyncClient, team: dict
) -> None:
    response = await post_member(
        db_client, team["workspace"], team["member"], team["member"], "owner"
    )

    assert response.status_code == 403


async def test_owner_can_remove_admin_and_member(db_client: AsyncClient, team: dict) -> None:
    for target in ("admin", "member"):
        response = await delete_member(db_client, team["workspace"], team["owner"], team[target])
        assert response.status_code == 204

    assert await roles(db_client, team["workspace"], team["owner"]) == {str(team["owner"]): "owner"}


async def test_admin_can_remove_a_regular_member(db_client: AsyncClient, team: dict) -> None:
    response = await delete_member(db_client, team["workspace"], team["admin"], team["member"])

    assert response.status_code == 204


async def test_admin_cannot_remove_the_owner(db_client: AsyncClient, team: dict) -> None:
    response = await delete_member(db_client, team["workspace"], team["admin"], team["owner"])

    assert response.status_code == 403
    seen = await roles(db_client, team["workspace"], team["owner"])
    assert seen[str(team["owner"])] == "owner"


async def test_admin_cannot_remove_another_admin(db_client: AsyncClient, team: dict) -> None:
    other_admin = uuid.uuid4()
    await add_member(db_client, team["workspace"], team["owner"], other_admin, "admin")

    response = await delete_member(db_client, team["workspace"], team["admin"], other_admin)

    assert response.status_code == 403


async def test_member_cannot_remove_anyone(db_client: AsyncClient, team: dict) -> None:
    for target in ("owner", "admin"):
        response = await delete_member(db_client, team["workspace"], team["member"], team[target])
        assert response.status_code == 403


async def test_member_gets_403_not_404_for_an_unknown_user(
    db_client: AsyncClient, team: dict
) -> None:
    """A member must not be able to tell who is or is not in the workspace by probing."""
    response = await delete_member(db_client, team["workspace"], team["member"], uuid.uuid4())

    assert response.status_code == 403


async def test_removing_an_unknown_user_is_404_for_an_owner(
    db_client: AsyncClient, team: dict
) -> None:
    response = await delete_member(db_client, team["workspace"], team["owner"], uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "member_not_found"


async def test_member_and_admin_can_leave(db_client: AsyncClient, team: dict) -> None:
    for person in ("member", "admin"):
        response = await delete_member(db_client, team["workspace"], team[person], team[person])
        assert response.status_code == 204

    lost_access = await db_client.get(
        f"{WORKSPACES}/{team['workspace']}", headers=auth(team["member"])
    )
    assert lost_access.status_code == 404


async def test_last_owner_cannot_be_removed(db_client: AsyncClient, team: dict) -> None:
    response = await delete_member(db_client, team["workspace"], team["owner"], team["owner"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_owner"
    seen = await roles(db_client, team["workspace"], team["owner"])
    assert seen[str(team["owner"])] == "owner"


async def test_an_owner_can_leave_once_there_is_another_owner(
    db_client: AsyncClient, team: dict
) -> None:
    second_owner = uuid.uuid4()
    await add_member(db_client, team["workspace"], team["owner"], second_owner, "owner")

    left = await delete_member(db_client, team["workspace"], team["owner"], team["owner"])

    assert left.status_code == 204
    workspace = await db_client.get(f"{WORKSPACES}/{team['workspace']}", headers=auth(second_owner))
    # Primary ownership moved to the remaining owner, and that owner is now the last one.
    assert workspace.json()["owner_id"] == str(second_owner)
    stuck = await delete_member(db_client, team["workspace"], second_owner, second_owner)
    assert stuck.status_code == 409


async def test_one_owner_can_remove_another_but_not_the_last(
    db_client: AsyncClient, team: dict
) -> None:
    second_owner = uuid.uuid4()
    await add_member(db_client, team["workspace"], team["owner"], second_owner, "owner")

    removed = await delete_member(db_client, team["workspace"], second_owner, team["owner"])
    last = await delete_member(db_client, team["workspace"], second_owner, second_owner)

    assert removed.status_code == 204
    assert last.status_code == 409
