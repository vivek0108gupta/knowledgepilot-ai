"""Reading usage back: summaries, history and quota policies, always for one workspace the
caller is a member of.

Who sees what (the existing role model): owners and admins see the whole workspace's usage,
including who used it; members see only their own usage in it, plus the workspace's
remaining allowance, which every member is subject to. Only owners change quota policies, and
only to tighten them. The workspace is always checked through the caller's own membership:
a workspace ID in a request is never taken as proof of access.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.usage import (
    GENERATIONS,
    AlertKind,
    AuditEvent,
    UsageAlert,
    UsageEvent,
    UsageSource,
    WorkspaceMemberQuota,
    WorkspaceQuotaPolicy,
)
from app.models.workspace import WorkspaceMember, WorkspaceRole
from app.schemas.usage import (
    AlertOut,
    AuditEventOut,
    AuditPage,
    ChargeBreakdown,
    DailyUsage,
    MemberQuotaOut,
    MemberQuotaUpdate,
    ModelUsage,
    MyUsage,
    OperationUsage,
    QuotaLimits,
    QuotaPolicyOut,
    QuotaPolicyUpdate,
    UsageEventOut,
    UsageHistory,
    UsageSummary,
    UsageTotals,
    UserAllowance,
    WorkspaceAllowance,
)
from app.services import usage
from app.services import workspaces as workspace_service
from app.services.pricing import get_pricing

MANAGERS = (WorkspaceRole.OWNER, WorkspaceRole.ADMIN)


def _totals(tokens: Any, cost: Any, count: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "tokens": int(tokens or 0),
        "estimated_cost": Decimal(cost or 0),
        "operations": int(count or 0),
    }


async def workspace_allowance(
    session: AsyncSession, workspace_id: uuid.UUID, now: datetime
) -> WorkspaceAllowance:
    limits = await usage.effective_limits(session, workspace_id)
    balance = await usage.workspace_balance(session, workspace_id, now)
    return WorkspaceAllowance(
        monthly_token_limit=limits.workspace_monthly_tokens,
        monthly_tokens_used=balance.tokens_this_month,
        monthly_tokens_remaining=max(
            0, limits.workspace_monthly_tokens - balance.tokens_this_month
        ),
        monthly_cost_limit=limits.workspace_monthly_cost,
        monthly_cost_used=balance.cost_this_month,
        monthly_cost_remaining=max(
            Decimal(0), limits.workspace_monthly_cost - balance.cost_this_month
        ),
        max_concurrent_operations=limits.workspace_concurrent_operations,
        operations_running=balance.operations_running,
    )


async def user_allowance(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> UserAllowance:
    limits = usage.system_limits(get_settings(), get_pricing())
    balance = await usage.user_balance(session, user_id, now)
    return UserAllowance(
        daily_token_limit=limits.user_daily_tokens,
        daily_tokens_used=balance.tokens_today,
        daily_tokens_remaining=max(0, limits.user_daily_tokens - balance.tokens_today),
        daily_question_limit=limits.user_daily_chats,
        daily_questions_used=balance.chats_today,
        max_concurrent_generations=limits.user_concurrent_generations,
        generations_running=balance.generations_running,
        resets_at=usage.day_start(now) + timedelta(days=1),
    )


async def summary(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> UsageSummary:
    access = await workspace_service.get_access(session, workspace_id, user_id)
    everyone = access.role in MANAGERS
    now = datetime.now(UTC)
    start, end = usage.month_start(now), usage.next_month(now)
    scope = [UsageEvent.workspace_id == workspace_id, UsageEvent.created_at >= start]
    if not everyone:
        scope.append(UsageEvent.user_id == user_id)

    tokens, cost, count = UsageEvent.charged_tokens, UsageEvent.charged_cost, UsageEvent.id
    total = (
        await session.execute(
            select(
                func.sum(tokens),
                func.sum(cost),
                func.count(count),
                func.count().filter(UsageEvent.usage_source == UsageSource.ESTIMATED.value),
                func.count().filter(UsageEvent.usage_source == UsageSource.UNAVAILABLE.value),
                func.coalesce(
                    func.sum(tokens).filter(UsageEvent.usage_source == UsageSource.PROVIDER.value),
                    0,
                ),
                func.coalesce(
                    func.sum(tokens).filter(UsageEvent.usage_source == UsageSource.ESTIMATED.value),
                    0,
                ),
                # Held: unknown outcomes, and open reservations (not yet known either).
                func.coalesce(
                    func.sum(tokens).filter(
                        (UsageEvent.usage_source == UsageSource.UNAVAILABLE.value)
                        | UsageEvent.usage_source.is_(None)
                    ),
                    0,
                ),
                func.count().filter(UsageEvent.usage_source == UsageSource.NONE.value),
            ).where(*scope)
        )
    ).one()
    outcomes = {
        (outcome or "in_progress"): count
        for outcome, count in (
            await session.execute(
                select(UsageEvent.outcome, func.count()).where(*scope).group_by(UsageEvent.outcome)
            )
        ).all()
    }
    by_operation = (
        await session.execute(
            select(UsageEvent.operation, func.sum(tokens), func.sum(cost), func.count(count))
            .where(*scope)
            .group_by(UsageEvent.operation)
            .order_by(func.sum(tokens).desc())
        )
    ).all()
    by_model = (
        await session.execute(
            select(UsageEvent.model, func.sum(tokens), func.sum(cost), func.count(count))
            .where(*scope)
            .group_by(UsageEvent.model)
            .order_by(func.sum(tokens).desc())
        )
    ).all()
    day = cast(func.timezone("UTC", UsageEvent.created_at), Date)
    chat = UsageEvent.operation.in_(GENERATIONS)
    daily = (
        await session.execute(
            select(
                day,
                func.coalesce(func.sum(tokens).filter(chat), 0),
                func.coalesce(func.sum(tokens).filter(~chat), 0),
                func.sum(cost),
            )
            .where(*scope)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    pricing = get_pricing()
    result = UsageSummary(
        workspace_id=workspace_id,
        scope="workspace" if everyone else "own",
        period_start=start,
        period_end=end,
        currency=pricing.currency,
        pricing_version=pricing.version,
        totals=UsageTotals(**_totals(total[0], total[1], total[2])),
        by_operation=[
            OperationUsage(operation=r[0], **_totals(r[1], r[2], r[3])) for r in by_operation
        ],
        by_model=[ModelUsage(model=r[0], **_totals(r[1], r[2], r[3])) for r in by_model],
        daily=[
            DailyUsage(
                date=r[0],
                chat_tokens=int(r[1]),
                embedding_tokens=int(r[2]),
                estimated_cost=Decimal(r[3] or 0),
            )
            for r in daily
        ],
        estimated_records=int(total[3] or 0),
        unavailable_records=int(total[4] or 0),
        accounting=ChargeBreakdown(
            provider_reported_tokens=int(total[5]),
            estimated_tokens=int(total[6]),
            held_tokens=int(total[7]),
            not_charged_records=int(total[8] or 0),
        ),
        outcomes={str(key): int(value) for key, value in outcomes.items()},
        allowance=await workspace_allowance(session, workspace_id, now),
        my_allowance=await user_allowance(session, user_id, now),
    )
    await session.rollback()
    return result


async def history(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    operation: str | None,
    status: str | None,
) -> UsageHistory:
    access = await workspace_service.get_access(session, workspace_id, user_id)
    everyone = access.role in MANAGERS
    scope = [UsageEvent.workspace_id == workspace_id]
    if not everyone:
        scope.append(UsageEvent.user_id == user_id)
    if operation is not None:
        scope.append(UsageEvent.operation == operation)
    if status is not None:
        scope.append(UsageEvent.status == status)
    total = await session.scalar(select(func.count()).select_from(UsageEvent).where(*scope))
    rows = await session.scalars(
        select(UsageEvent)
        .where(*scope)
        .order_by(UsageEvent.created_at.desc(), UsageEvent.id)
        .limit(limit)
        .offset(offset)
    )
    items = []
    for row in rows:
        item = UsageEventOut.model_validate(row)
        item.user_id = row.user_id if everyone else None
        items.append(item)
    await session.rollback()
    return UsageHistory(items=items, total=int(total or 0), limit=limit, offset=offset)


def _system(settings_limits: usage.Limits) -> QuotaLimits:
    return QuotaLimits(
        monthly_token_limit=settings_limits.workspace_monthly_tokens,
        monthly_cost_limit=settings_limits.workspace_monthly_cost,
        max_concurrent_operations=settings_limits.workspace_concurrent_operations,
    )


async def quota_policy(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> QuotaPolicyOut:
    access = await workspace_service.get_access(session, workspace_id, user_id)
    now = datetime.now(UTC)
    system = usage.system_limits(get_settings(), get_pricing())
    effective = await usage.effective_limits(session, workspace_id)
    own = await session.get(WorkspaceQuotaPolicy, workspace_id)
    result = QuotaPolicyOut(
        workspace_id=workspace_id,
        currency=system.currency,
        effective=_system(effective),
        system=_system(system),
        workspace_monthly_token_limit=own.monthly_token_limit if own else None,
        workspace_monthly_cost_limit=own.monthly_cost_limit if own else None,
        workspace_max_concurrent_operations=own.max_concurrent_operations if own else None,
        alert_threshold_percent=(own.alert_threshold_percent if own else None)
        or get_settings().usage_alert_threshold_percent,
        can_edit=access.role == WorkspaceRole.OWNER,
        allowance=await workspace_allowance(session, workspace_id, now),
    )
    await session.rollback()
    return result


async def update_quota_policy(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, body: QuotaPolicyUpdate
) -> QuotaPolicyOut:
    """Tighten (or relax back to the system limit) a workspace's limits. Owners only: limits
    decide what the workspace may spend."""
    access = await workspace_service.get_access(session, workspace_id, user_id, lock=True)
    workspace_service.require_role(access, WorkspaceRole.OWNER)
    system = usage.system_limits(get_settings(), get_pricing())
    changes = body.model_dump(exclude_unset=True)
    ceilings = {
        "monthly_token_limit": system.workspace_monthly_tokens,
        "monthly_cost_limit": system.workspace_monthly_cost,
        "max_concurrent_operations": system.workspace_concurrent_operations,
        "alert_threshold_percent": 100,
    }
    for field, value in changes.items():
        if value is not None and value > ceilings[field]:
            await session.rollback()
            raise AppError(
                422,
                "quota_above_system_limit",
                f"{field} can be at most {ceilings[field]}, the system limit. A workspace can "
                "only make its limits stricter.",
            )
    policy = await session.get(WorkspaceQuotaPolicy, workspace_id)
    if policy is None:
        policy = WorkspaceQuotaPolicy(workspace_id=workspace_id, updated_by=user_id)
        session.add(policy)
    before = {field: _plain(getattr(policy, field, None)) for field in changes}
    for field, value in changes.items():
        setattr(policy, field, value)
    policy.updated_by = user_id
    policy.updated_at = datetime.now(UTC)
    audit(session, workspace_id, user_id, "quota_policy.updated", before, changes)
    await session.commit()
    return await quota_policy(session, workspace_id, user_id)


async def my_usage(session: AsyncSession, user_id: uuid.UUID) -> MyUsage:
    """The caller's own usage today, across every workspace: their personal daily limits."""
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(
                func.sum(UsageEvent.charged_tokens),
                func.sum(UsageEvent.charged_cost),
                func.count(UsageEvent.id),
            ).where(UsageEvent.user_id == user_id, UsageEvent.created_at >= usage.day_start(now))
        )
    ).one()
    result = MyUsage(
        allowance=await user_allowance(session, user_id, now),
        currency=get_pricing().currency,
        today=UsageTotals(**_totals(row[0], row[1], row[2])),
        alerts=await my_alerts(session, user_id),
    )
    await session.rollback()
    return result


# --- Audit log ----------------------------------------------------------------------------


def _plain(value: object) -> object:
    return str(value) if isinstance(value, Decimal) else value


def audit(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    before: dict,
    after: dict,
    target_user_id: uuid.UUID | None = None,
) -> None:
    """Append a policy change to the audit log, in the caller's transaction: the change and
    its record are committed together or not at all."""
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=action,
            target_user_id=target_user_id,
            details={
                "before": {key: _plain(value) for key, value in before.items()},
                "after": {key: _plain(value) for key, value in after.items()},
            },
        )
    )


async def audit_log(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, *, limit: int, offset: int
) -> AuditPage:
    """Policy changes in a workspace, newest first. Owners and admins."""
    access = await workspace_service.get_access(session, workspace_id, user_id)
    workspace_service.require_role(access, *MANAGERS)
    scope = AuditEvent.workspace_id == workspace_id
    total = await session.scalar(select(func.count()).select_from(AuditEvent).where(scope))
    rows = await session.scalars(
        select(AuditEvent)
        .where(scope)
        .order_by(AuditEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = AuditPage(
        items=[AuditEventOut.model_validate(row) for row in rows], total=int(total or 0)
    )
    await session.rollback()
    return result


# --- Member quotas ------------------------------------------------------------------------


async def member_quotas(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[MemberQuotaOut]:
    """Every member's own limits and this month's use. Owners and admins."""
    access = await workspace_service.get_access(session, workspace_id, user_id)
    workspace_service.require_role(access, *MANAGERS)
    now = datetime.now(UTC)
    used = dict(
        (
            await session.execute(
                select(UsageEvent.user_id, func.sum(UsageEvent.charged_tokens))
                .where(
                    UsageEvent.workspace_id == workspace_id,
                    UsageEvent.created_at >= usage.month_start(now),
                )
                .group_by(UsageEvent.user_id)
            )
        ).all()
    )
    quotas = {
        quota.user_id: quota
        for quota in await session.scalars(
            select(WorkspaceMemberQuota).where(WorkspaceMemberQuota.workspace_id == workspace_id)
        )
    }
    members = await session.scalars(
        select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_id)
    )
    result = [
        MemberQuotaOut(
            user_id=member,
            monthly_token_limit=quotas[member].monthly_token_limit if member in quotas else None,
            daily_question_limit=quotas[member].daily_question_limit if member in quotas else None,
            monthly_tokens_used=int(used.get(member) or 0),
            updated_at=quotas[member].updated_at if member in quotas else None,
        )
        for member in members
    ]
    await session.rollback()
    return result


async def update_member_quota(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MemberQuotaUpdate,
) -> MemberQuotaOut:
    """Set a member's own limits in this workspace. Owners only, for members of this
    workspace only, and never above the workspace's or the system's limits."""
    access = await workspace_service.get_access(session, workspace_id, user_id, lock=True)
    workspace_service.require_role(access, WorkspaceRole.OWNER)
    is_member = await session.scalar(
        select(WorkspaceMember.id).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == member_id
        )
    )
    if is_member is None:
        await session.rollback()
        raise AppError(404, "member_not_found", "That user is not a member of this workspace.")
    limits = await usage.effective_limits(session, workspace_id)
    changes = body.model_dump(exclude_unset=True)
    ceilings = {
        "monthly_token_limit": limits.workspace_monthly_tokens,
        "daily_question_limit": limits.user_daily_chats,
    }
    for field, value in changes.items():
        if value is not None and value > ceilings[field]:
            await session.rollback()
            raise AppError(
                422,
                "quota_above_workspace_limit",
                f"{field} can be at most {ceilings[field]}: a member's limit can only be "
                "stricter than the workspace's.",
            )
    quota = await session.get(WorkspaceMemberQuota, (workspace_id, member_id))
    if quota is None:
        quota = WorkspaceMemberQuota(
            workspace_id=workspace_id, user_id=member_id, updated_by=user_id
        )
        session.add(quota)
    before = {field: getattr(quota, field, None) for field in changes}
    for field, value in changes.items():
        setattr(quota, field, value)
    quota.updated_by = user_id
    quota.updated_at = datetime.now(UTC)
    audit(session, workspace_id, user_id, "member_quota.updated", before, changes, member_id)
    await session.commit()
    return next(
        q for q in await member_quotas(session, workspace_id, user_id) if q.user_id == member_id
    )


# --- Alerts -------------------------------------------------------------------------------


async def workspace_alerts(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[AlertOut]:
    """This period's alerts for a workspace (every member is subject to its limits), and the
    caller's own daily alerts."""
    await workspace_service.get_access(session, workspace_id, user_id)
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(UsageAlert)
        .where(
            (
                (UsageAlert.workspace_id == workspace_id)
                & (UsageAlert.period_start >= usage.month_start(now))
            )
            | (
                (UsageAlert.user_id == user_id)
                & (UsageAlert.kind == AlertKind.USER_DAILY_TOKENS.value)
                & (UsageAlert.period_start >= usage.day_start(now))
            )
        )
        .order_by(UsageAlert.created_at.desc())
    )
    result = [AlertOut.model_validate(row) for row in rows]
    await session.rollback()
    return result


async def my_alerts(session: AsyncSession, user_id: uuid.UUID) -> list[AlertOut]:
    now = datetime.now(UTC)
    rows = await session.scalars(
        select(UsageAlert)
        .where(UsageAlert.user_id == user_id, UsageAlert.period_start >= usage.day_start(now))
        .order_by(UsageAlert.created_at.desc())
    )
    return [AlertOut.model_validate(row) for row in rows]
