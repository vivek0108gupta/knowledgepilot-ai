"""Usage metering, quotas and rate limits.

`usage_events` is a ledger with one row per provider operation (an answer, the embedding
of a question, the embedding of a document). A row is created as a reservation before the
provider is called, and finalised exactly once. Token counts are kept apart by where they
came from, and never mixed:

    reserved_*   the most the operation was allowed to use, set before the call
    input/output/total_tokens   what the provider reported, when it did
    estimated_*  this application's own count, when the provider reported nothing
    charged_*    what counts against quotas: the provider's numbers if there are any, else
                 the estimate, else the reservation
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.workspace import utc_now

COST = Numeric(16, 8)


class UsageOperation(StrEnum):
    CHAT = "chat"
    CHAT_STREAM = "chat_stream"
    QUERY_EMBEDDING = "query_embedding"
    DOCUMENT_EMBEDDING = "document_embedding"


GENERATIONS = (UsageOperation.CHAT.value, UsageOperation.CHAT_STREAM.value)


class UsageStatus(StrEnum):
    RESERVED = "reserved"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Never finalised before `expires_at`, for example because the process stopped.
    EXPIRED = "expired"


class UsageSource(StrEnum):
    PROVIDER = "provider"  # the provider reported the token counts
    ESTIMATED = "estimated"  # counted here; the provider reported nothing
    UNAVAILABLE = "unavailable"  # nothing is known; the reservation is charged
    NONE = "none"  # the provider was not called, or refused before doing any work


class UsageOutcome(StrEnum):
    """What happened at the provider, independently of where the token numbers came from."""

    NOT_CALLED = "not_called"  # the provider was never called
    REJECTED = "rejected"  # refused before doing any work (authentication, bad request)
    EXECUTED_WITH_USAGE = "executed_with_usage"  # ran, and reported its usage
    EXECUTED_WITHOUT_USAGE = "executed_without_usage"  # ran, but reported no usage
    UNKNOWN = "unknown"  # a timeout or lost connection: it may or may not have run
    PARTIAL_STREAM = "partial_stream"  # a stream failed after some output arrived
    CANCELLED_BY_USER = "cancelled_by_user"  # the user asked to stop
    CLIENT_DISCONNECTED = "client_disconnected"  # the connection closed without a request
    ABANDONED = "abandoned"  # never finalised: the process stopped or lost track of it


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(_in("operation", UsageOperation), name="operation_valid"),
        CheckConstraint(_in("status", UsageStatus), name="status_valid"),
        CheckConstraint(
            f"usage_source IS NULL OR {_in('usage_source', UsageSource)}", name="source_valid"
        ),
        CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens >= 0 "
            "AND (input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0) "
            "AND (total_tokens IS NULL OR total_tokens >= 0) "
            "AND (estimated_input_tokens IS NULL OR estimated_input_tokens >= 0) "
            "AND (estimated_output_tokens IS NULL OR estimated_output_tokens >= 0) "
            "AND charged_tokens >= 0",
            name="tokens_non_negative",
        ),
        CheckConstraint(
            "(reserved_cost IS NULL OR reserved_cost >= 0) "
            "AND (estimated_cost IS NULL OR estimated_cost >= 0) AND charged_cost >= 0",
            name="cost_non_negative",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_valid"),
        CheckConstraint(f"outcome IS NULL OR {_in('outcome', UsageOutcome)}", name="outcome_valid"),
        # A reservation is open; anything else is final and has a completion time.
        CheckConstraint(
            "(status = 'reserved') = (completed_at IS NULL)", name="completed_at_matches_status"
        ),
        Index("ix_usage_events_workspace_created", "workspace_id", "created_at"),
        Index("ix_usage_events_user_created", "user_id", "created_at"),
        Index(
            "ix_usage_events_open",
            "status",
            "expires_at",
            postgresql_where=text("status = 'reserved'"),
        ),
        Index("ix_usage_events_workspace_operation", "workspace_id", "operation"),
        Index("ix_usage_events_workspace_model", "workspace_id", "model"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # SET NULL, not CASCADE: deleting a workspace must not erase what its members used, or
    # deleting and recreating workspaces would reset their daily quotas.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    # A Supabase Auth user ID from a verified token (not a foreign key, as elsewhere).
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    operation: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32), default="openai")
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default=UsageStatus.RESERVED.value)
    usage_source: Mapped[str | None] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(32))

    reserved_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_input_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_output_tokens: Mapped[int | None] = mapped_column(Integer)
    charged_tokens: Mapped[int] = mapped_column(BigInteger, default=0)

    reserved_cost: Mapped[Decimal | None] = mapped_column(COST)
    estimated_cost: Mapped[Decimal | None] = mapped_column(COST)
    charged_cost: Mapped[Decimal] = mapped_column(COST, default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))
    pricing_version: Mapped[str] = mapped_column(String(64))

    request_id: Mapped[str | None] = mapped_column(String(64))
    # One provider operation, one row: a second reservation for the same operation fails.
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="SET NULL"), unique=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When the provider call began: a reservation that expires without one was never sent.
    provider_called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # This application's count of the prompt actually sent, taken when the call began. The
    # basis of the upper bound held if the outcome is never known.
    prompt_tokens_sent: Mapped[int | None] = mapped_column(Integer)
    # Last sign of life from the operation; each one pushes `expires_at` out again.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The user asked to stop (as opposed to the connection just closing).
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Audit trail of recovery: when it was found abandoned, and when (if ever) the real
    # outcome arrived afterwards and replaced the conservative charge.
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    late_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceQuotaPolicy(Base):
    """A workspace's own limits. Each may only be stricter than the system's (NULL: the
    system limit applies)."""

    __tablename__ = "workspace_quota_policies"
    __table_args__ = (
        CheckConstraint(
            "(monthly_token_limit IS NULL OR monthly_token_limit >= 0) "
            "AND (monthly_cost_limit IS NULL OR monthly_cost_limit >= 0) "
            "AND (max_concurrent_operations IS NULL OR max_concurrent_operations >= 1) "
            "AND (alert_threshold_percent IS NULL "
            "OR alert_threshold_percent BETWEEN 1 AND 100)",
            name="limits_valid",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    monthly_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    monthly_cost_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    max_concurrent_operations: Mapped[int | None] = mapped_column(Integer)
    # Record an alert when this percentage of a monthly limit is used (NULL: the default).
    alert_threshold_percent: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utc_now
    )


class RateLimitBucket(Base):
    """Requests counted per policy, subject (a user or a workspace) and fixed window."""

    __tablename__ = "rate_limit_buckets"
    __table_args__ = (CheckConstraint("count >= 0", name="count_non_negative"),)

    policy: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class WorkspaceMemberQuota(Base):
    """A member's own limits within one workspace, set by the workspace's owner. Like a
    workspace policy, it can only tighten: the workspace and system limits still apply."""

    __tablename__ = "workspace_member_quotas"
    __table_args__ = (
        CheckConstraint(
            "(monthly_token_limit IS NULL OR monthly_token_limit >= 0) "
            "AND (daily_question_limit IS NULL OR daily_question_limit >= 0)",
            name="limits_valid",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    monthly_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    daily_question_limit: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utc_now
    )


class AlertKind(StrEnum):
    WORKSPACE_TOKENS = "workspace_tokens"
    WORKSPACE_COST = "workspace_cost"
    USER_DAILY_TOKENS = "user_daily_tokens"


class UsageAlert(Base):
    """A usage threshold crossed, recorded once per subject, kind, threshold and period."""

    __tablename__ = "usage_alerts"
    __table_args__ = (
        CheckConstraint(_in("kind", AlertKind), name="kind_valid"),
        CheckConstraint("threshold_percent BETWEEN 1 AND 100", name="threshold_valid"),
        UniqueConstraint(
            "subject_id", "kind", "threshold_percent", "period_start", name="uq_usage_alerts_once"
        ),
        Index("ix_usage_alerts_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(String(32))
    # The workspace for workspace alerts, or the user for their daily alert.
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    threshold_percent: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    limit_value: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utc_now
    )


class AuditEvent(Base):
    """Who changed which usage policy, from what to what. Append-only."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # SET NULL: the record of a change outlives the workspace it was made in.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64))
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utc_now
    )


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_JOB = "status IN ('queued', 'running')"


class IngestionJob(Base):
    """A document waiting for, or undergoing, ingestion. Durable: it survives restarts, and
    any API process may claim it. At most one active job per document."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JobStatus), name="status_valid"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
        CheckConstraint(
            "(status = 'running') = (leased_until IS NOT NULL)", name="lease_matches_status"
        ),
        Index(
            "uq_ingestion_jobs_active_document",
            "document_id",
            unique=True,
            postgresql_where=text(ACTIVE_JOB),
        ),
        Index("ix_ingestion_jobs_claimable", "status", "available_at"),
        Index("ix_ingestion_jobs_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utc_now
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
