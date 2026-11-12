"""Cross-tenant isolation: one user's workspace is invisible and untouchable to another."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.auth import auth
from tests.test_workspaces import WORKSPACES, add_member, create_workspace


async def test_user_a_cannot_reach_user_bs_workspace_by_any_endpoint(
    db_client: AsyncClient,
) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    bobs = await create_workspace(db_client, bob, "Bob's secrets")
    url = f"{WORKSPACES}/{bobs}"
    as_alice = auth(alice)

    attempts = [
        await db_client.get(url, headers=as_alice),
        await db_client.patch(url, json={"name": "Hijacked"}, headers=as_alice),
        await db_client.delete(url, headers=as_alice),
        await db_client.get(f"{url}/members", headers=as_alice),
        await db_client.post(
            f"{url}/members", json={"user_id": str(alice), "role": "owner"}, headers=as_alice
        ),
        await db_client.delete(f"{url}/members/{bob}", headers=as_alice),
        await db_client.delete(f"{url}/members/{alice}", headers=as_alice),
    ]

    # 404 every time, identical to a workspace that does not exist: existence is not revealed.
    assert [response.status_code for response in attempts] == [404] * len(attempts)
    assert {response.json()["error"]["code"] for response in attempts} == {"workspace_not_found"}
    assert "Bob's secrets" not in "".join(response.text for response in attempts)

    untouched = await db_client.get(url, headers=auth(bob))
    assert untouched.json()["name"] == "Bob's secrets"
    members = await db_client.get(f"{url}/members", headers=auth(bob))
    assert [member["user_id"] for member in members.json()] == [str(bob)]


async def test_role_in_one_workspace_grants_nothing_in_another(db_client: AsyncClient) -> None:
    alice, bob = uuid.uuid4(), uuid.uuid4()
    alices = await create_workspace(db_client, alice, "Alice's")
    bobs = await create_workspace(db_client, bob, "Bob's")
    await add_member(db_client, bobs, bob, alice, "member")

    # Alice owns her own workspace but is only a member of Bob's.
    rename = await db_client.patch(
        f"{WORKSPACES}/{bobs}", json={"name": "Taken over"}, headers=auth(alice)
    )
    delete = await db_client.delete(f"{WORKSPACES}/{bobs}", headers=auth(alice))
    remove_bob = await db_client.delete(f"{WORKSPACES}/{bobs}/members/{bob}", headers=auth(alice))

    assert (rename.status_code, delete.status_code, remove_bob.status_code) == (403, 403, 403)
    assert alices != bobs


async def test_removed_member_loses_access_immediately(db_client: AsyncClient) -> None:
    owner, member = uuid.uuid4(), uuid.uuid4()
    workspace = await create_workspace(db_client, owner)
    await add_member(db_client, workspace, owner, member, "member")
    assert (
        await db_client.get(f"{WORKSPACES}/{workspace}", headers=auth(member))
    ).status_code == 200

    await db_client.delete(f"{WORKSPACES}/{workspace}/members/{member}", headers=auth(owner))

    assert (
        await db_client.get(f"{WORKSPACES}/{workspace}", headers=auth(member))
    ).status_code == 404
    assert (await db_client.get(WORKSPACES, headers=auth(member))).json() == []


async def test_deleting_a_workspace_cascades_to_its_members_only(
    db_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    owner = uuid.uuid4()
    doomed = await create_workspace(db_client, owner, "Doomed")
    kept = await create_workspace(db_client, owner, "Kept")
    await add_member(db_client, doomed, owner, uuid.uuid4(), "member")

    await db_client.delete(f"{WORKSPACES}/{doomed}", headers=auth(owner))

    async with db_engine.connect() as connection:
        rows = await connection.execute(text("SELECT workspace_id::text FROM workspace_members"))
        assert [row[0] for row in rows] == [kept]


async def test_concurrent_removals_cannot_leave_a_workspace_without_an_owner(
    db_client: AsyncClient,
) -> None:
    """Two owners remove each other at the same moment; the row lock lets only one succeed."""
    first, second = uuid.uuid4(), uuid.uuid4()
    workspace = await create_workspace(db_client, first)
    await add_member(db_client, workspace, first, second, "owner")
    url = f"{WORKSPACES}/{workspace}/members"

    results = await asyncio.gather(
        db_client.delete(f"{url}/{second}", headers=auth(first)),
        db_client.delete(f"{url}/{first}", headers=auth(second)),
    )

    # One removal wins. The loser either hits the last-owner rule or finds it is no longer
    # a member; either way exactly one owner remains.
    statuses = sorted(response.status_code for response in results)
    assert statuses[0] == 204
    assert statuses[1] in (404, 409)
    survivor = first if results[0].status_code == 204 else second
    members = await db_client.get(url, headers=auth(survivor))
    assert [(m["user_id"], m["role"]) for m in members.json()] == [(str(survivor), "owner")]


class TestDatabaseConstraints:
    """The schema itself refuses bad data, even if application code had a bug."""

    async def insert_workspace(self, engine: AsyncEngine) -> uuid.UUID:
        workspace_id = uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO workspaces (id, name, owner_id) VALUES (:id, 'W', :owner)"),
                {"id": workspace_id, "owner": uuid.uuid4()},
            )
        return workspace_id

    async def insert_member(
        self, engine: AsyncEngine, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) "
                    "VALUES (:workspace, :user, :role)"
                ),
                {"workspace": workspace_id, "user": user_id, "role": role},
            )

    async def test_migrations_are_applied_and_pgvector_is_installed(
        self, db_engine: AsyncEngine
    ) -> None:
        async with db_engine.connect() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            pgvector = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        assert version == "0008"
        assert pgvector is not None

    async def test_duplicate_membership_is_rejected(self, db_engine: AsyncEngine) -> None:
        workspace_id, user_id = await self.insert_workspace(db_engine), uuid.uuid4()
        await self.insert_member(db_engine, workspace_id, user_id, "member")

        with pytest.raises(IntegrityError, match="uq_workspace_members_workspace_id_user_id"):
            await self.insert_member(db_engine, workspace_id, user_id, "admin")

    async def test_unknown_role_is_rejected(self, db_engine: AsyncEngine) -> None:
        workspace_id = await self.insert_workspace(db_engine)

        with pytest.raises(IntegrityError, match="ck_workspace_members_role_valid"):
            await self.insert_member(db_engine, workspace_id, uuid.uuid4(), "superuser")

    async def test_member_of_a_missing_workspace_is_rejected(self, db_engine: AsyncEngine) -> None:
        with pytest.raises(IntegrityError, match="fk_workspace_members_workspace_id_workspaces"):
            await self.insert_member(db_engine, uuid.uuid4(), uuid.uuid4(), "member")
