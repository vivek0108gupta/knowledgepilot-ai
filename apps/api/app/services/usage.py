"""Usage metering and quotas for every AI provider operation.

Lifecycle of an expensive operation (an answer, a document's embeddings):

    reserve   before the provider is called: lock, expire stale reservations, check every
              limit, and insert a `reserved` row holding the most the operation may use
    provider  the call itself
    finalize  exactly once: `completed`, `failed` or `cancelled`, with the provider's token
              counts when it reported them, this application's estimate when it did not, or
              nothing (the reservation stays charged) when nothing is known

A reservation that is never finalised (the process stopped) becomes `expired` once its
`expires_at` passes: it then no longer counts as running, and its reserved amount stays
charged, because the provider may well have done the work.

Atomicity: `reserve` takes transaction-scoped advisory locks on the user and then the
workspace, always in that order, before reading any balance. Two concurrent reservations
for the same user or workspace therefore run one after the other, and cannot both spend the
same remaining allowance. The locks are released when the transaction ends.

Small, cheap operations (embedding a search query) are not reserved: they are checked
against the remaining allowance, run, and recorded (`record`).

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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import request_id_var
from app.models.usage import (
    GENERATIONS,
    UsageEvent,
    UsageOperation,
    UsageSource,
    UsageStatus,
    WorkspaceQuotaPolicy,
)
from app.services.chunking import get_tokenizer
from app.services.pricing import Pricing, get_pricing
from app.services.rate_limits import usage_unavailable

logger = logging.getLogger(__name__)

PROVIDER = "openai"
RESERVED = UsageStatus.RESERVED.value
OPEN = UsageEvent.status == RESERVED
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
                func.count().filter(_active(now)),
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


# --- Reservations -------------------------------------------------------------------------


async def lock(session: AsyncSession, kind: str, subject_id: uuid.UUID) -> None:
    """A transaction-scoped advisory lock on one subject, released when the transaction ends."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"knowledgepilot-usage:{kind}:{subject_id}"},
    )


async def expire_stale(
    session: AsyncSession,
    now: datetime,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
) -> int:
    """Mark reservations past their expiry as `expired`. Their reserved amount stays charged:
    the provider may have done the work before the process that reserved it stopped."""
    scope = []
    if user_id is not None:
        scope.append(UsageEvent.user_id == user_id)
    if workspace_id is not None:
        scope.append(UsageEvent.workspace_id == workspace_id)
    result = await session.execute(
        update(UsageEvent)
        .where(OPEN, UsageEvent.expires_at <= now, or_(*scope) if scope else text("true"))
        .values(
            status=UsageStatus.EXPIRED.value,
            usage_source=UsageSource.UNAVAILABLE.value,
            error_code="reservation_expired",
            completed_at=now,
        )
    )
    expired = result.rowcount or 0  # type: ignore[attr-defined]
    if expired:
        logger.warning("Expired %d abandoned usage reservation(s)", expired)
    return expired


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
        if workspace.operations_running >= limits.workspace_concurrent_operations:
            raise workspace_busy()
        if generation and user.chats_today >= limits.user_daily_chats:
            raise user_quota_exceeded("question", now)
        if user.tokens_today + tokens > limits.user_daily_tokens:
            raise user_quota_exceeded("AI usage", now)
        if workspace.tokens_this_month + tokens > limits.workspace_monthly_tokens:
            raise workspace_quota_exceeded("AI usage", now)
        if cost is not None and workspace.cost_this_month + cost > limits.workspace_monthly_cost:
            raise workspace_quota_exceeded("spending", now)

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


def charges(
    outcome: Outcome,
    reserved_tokens: int,
    reserved_cost: Decimal | None,
    model: str,
    pricing: Pricing,
) -> dict[str, Any]:
    """The columns that record an outcome. Provider numbers and estimates go to separate
    columns; `charged_*` holds whichever counts."""
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
    else:  # UNAVAILABLE: nothing is known, so the reservation stays charged.
        values.update(
            charged_tokens=reserved_tokens,
            estimated_cost=reserved_cost,
            charged_cost=reserved_cost or Decimal(0),
        )
    return values


async def finalize(
    session: AsyncSession, reservation_id: uuid.UUID, outcome: Outcome, *, commit: bool = True
) -> bool:
    """Record how a reserved operation ended. Only the first call for a reservation has any
    effect, so a terminal path that runs twice cannot record usage twice. Returns whether
    this call recorded it."""
    pricing = get_pricing()
    row = (
        await session.execute(
            select(
                UsageEvent.model,
                UsageEvent.reserved_input_tokens + UsageEvent.reserved_output_tokens,
                UsageEvent.reserved_cost,
            ).where(UsageEvent.id == reservation_id, OPEN)
        )
    ).one_or_none()
    if row is None:
        return False
    model = outcome.model or row[0]
    values = charges(outcome, int(row[1]), row[2], model, pricing)
    result = await session.execute(
        update(UsageEvent)
        .where(UsageEvent.id == reservation_id, OPEN)
        .values(
            status=outcome.status.value,
            usage_source=outcome.source.value,
            model=model,
            error_code=outcome.error_code,
            message_id=outcome.message_id,
            completed_at=datetime.now(UTC),
            **values,
        )
    )
    if commit:
        await session.commit()
    recorded = bool(result.rowcount)  # type: ignore[attr-defined]
    if recorded:
        logger.info(
            "Usage %s: %s, %s, %d token(s)",
            reservation_id,
            outcome.status.value,
            outcome.source.value,
            values["charged_tokens"],
        )
    return recorded


async def ensure_available(
    session: AsyncSession, user_id: uuid.UUID, workspace_id: uuid.UUID, tokens: int
) -> None:
    """Refuse a small unreserved operation when the allowance it needs is already spent."""
    now = datetime.now(UTC)
    try:
        limits = await effective_limits(session, workspace_id)
        user = await user_balance(session, user_id, now)
        workspace = await workspace_balance(session, workspace_id, now)
    except SQLAlchemyError as error:
        await session.rollback()
        logger.error("Usage check failed: %s", type(error).__name__)
        raise usage_unavailable() from error
    if user.tokens_today + tokens > limits.user_daily_tokens:
        raise user_quota_exceeded("AI usage", now)
    if workspace.tokens_this_month + tokens > limits.workspace_monthly_tokens:
        raise workspace_quota_exceeded("AI usage", now)
    if workspace.cost_this_month >= limits.workspace_monthly_cost:
        raise workspace_quota_exceeded("spending", now)


async def record(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    operation: UsageOperation,
    model: str,
    outcome: Outcome,
    conversation_id: uuid.UUID | None = None,
) -> None:
    """Record a small operation that was not reserved. Never raises: failing to record
    such an operation must not fail the request it served, so the failure is logged."""
    pricing = get_pricing()
    now = datetime.now(UTC)
    values = charges(outcome, 0, None, outcome.model or model, pricing)
    try:
        await session.execute(
            insert(UsageEvent).values(
                workspace_id=workspace_id,
                user_id=user_id,
                operation=operation.value,
                provider=PROVIDER,
                model=outcome.model or model,
                status=outcome.status.value,
                usage_source=outcome.source.value,
                reserved_input_tokens=0,
                reserved_output_tokens=0,
                currency=pricing.currency,
                pricing_version=pricing.version,
                request_id=request_id_var.get(),
                idempotency_key=f"{operation.value}:{uuid.uuid4()}",
                conversation_id=conversation_id,
                error_code=outcome.error_code,
                created_at=now,
                completed_at=now,
                expires_at=now,
                **values,
            )
        )
        await session.commit()
    except SQLAlchemyError as error:
        await session.rollback()
        logger.error("Could not record %s usage: %s", operation.value, type(error).__name__)


# --- Estimates ----------------------------------------------------------------------------


def count_tokens(text_value: str) -> int:
    """This application's own token count. An estimate of what a provider would bill: the
    tokenizer may differ from the model's."""
    return len(get_tokenizer().encode(text_value)) if text_value else 0


@dataclass
class GenerationMeter:
    """What is known about one model call as it happens, so that however it ends the right
    usage can be recorded: the provider's numbers when there are any, otherwise an estimate
    from what was sent and received, otherwise nothing."""

    reservation_id: uuid.UUID
    called: bool = False
    streamed: bool = False
    prompt_tokens: int = 0
    received: list[str] = field(default_factory=list)
    provider_input: int | None = None
    provider_output: int | None = None
    provider_model: str | None = None

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
        self, input_tokens: int | None, output_tokens: int | None, model: str | None
    ) -> None:
        if input_tokens is not None and output_tokens is not None:
            self.provider_input, self.provider_output = input_tokens, output_tokens
        if model:
            self.provider_model = model

    def outcome(
        self,
        status: UsageStatus,
        *,
        error_code: str | None = None,
        message_id: uuid.UUID | None = None,
        refused_by_provider: bool = False,
    ) -> Outcome:
        if self.provider_input is not None:
            return Outcome(
                status,
                UsageSource.PROVIDER,
                self.provider_input,
                self.provider_output,
                self.provider_model,
                error_code,
                message_id,
            )
        if not self.called or refused_by_provider:
            # Never sent, or rejected before any work (authentication, a bad request).
            return Outcome(
                status,
                UsageSource.NONE,
                model=self.provider_model,
                error_code=error_code,
                message_id=message_id,
            )
        if self.streamed:
            # The prompt was sent and this much came back before the stream ended. The
            # provider may have generated a little more before it noticed.
            return Outcome(
                status,
                UsageSource.ESTIMATED,
                self.prompt_tokens,
                count_tokens("".join(self.received)),
                self.provider_model,
                error_code,
                message_id,
            )
        # A whole response was awaited and none arrived: how much was used is unknown.
        return Outcome(
            status,
            UsageSource.UNAVAILABLE,
            model=self.provider_model,
            error_code=error_code,
            message_id=message_id,
        )
