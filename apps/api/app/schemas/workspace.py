import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from app.models.workspace import WorkspaceRole


def no_control_characters(value: str) -> str:
    if any(not character.isprintable() for character in value):
        raise ValueError("must not contain control characters")
    return value


WorkspaceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(no_control_characters),
]


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkspaceName


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkspaceName


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    # The caller's own role in this workspace, so the UI knows what to offer.
    role: WorkspaceRole
    created_at: datetime
    updated_at: datetime


class MemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    role: WorkspaceRole = WorkspaceRole.MEMBER


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: WorkspaceRole
    created_at: datetime


class Me(BaseModel):
    id: uuid.UUID
    email: str | None
