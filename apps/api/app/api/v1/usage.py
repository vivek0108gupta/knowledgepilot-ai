import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.models.usage import UsageOperation, UsageStatus
from app.schemas.usage import (
    AlertOut,
    AuditPage,
    MemberQuotaOut,
    MemberQuotaUpdate,
    MyUsage,
    QuotaPolicyOut,
    QuotaPolicyUpdate,
    UsageHistory,
    UsageSummary,
)
from app.services import usage_reports as service

router = APIRouter(tags=["usage"])


@router.get("/usage/me")
async def get_my_usage(user: CurrentUser, session: DbSession) -> MyUsage:
    """The caller's own AI usage today, across all their workspaces, and their daily limits."""
    return await service.my_usage(session, user.id)


@router.get("/workspaces/{workspace_id}/usage")
async def get_workspace_usage(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> UsageSummary:
    """This month's usage (UTC): totals, by operation, by model and by day, with the
    remaining allowance. Owners and admins see the whole workspace; members see their own."""
    return await service.summary(session, workspace_id, user.id)


@router.get("/workspaces/{workspace_id}/usage/history")
async def get_usage_history(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    operation: UsageOperation | None = None,
    status: UsageStatus | None = None,
) -> UsageHistory:
    """Usage records, newest first. Owners and admins see everyone's; members their own."""
    return await service.history(
        session,
        workspace_id,
        user.id,
        limit=limit,
        offset=offset,
        operation=operation.value if operation else None,
        status=status.value if status else None,
    )


@router.get("/workspaces/{workspace_id}/quotas")
async def get_quotas(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> QuotaPolicyOut:
    """The limits in force for this workspace, the system ceilings, and what remains."""
    return await service.quota_policy(session, workspace_id, user.id)


@router.patch("/workspaces/{workspace_id}/quotas")
async def update_quotas(
    workspace_id: uuid.UUID, body: QuotaPolicyUpdate, user: CurrentUser, session: DbSession
) -> QuotaPolicyOut:
    """Tighten this workspace's limits, or set one to null to use the system limit again.
    Owners only. A limit above the system's is refused."""
    return await service.update_quota_policy(session, workspace_id, user.id, body)


@router.get("/workspaces/{workspace_id}/usage/alerts")
async def get_usage_alerts(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[AlertOut]:
    """Usage thresholds crossed this month in this workspace, and your own today."""
    return await service.workspace_alerts(session, workspace_id, user.id)


@router.get("/workspaces/{workspace_id}/members/quotas")
async def get_member_quotas(
    workspace_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> list[MemberQuotaOut]:
    """Each member's own limits and this month's use. Owners and admins."""
    return await service.member_quotas(session, workspace_id, user.id)


@router.put("/workspaces/{workspace_id}/members/{member_id}/quota")
async def put_member_quota(
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MemberQuotaUpdate,
    user: CurrentUser,
    session: DbSession,
) -> MemberQuotaOut:
    """Set a member's own limits in this workspace (null: none). Owners only; never above
    the workspace's limits. Recorded in the audit log."""
    return await service.update_member_quota(session, workspace_id, user.id, member_id, body)


@router.get("/workspaces/{workspace_id}/audit")
async def get_audit_log(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AuditPage:
    """Usage policy changes in this workspace, newest first. Owners and admins."""
    return await service.audit_log(session, workspace_id, user.id, limit=limit, offset=offset)
