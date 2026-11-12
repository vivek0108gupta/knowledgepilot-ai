"""Usage metering and quotas for every AI provider operation.

Lifecycle of an expensive operation (an answer, a document's embeddings):

    reserve   before the provider is called: lock, expire stale reservations, check every
              limit, and insert a `reserved` row holding the most the operation may use
    provider  the call itself
    finalize  exactly once: `completed`, `failed` or `cancelled`, with the provider's token
              counts when it reported them, this application's estimate when it did not, or
              nothing (the reservation stays charged) when nothing is known

A reservation that is never finalised (the process stopped) becomes `expired` once its
`expires_at` passes without a heartbeat: it then no longer counts as running. If the provider
call never began (`provider_called_at` is empty) nothing is charged; otherwise an upper bound
is held, because the provider may well have done the work. If the real outcome arrives later,
it replaces the held charge once (`late_reconciled_at`).

Every final row says what happened at the provider (`outcome`) separately from where its
numbers come from (`usage_source`): `provider` (reported), `estimated` (counted here),
`unavailable` (an upper bound is *held* because nothing is known), `none` (nothing used).
Reserved and held amounts are never written to the provider-reported columns.

Atomicity: `reserve` takes transaction-scoped advisory locks on the user and then the
workspace, always in that order, before reading any balance. Two concurrent reservations
for the same user or workspace therefore run one after the other, and cannot both spend the
same remaining allowance. The locks are released when the transaction ends.

Query embeddings are reserved like everything else, but do not take a concurrency slot:
they last a fraction of a second, and an answer's own question embedding must not compete
with the answer for its workspace's slots.

Nothing here logs text, prompts or keys: only IDs, operation names, codes and counts.
"""

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import request_id_var
from app.models.usage import (
    GENERATIONS,
    AlertKind,
    UsageAlert,
    UsageEvent,
    UsageOperation,
    UsageOutcome,
    UsageSource,
    UsageStatus,
    WorkspaceMemberQuota,
    WorkspaceQuotaPolicy,
)
from app.services.chunking import get_tokenizer
from app.services.pricing import Pricing, get_pricing
from app.services.rate_limits import usage_unavailable

logger = logging.getLogger(__name__)

PROVIDER = "openai"
RESERVED = UsageStatus.RESERVED.value
OPEN = UsageEvent.status == RESERVED
# Operations that hold a concurrency slot while they run.
SLOTTED = (*GENERATIONS, UsageOperation.DOCUMENT_EMBEDDING.value)
# Retry-After for limits on things running at once: they usually free up within seconds.
CONCURRENCY_RETRY_SECONDS = 5
# Added to an estimated prompt for message framing and the output schema.
PROMPT_OVERHEAD_TOKENS = 200


# --- Periods ------------------------------------------------------------------------------


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


def next_month(now: datetime) -> datetime:
    start = month_start(now)
    return (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )


def seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


# --- Limits -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limits:
    user_daily_tokens: int
    user_daily_chats: int
    user_concurrent_generations: int
    workspace_monthly_tokens: int
    workspace_monthly_cost: Decimal
    workspace_concurrent_operations: int
    currency: str
    # Which workspace limits are the workspace's own (stricter) setting, not the system's.
    overridden: tuple[str, ...] = ()


def system_limits(settings: Settings, pricing: Pricing) -> Limits:
    return Limits(
        user_daily_tokens=settings.user_daily_token_limit,
        user_daily_chats=settings.user_daily_chat_limit,
        user_concurrent_generations=settings.max_concurrent_generations_per_user,
        workspace_monthly_tokens=settings.workspace_monthly_token_limit,
        workspace_monthly_cost=settings.workspace_monthly_cost_limit,
        workspace_concurrent_operations=settings.max_concurrent_ai_operations_per_workspace,
        currency=pricing.currency,
    )


async def effective_limits(
    session: AsyncSession, workspace_id: uuid.UUID | None, settings: Settings | None = None
) -> Limits:
    """The system limits, tightened by the workspace's own policy where it has one. A
    workspace policy can only ever lower a limit."""
    settings = settings or get_settings()
    limits = system_limits(settings, get_pricing())
    if workspace_id is None:
        return limits
    policy = await session.get(WorkspaceQuotaPolicy, workspace_id)
    if policy is None:
        return limits
    overridden: list[str] = []
    tokens, cost, concurrent = (
        limits.workspace_monthly_tokens,
        limits.workspace_monthly_cost,
        limits.workspace_concurrent_operations,
    )
    if policy.monthly_token_limit is not None and policy.monthly_token_limit < tokens:
        tokens = policy.monthly_token_limit
        overridden.append("monthly_token_limit")
    if policy.monthly_cost_limit is not None and policy.monthly_cost_limit < cost:
        cost = policy.monthly_cost_limit
        overridden.append("monthly_cost_limit")
    if (
        policy.max_concurrent_operations is not None
        and policy.max_concurrent_operations < concurrent
    ):
        concurrent = policy.max_concurrent_operations
        overridden.append("max_concurrent_operations")
    return Limits(
        limits.user_daily_tokens,
        limits.user_daily_chats,
        limits.user_concurrent_generations,
        tokens,
        cost,
        concurrent,
        limits.currency,
        tuple(overridden),
    )


# --- Balances -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserBalance:
    tokens_today: int
    chats_today: int
    generations_running: int


@dataclass(frozen=True, slots=True)
class WorkspaceBalance:
    tokens_this_month: int
    cost_this_month: Decimal
    operations_running: int


def _active(now: datetime) -> Any:  # noqa: ANN401 - a SQL expression
    return and_(OPEN, UsageEvent.expires_at > now)


async def user_balance(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> UserBalance:
    today = UsageEvent.created_at >= day_start(now)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.charged_tokens).filter(today), 0),
                func.count().filter(today, UsageEvent.operation.in_(GENERATIONS)),
                func.count().filter(_active(now), UsageEvent.operation.in_(GENERATIONS)),
            ).where(UsageEvent.user_id == user_id)
        )
    ).one()
    return UserBalance(int(row[0]), int(row[1]), int(row[2]))


async def workspace_balance(
    session: AsyncSession, workspace_id: uuid.UUID, now: datetime
) -> WorkspaceBalance:
    month = UsageEvent.created_at >= month_start(now)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.charged_tokens).filter(month), 0),
                func.coalesce(func.sum(UsageEvent.charged_cost).filter(month), 0),
                func.count().filter(_active(now), UsageEvent.operation.in_(SLOTTED)),
            ).where(UsageEvent.workspace_id == workspace_id)
        )
    ).one()
    return WorkspaceBalance(int(row[0]), Decimal(row[1]), int(row[2]))


# --- Errors -------------------------------------------------------------------------------


def user_quota_exceeded(what: str, now: datetime) -> AppError:
    return AppError(
        429,
        "user_quota_exceeded",
        f"You have reached your daily {what} limit. It resets at midnight UTC.",
        retry_after_seconds=seconds_until(day_start(now) + timedelta(days=1), now),
    )


def workspace_quota_exceeded(what: str, now: datetime) -> AppError:
    return AppError(
        429,
        "workspace_quota_exceeded",
        f"This workspace has reached its monthly {what} limit. It resets on the first of the "
        "month (UTC); an owner can review the workspace's usage.",
        retry_after_seconds=seconds_until(next_month(now), now),
    )


def generation_limit_reached(limit: int) -> AppError:
    return AppError(
        429,
        "concurrent_generation_limit",
        f"You already have {limit} answers being written. Wait for one to finish.",
        retry_after_seconds=CONCURRENCY_RETRY_SECONDS,
    )


def workspace_busy() -> AppError:
    return AppError(
        429,
        "workspace_busy",
        "This workspace is running as many AI operations as it may at once. Try again shortly.",
        retry_after_seconds=CONCURRENCY_RETRY_SECONDS,
    )


def member_quota_exceeded(what: str, retry_after: int) -> AppError:
    return AppError(
        429,
        "member_quota_exceeded",
        f"You have reached the {what} limit the owner set for you in this workspace.",
        retry_after_seconds=retry_after,
    )


# --- Reservations -------------------------------------------------------------------------


async def lock(session: AsyncSession, kind: str, subject_id: uuid.UUID) -> None:
    """A transaction-scoped advisory lock on one subject, released when the transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"knowledgepilot-usage:{kind}:{subject_id}"},
    )


def held_tokens(estimated_input: int | None, reserved_input: int, reserved_output: int) -> int:
    """The upper bound held when an operation ran but its usage is unknown: the prompt as
    estimated when it was sent (or, failing that, the reserved input) plus the most output
    it could have produced."""
    return (estimated_input if estimated_input is not None else reserved_input) + reserved_output


async def expire_stale(
    session: AsyncSession,
    now: datetime,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> int:
    """Finalise reservations whose operation stopped sending heartbeats before `expires_at`.

    One never sent to the provider (`provider_called_at` empty) is charged nothing. One that
    was sent may have been billed, so an upper bound is held. Rows locked by a concurrent
    sweep are skipped, so two sweeps cannot expire the same reservation twice.
    """
    scope = []
    if user_id is not None:
        scope.append(UsageEvent.user_id == user_id)
    if workspace_id is not None:
        scope.append(UsageEvent.workspace_id == workspace_id)
    pricing = get_pricing()
    rows = (
        await session.execute(
            select(
                UsageEvent.id,
                UsageEvent.model,
                UsageEvent.provider_called_at,
                UsageEvent.prompt_tokens_sent,
                UsageEvent.reserved_input_tokens,
                UsageEvent.reserved_output_tokens,
            )
            .where(OPEN, UsageEvent.expires_at <= now, or_(*scope) if scope else text("true"))
            .with_for_update(skip_locked=True)
        )
    ).all()
    for row in rows:
        called = row.provider_called_at is not None
        held = (
            held_tokens(
                row.prompt_tokens_sent, row.reserved_input_tokens, row.reserved_output_tokens
            )
            if called
            else 0
        )
        held_input = held - row.reserved_output_tokens if called else 0
        cost = (
            pricing.cost(row.model, held_input, row.reserved_output_tokens)
            if called
            else Decimal(0)
        )
        await session.execute(
            update(UsageEvent)
            .where(UsageEvent.id == row.id, OPEN)
            .values(
                status=UsageStatus.EXPIRED.value,
                outcome=UsageOutcome.ABANDONED.value,
                usage_source=(UsageSource.UNAVAILABLE if called else UsageSource.NONE).value,
                error_code="reservation_expired",
                pricing_version=pricing.version,
                currency=pricing.currency,
                charged_tokens=held,
                estimated_cost=cost,
                charged_cost=cost or Decimal(0),
                completed_at=now,
                expired_at=now,
            )
        )
    if rows:
        logger.warning("Expired %d abandoned usage reservation(s)", len(rows))
    return len(rows)


async def sweep_expired(session: AsyncSession) -> int:
    """Expire every abandoned reservation, whoever made it. Run at startup and periodically,
    so a crashed process's reservations are recovered even in a workspace nobody uses."""
    try:
        expired = await expire_stale(session, datetime.now(UTC))
        await session.commit()
        return expired
    except SQLAlchemyError as error:
        await session.rollback()
        logger.error("Sweeping expired reservations failed: %s", type(error).__name__)
        return 0


async def mark_called(
    session: AsyncSession, reservation_id: uuid.UUID, estimated_input_tokens: int
) -> None:
    """Record that the provider call is about to begin, and the prompt's estimated size.
    From here on, if the operation is lost, it may have been billed."""
    now = datetime.now(UTC)
    await session.execute(
        update(UsageEvent)
        .where(UsageEvent.id == reservation_id, OPEN)
        .values(provider_called_at=now, heartbeat_at=now, prompt_tokens_sent=estimated_input_tokens)
    )
    await session.commit()


async def heartbeat(session: AsyncSession, reservation_id: uuid.UUID, ttl_seconds: int) -> None:
    """Show the operation is still running: push its expiry out by `ttl_seconds`. A long
    stream or ingestion is therefore never mistaken for an abandoned one."""
    now = datetime.now(UTC)
    try:
        await session.execute(
            update(UsageEvent)
            .where(UsageEvent.id == reservation_id, OPEN)
            .values(heartbeat_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
        )
        await session.commit()
    except SQLAlchemyError as error:
        await session.rollback()
        logger.error("Usage heartbeat failed: %s", type(error).__name__)


async def request_cancel(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Note that the user asked to stop the answer being written in a conversation, so that
    when its stream ends it is recorded as cancelled by the user, not as a lost connection."""
    result = await session.execute(
        update(UsageEvent)
        .where(
            OPEN,
            UsageEvent.conversation_id == conversation_id,
            UsageEvent.user_id == user_id,
            UsageEvent.operation.in_(GENERATIONS),
        )
        .values(cancel_requested_at=datetime.now(UTC))
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Reservation:
    id: uuid.UUID
    operation: str
    model: str
    input_tokens: int
    output_tokens: int


async def reserve(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    operation: UsageOperation,
    model: str,
    input_tokens: int,
    output_tokens: int,
    idempotency_key: str,
    ttl_seconds: int,
    conversation_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    commit: bool = True,
    now: datetime | None = None,
) -> Reservation:
    """Check every limit and reserve the most this operation may use.

    With `commit=False` the reservation joins the caller's transaction (and its locks are
    held until the caller commits), so it is stored if and only if the caller's own changes
    are, for example together with the question it answers.

    Raises 429 with a specific code when a limit would be exceeded, and 503 when usage
    cannot be checked: an operation is never run unchecked.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("reserved token counts cannot be negative")
    now = now or datetime.now(UTC)
    settings = get_settings()
    pricing = get_pricing()
    generation = operation.value in GENERATIONS
    slotted = operation.value in SLOTTED
    tokens = input_tokens + output_tokens
    cost = pricing.cost(model, input_tokens, output_tokens)
    try:
        await lock(session, "user", user_id)
        await lock(session, "workspace", workspace_id)
        await expire_stale(session, now, user_id=user_id, workspace_id=workspace_id)
        limits = await effective_limits(session, workspace_id, settings)
        user = await user_balance(session, user_id, now)
        workspace = await workspace_balance(session, workspace_id, now)

        if generation and user.generations_running >= limits.user_concurrent_generations:
            raise generation_limit_reached(limits.user_concurrent_generations)
        if slotted and workspace.operations_running >= limits.workspace_concurrent_operations:
            raise workspace_busy()
        if generation and user.chats_today >= limits.user_daily_chats:
            raise user_quota_exceeded("question", now)
        if user.tokens_today + tokens > limits.user_daily_tokens:
            raise user_quota_exceeded("AI usage", now)
        if workspace.tokens_this_month + tokens > limits.workspace_monthly_tokens:
            raise workspace_quota_exceeded("AI usage", now)
        if cost is not None and workspace.cost_this_month + cost > limits.workspace_monthly_cost:
            raise workspace_quota_exceeded("spending", now)
        await check_member_quota(session, workspace_id, user_id, tokens, generation, now)

        event_id = uuid.uuid4()
        await session.execute(
            insert(UsageEvent).values(
                id=event_id,
                workspace_id=workspace_id,
                user_id=user_id,
                operation=operation.value,
                provider=PROVIDER,
                model=model,
                status=RESERVED,
                reserved_input_tokens=input_tokens,
                reserved_output_tokens=output_tokens,
                charged_tokens=tokens,
                reserved_cost=cost,
                charged_cost=cost or Decimal(0),
                currency=pricing.currency,
                pricing_version=pricing.version,
                request_id=request_id_var.get(),
                idempotency_key=idempotency_key,
                conversation_id=conversation_id,
                document_id=document_id,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
        )
        if commit:
            await session.commit()
    except AppError:
        await session.rollback()
        raise
    except IntegrityError as error:
        await session.rollback()
        logger.warning("Refused a second reservation for one %s operation", operation.value)
        raise AppError(
            409, "duplicate_operation", "This operation is already being processed."
        ) from error
    except SQLAlchemyError as error:
        await session.rollback()
        logger.error("Usage reservation failed: %s", type(error).__name__)
        raise usage_unavailable() from error
    return Reservation(event_id, operation.value, model, input_tokens, output_tokens)


@dataclass(frozen=True, slots=True)
class Outcome:
    """How an operation ended, and what is known about what it used."""

    status: UsageStatus
    source: UsageSource
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    error_code: str | None = None
    message_id: uuid.UUID | None = None
    # What happened at the provider. Derived from `source` when not given.
    outcome: UsageOutcome | None = None

    def provider_outcome(self) -> UsageOutcome:
        if self.outcome is not None:
            return self.outcome
        return {
            UsageSource.PROVIDER: UsageOutcome.EXECUTED_WITH_USAGE,
            UsageSource.ESTIMATED: UsageOutcome.EXECUTED_WITHOUT_USAGE,
            UsageSource.UNAVAILABLE: UsageOutcome.UNKNOWN,
            UsageSource.NONE: UsageOutcome.NOT_CALLED,
        }[self.source]


def charges(
    outcome: Outcome,
    reserved_input: int,
    reserved_output: int,
    estimated_input: int | None,
    model: str,
    pricing: Pricing,
) -> dict[str, Any]:
    """The columns that record an outcome. Provider numbers and estimates go to separate
    columns; `charged_*` holds whichever counts. A held upper bound is only ever written to
    `charged_*`, never presented as usage."""
    values: dict[str, Any] = {}
    if outcome.source in (UsageSource.PROVIDER, UsageSource.ESTIMATED):
        used_in, used_out = outcome.input_tokens or 0, outcome.output_tokens or 0
        if used_in < 0 or used_out < 0:
            raise ValueError("token counts cannot be negative")
        cost = pricing.cost(model, used_in, used_out)
        if outcome.source == UsageSource.PROVIDER:
            values.update(
                input_tokens=used_in, output_tokens=used_out, total_tokens=used_in + used_out
            )
        else:
            values.update(estimated_input_tokens=used_in, estimated_output_tokens=used_out)
        values.update(
            charged_tokens=used_in + used_out, estimated_cost=cost, charged_cost=cost or Decimal(0)
        )
    elif outcome.source == UsageSource.NONE:
        values.update(charged_tokens=0, estimated_cost=Decimal(0), charged_cost=Decimal(0))
    else:  # UNAVAILABLE: it may have run and nothing is known, so an upper bound is held.
        held = held_tokens(estimated_input, reserved_input, reserved_output)
        cost = pricing.cost(model, held - reserved_output, reserved_output)
        values.update(charged_tokens=held, estimated_cost=cost, charged_cost=cost or Decimal(0))
    return values


async def finalize(
    session: AsyncSession, reservation_id: uuid.UUID, outcome: Outcome, *, commit: bool = True
) -> bool:
    """Record how a reserved operation ended. Only the first call for a reservation has any
    effect, so a terminal path that runs twice cannot record usage twice.

    If the reservation was already expired as abandoned, the real outcome replaces the held
    charge once, and `late_reconciled_at` records that it did. Returns whether this call
    recorded anything."""
    pricing = get_pricing()
    row = (
        await session.execute(
            select(
                UsageEvent.status,
                UsageEvent.model,
                UsageEvent.reserved_input_tokens,
                UsageEvent.reserved_output_tokens,
                UsageEvent.prompt_tokens_sent,
                UsageEvent.cancel_requested_at,
                UsageEvent.workspace_id,
                UsageEvent.user_id,
            )
            .where(
                UsageEvent.id == reservation_id,
                or_(
                    OPEN,
                    and_(
                        UsageEvent.status == UsageStatus.EXPIRED.value,
                        UsageEvent.late_reconciled_at.is_(None),
                    ),
                ),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        return False
    late = row.status == UsageStatus.EXPIRED.value
    model = outcome.model or row.model
    provider_outcome = outcome.provider_outcome()
    if provider_outcome == UsageOutcome.CLIENT_DISCONNECTED and row.cancel_requested_at is not None:
        provider_outcome = UsageOutcome.CANCELLED_BY_USER
    values = charges(
        outcome,
        row.reserved_input_tokens,
        row.reserved_output_tokens,
        row.prompt_tokens_sent,
        model,
        pricing,
    )
    now = datetime.now(UTC)
    if late:
        values["late_reconciled_at"] = now
    result = await session.execute(
        update(UsageEvent)
        .where(UsageEvent.id == reservation_id, UsageEvent.status == row.status)
        .values(
            status=outcome.status.value,
            usage_source=outcome.source.value,
            outcome=provider_outcome.value,
            # The prices actually used, which may be newer than those at reservation.
            pricing_version=pricing.version,
            currency=pricing.currency,
            model=model,
            error_code=outcome.error_code,
            message_id=outcome.message_id,
            completed_at=now,
            **values,
        )
    )
    recorded = bool(result.rowcount)  # type: ignore[attr-defined]
    if recorded and row.workspace_id is not None:
        await record_alerts(session, row.workspace_id, row.user_id, now)
    if commit:
        await session.commit()
    if recorded:
        logger.info(
            "Usage %s: %s, %s, %s, %d token(s)%s",
            reservation_id,
            outcome.status.value,
            provider_outcome.value,
            outcome.source.value,
            values["charged_tokens"],
            " (after it had expired)" if late else "",
        )
    return recorded


# --- Member quotas and alerts -------------------------------------------------------------


async def check_member_quota(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    tokens: int,
    generation: bool,
    now: datetime,
) -> None:
    """Apply the limits a workspace owner set for this member, if any."""
    quota = await session.get(WorkspaceMemberQuota, (workspace_id, user_id))
    if quota is None:
        return
    mine = and_(UsageEvent.workspace_id == workspace_id, UsageEvent.user_id == user_id)
    month, today = (
        UsageEvent.created_at >= month_start(now),
        UsageEvent.created_at >= day_start(now),
    )
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageEvent.charged_tokens).filter(month), 0),
                func.count().filter(today, UsageEvent.operation.in_(GENERATIONS)),
            ).where(mine)
        )
    ).one()
    if quota.monthly_token_limit is not None and int(row[0]) + tokens > quota.monthly_token_limit:
        raise member_quota_exceeded("monthly AI usage", seconds_until(next_month(now), now))
    if (
        generation
        and quota.daily_question_limit is not None
        and int(row[1]) >= quota.daily_question_limit
    ):
        raise member_quota_exceeded(
            "daily question", seconds_until(day_start(now) + timedelta(days=1), now)
        )


def thresholds(percent: int) -> list[int]:
    return sorted({percent, 100})


async def record_alerts(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> None:
    """Record each usage threshold newly crossed: the workspace's monthly tokens and cost,
    and the user's daily tokens. Once per subject, kind, threshold and period (a unique
    constraint), so recording is idempotent and concurrent finalisations cannot duplicate."""
    settings = get_settings()
    limits = await effective_limits(session, workspace_id, settings)
    policy = await session.get(WorkspaceQuotaPolicy, workspace_id)
    percent = (
        policy.alert_threshold_percent if policy and policy.alert_threshold_percent else None
    ) or settings.usage_alert_threshold_percent
    workspace = await workspace_balance(session, workspace_id, now)
    user = await user_balance(session, user_id, now)
    checks = [
        (
            AlertKind.WORKSPACE_TOKENS,
            workspace_id,
            workspace_id,
            None,
            Decimal(workspace.tokens_this_month),
            Decimal(limits.workspace_monthly_tokens),
            month_start(now),
            thresholds(percent),
        ),
        (
            AlertKind.WORKSPACE_COST,
            workspace_id,
            workspace_id,
            None,
            workspace.cost_this_month,
            limits.workspace_monthly_cost,
            month_start(now),
            thresholds(percent),
        ),
        (
            AlertKind.USER_DAILY_TOKENS,
            user_id,
            None,
            user_id,
            Decimal(user.tokens_today),
            Decimal(limits.user_daily_tokens),
            day_start(now),
            thresholds(settings.usage_alert_threshold_percent),
        ),
    ]
    for kind, subject, workspace_ref, user_ref, used, limit, period, levels in checks:
        if limit <= 0:
            continue
        for level in levels:
            if used * 100 >= limit * level:
                await session.execute(
                    pg_insert(UsageAlert)
                    .values(
                        kind=kind.value,
                        subject_id=subject,
                        workspace_id=workspace_ref,
                        user_id=user_ref,
                        threshold_percent=level,
                        period_start=period,
                        used=used,
                        limit_value=limit,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(constraint="uq_usage_alerts_once")
                )


# --- Estimates ----------------------------------------------------------------------------


def count_tokens(text_value: str) -> int:
    """This application's own token count. An estimate of what a provider would bill: the
    tokenizer may differ from the model's."""
    return len(get_tokenizer().encode(text_value)) if text_value else 0


@dataclass
class GenerationMeter:
    """What is known about one model call as it happens, so that however it ends the right
    usage and outcome are recorded."""

    reservation_id: uuid.UUID
    called: bool = False
    streamed: bool = False
    prompt_tokens: int = 0
    received: list[str] = field(default_factory=list)
    provider_input: int | None = None
    provider_output: int | None = None
    provider_model: str | None = None
    # The whole output, when a non-streamed response arrived without usage.
    output_text: str | None = None
    # False when the last failure is known not to have run (a rate limit refused it).
    maybe_executed: bool = True

    def started(self, instructions: str, turns: list[Any], *, streamed: bool = False) -> None:
        self.called = True
        self.streamed = streamed
        self.prompt_tokens = (
            count_tokens(instructions)
            + sum(count_tokens(turn.content) for turn in turns)
            + PROMPT_OVERHEAD_TOKENS
        )

    def receive(self, text_value: str) -> None:
        self.received.append(text_value)

    def reported(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        model: str | None,
        text_value: str | None = None,
    ) -> None:
        if input_tokens is not None and output_tokens is not None:
            self.provider_input, self.provider_output = input_tokens, output_tokens
        if model:
            self.provider_model = model
        if text_value is not None:
            self.output_text = text_value

    def outcome(
        self,
        status: UsageStatus,
        *,
        error_code: str | None = None,
        message_id: uuid.UUID | None = None,
        refused_by_provider: bool = False,
    ) -> Outcome:
        def make(
            source: UsageSource,
            kind: UsageOutcome,
            used_in: int | None = None,
            used_out: int | None = None,
        ) -> Outcome:
            return Outcome(
                status, source, used_in, used_out, self.provider_model, error_code, message_id, kind
            )

        if self.provider_input is not None:
            return make(
                UsageSource.PROVIDER,
                UsageOutcome.EXECUTED_WITH_USAGE,
                self.provider_input,
                self.provider_output,
            )
        if not self.called:
            return make(UsageSource.NONE, UsageOutcome.NOT_CALLED)
        if refused_by_provider or not self.maybe_executed:
            # Rejected (authentication, a bad request) or refused by a rate limit: no work.
            return make(UsageSource.NONE, UsageOutcome.REJECTED)
        if self.output_text is not None:
            # A whole response arrived, without usage: count what was sent and received.
            return make(
                UsageSource.ESTIMATED,
                UsageOutcome.EXECUTED_WITHOUT_USAGE,
                self.prompt_tokens,
                count_tokens(self.output_text),
            )
        received = "".join(self.received)
        if self.streamed and status == UsageStatus.CANCELLED:
            # Stopped by the client. `finalize` tells a user's Stop from a lost connection.
            return make(
                UsageSource.ESTIMATED,
                UsageOutcome.CLIENT_DISCONNECTED,
                self.prompt_tokens,
                count_tokens(received),
            )
        if self.streamed and received:
            # Failed after some output: the prompt and what came back are known.
            return make(
                UsageSource.ESTIMATED,
                UsageOutcome.PARTIAL_STREAM,
                self.prompt_tokens,
                count_tokens(received),
            )
        # Timed out or lost with nothing back: it may have run in full. Hold an upper bound.
        return make(UsageSource.UNAVAILABLE, UsageOutcome.UNKNOWN)
