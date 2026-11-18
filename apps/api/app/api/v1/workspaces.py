import logging
import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUser, DbSession
from app.api.v1.documents import StorageDep
from app.core.errors import AppError
from app.schemas.workspace import (
    MemberCreate,
    MemberOut,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from app.services import documents as document_service
from app.services import workspaces as service
from app.services.workspaces import WorkspaceAccess

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def to_out(access: WorkspaceAccess) -> WorkspaceOut:
    workspace = access.workspace
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        owner_id=workspace.owner_id,
        role=access.role,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


@router.get("")
async def list_workspaces(user: CurrentUser, session: DbSession) -> list[WorkspaceOut]:
    """The workspaces the caller belongs to. Never anyone else's."""
    return [to_out(access) for access in await service.list_for_user(session, user.id)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate, user: CurrentUser, session: DbSession
) -> WorkspaceOut:
    """Create a workspace. The caller becomes its owner."""
    return to_out(await service.create(session, user.id, body.name))


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> WorkspaceOut:
    return to_out(await service.get_access(session, workspace_id, user.id))


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: uuid.UUID, body: WorkspaceUpdate, user: CurrentUser, session: DbSession
) -> WorkspaceOut:
    """Rename a workspace. Owners and admins only."""
    return to_out(await service.rename(session, workspace_id, user.id, body.name))


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession, storage_provider: StorageDep
) -> Response:
    """Delete a workspace with its memberships, documents and stored files. Owners only."""
    storage_paths = await service.remove(session, workspace_id, user.id)
    if storage_paths:
        # The workspace is gone either way; a storage failure here is logged, not returned.
        try:
            storage = storage_provider()
        except AppError:
            logger.error("Storage is not configured; %d file(s) left behind", len(storage_paths))
        else:
            await document_service.delete_stored_files(storage, storage_paths)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workspace_id}/members")
async def list_members(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[MemberOut]:
    members = await service.list_members(session, workspace_id, user.id)
    return [MemberOut.model_validate(member) for member in members]


@router.post("/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    workspace_id: uuid.UUID, body: MemberCreate, user: CurrentUser, session: DbSession
) -> MemberOut:
    """Add an existing user by ID. Owners may grant any role; admins may add members only."""
    member = await service.add_member(session, workspace_id, user.id, body.user_id, body.role)
    return MemberOut.model_validate(member)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    """Remove a member, or leave the workspace by passing your own ID."""
    await service.remove_member(session, workspace_id, user.id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
