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
    Uuid,
    func,
    text,
)
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


class WorkspaceQuotaPolicy(Base):
    """A workspace's own limits. Each may only be stricter than the system's (NULL: the
    system limit applies)."""

    __tablename__ = "workspace_quota_policies"
    __table_args__ = (
        CheckConstraint(
            "(monthly_token_limit IS NULL OR monthly_token_limit >= 0) "
            "AND (monthly_cost_limit IS NULL OR monthly_cost_limit >= 0) "
            "AND (max_concurrent_operations IS NULL OR max_concurrent_operations >= 1)",
            name="limits_valid",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    monthly_token_limit: Mapped[int | None] = mapped_column(BigInteger)
    monthly_cost_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    max_concurrent_operations: Mapped[int | None] = mapped_column(Integer)
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
