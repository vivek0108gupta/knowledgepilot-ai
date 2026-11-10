"""Workspace and membership rules.

Every function takes the acting user's ID from a verified token and checks membership and
role itself, so no route can skip authorization by forgetting a dependency.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceRole

OWNER = WorkspaceRole.OWNER
ADMIN = WorkspaceRole.ADMIN
MEMBER = WorkspaceRole.MEMBER


@dataclass(frozen=True, slots=True)
class WorkspaceAccess:
    """A workspace together with the acting user's proven membership in it."""

    workspace: Workspace
    membership: WorkspaceMember

    @property
    def role(self) -> WorkspaceRole:
        return WorkspaceRole(self.membership.role)


def not_found() -> AppError:
    # The same answer whether the workspace does not exist or the caller is not a member,
    # so outsiders cannot probe for which workspace IDs are real.
    return AppError(404, "workspace_not_found", "Workspace not found.")


def forbidden() -> AppError:
    return AppError(403, "forbidden", "You do not have permission to do this.")


async def get_access(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> WorkspaceAccess:
    """Load a workspace only if the user is a member of it.

    With `lock=True` the workspace row is locked until the transaction ends, which serialises
    membership changes so two concurrent removals cannot both pass the last-owner check.
    """
    query = (
        select(Workspace, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user_id)
    )
    if lock:
        query = query.with_for_update(of=Workspace)

    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise not_found()
    return WorkspaceAccess(workspace=row[0], membership=row[1])


def require_role(access: WorkspaceAccess, *allowed: WorkspaceRole) -> None:
    if access.role not in allowed:
        raise forbidden()


async def list_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[WorkspaceAccess]:
    rows = await session.execute(
        select(Workspace, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.created_at, Workspace.id)
    )
    return [WorkspaceAccess(workspace=row[0], membership=row[1]) for row in rows]


async def create(session: AsyncSession, user_id: uuid.UUID, name: str) -> WorkspaceAccess:
    """Create a workspace and its creator's owner membership in one transaction."""
    workspace = Workspace(id=uuid.uuid4(), name=name, owner_id=user_id)
    membership = WorkspaceMember(workspace_id=workspace.id, user_id=user_id, role=OWNER.value)
    session.add(workspace)
    await session.flush()
    session.add(membership)
    await session.commit()
    await session.refresh(workspace)
    await session.refresh(membership)
    return WorkspaceAccess(workspace=workspace, membership=membership)


async def rename(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, name: str
) -> WorkspaceAccess:
    access = await get_access(session, workspace_id, user_id, lock=True)
    require_role(access, OWNER, ADMIN)
    access.workspace.name = name
    await session.commit()
    await session.refresh(access.workspace)
    return access


async def remove(session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    access = await get_access(session, workspace_id, user_id, lock=True)
    require_role(access, OWNER)
    # Members go with it through the foreign key's ON DELETE CASCADE.
    await session.execute(delete(Workspace).where(Workspace.id == access.workspace.id))
    await session.commit()


async def list_members(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[WorkspaceMember]:
    await get_access(session, workspace_id, user_id)
    members = await session.scalars(
        select(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at, WorkspaceMember.id)
    )
    return list(members)


async def add_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    new_user_id: uuid.UUID,
    role: WorkspaceRole,
) -> WorkspaceMember:
    access = await get_access(session, workspace_id, actor_id, lock=True)

    # Owners may grant any role. Admins may add regular members only, so they cannot create
    # another admin or an owner (role escalation). Members may not add anyone.
    if access.role == ADMIN:
        if role != MEMBER:
            raise forbidden()
    elif access.role != OWNER:
        raise forbidden()

    already_member = await session.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == new_user_id,
        )
    )
    if already_member is not None:
        raise AppError(409, "already_member", "This user is already a member.")

    member = WorkspaceMember(workspace_id=workspace_id, user_id=new_user_id, role=role.value)
    session.add(member)
    try:
        await session.commit()
    except IntegrityError as error:
        # The unique constraint is the final guard if two requests race past the check above.
        await session.rollback()
        raise AppError(409, "already_member", "This user is already a member.") from error
    await session.refresh(member)
    return member


async def remove_member(
    session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID, target_user_id: uuid.UUID
) -> None:
    access = await get_access(session, workspace_id, actor_id, lock=True)
    leaving = target_user_id == actor_id

    # Decide on the actor's role before looking the target up, so a regular member cannot
    # use this endpoint to discover who else belongs to the workspace.
    if not leaving and access.role == MEMBER:
        raise forbidden()

    target = (
        access.membership
        if leaving
        else await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == target_user_id,
            )
        )
    )
    if target is None:
        raise AppError(404, "member_not_found", "Member not found.")

    target_role = WorkspaceRole(target.role)

    # Admins manage regular members only: not owners, and not other admins.
    if not leaving and access.role == ADMIN and target_role != MEMBER:
        raise forbidden()

    if target_role == OWNER:
        owner_count = await session.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == OWNER.value,
            )
        )
        if owner_count is None or owner_count <= 1:
            raise AppError(409, "last_owner", "A workspace must keep at least one owner.")

    await session.delete(target)
    await session.flush()

    if access.workspace.owner_id == target_user_id:
        # The primary owner left; hand that over to the longest-standing remaining owner.
        next_owner = await session.scalar(
            select(WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == OWNER.value,
            )
            .order_by(WorkspaceMember.created_at, WorkspaceMember.id)
            .limit(1)
        )
        if next_owner is not None:
            access.workspace.owner_id = next_owner

    await session.commit()
