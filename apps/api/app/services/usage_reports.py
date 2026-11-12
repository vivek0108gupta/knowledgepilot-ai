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
from app.models.usage import GENERATIONS, UsageEvent, UsageSource, WorkspaceQuotaPolicy
from app.models.workspace import WorkspaceRole
from app.schemas.usage import (
    DailyUsage,
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
            ).where(*scope)
        )
    ).one()
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
    for field, value in changes.items():
        setattr(policy, field, value)
    policy.updated_by = user_id
    policy.updated_at = datetime.now(UTC)
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
    )
    await session.rollback()
    return result
