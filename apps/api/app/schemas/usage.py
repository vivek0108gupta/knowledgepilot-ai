import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.usage import UsageOperation, UsageSource, UsageStatus

# Costs are decimals serialised as strings ("0.00123456"), so no precision is lost.


class UsageTotals(BaseModel):
    tokens: int
    estimated_cost: Decimal
    operations: int


class OperationUsage(UsageTotals):
    operation: UsageOperation


class ModelUsage(UsageTotals):
    model: str


class DailyUsage(BaseModel):
    date: date
    chat_tokens: int
    embedding_tokens: int
    estimated_cost: Decimal


class WorkspaceAllowance(BaseModel):
    monthly_token_limit: int
    monthly_tokens_used: int
    monthly_tokens_remaining: int
    monthly_cost_limit: Decimal
    monthly_cost_used: Decimal
    monthly_cost_remaining: Decimal
    max_concurrent_operations: int
    operations_running: int


class UserAllowance(BaseModel):
    daily_token_limit: int
    daily_tokens_used: int
    daily_tokens_remaining: int
    daily_question_limit: int
    daily_questions_used: int
    max_concurrent_generations: int
    generations_running: int
    resets_at: datetime


class UsageSummary(BaseModel):
    workspace_id: uuid.UUID
    # "workspace": everyone's usage (owners and admins); "own": only the caller's (members).
    scope: Literal["workspace", "own"]
    period_start: datetime
    period_end: datetime
    currency: str
    pricing_version: str
    totals: UsageTotals
    by_operation: list[OperationUsage]
    by_model: list[ModelUsage]
    daily: list[DailyUsage]
    # Records whose tokens are this application's estimate, or unknown and charged at the
    # reserved maximum, rather than reported by the provider.
    estimated_records: int
    unavailable_records: int
    # How the charged tokens break down: reported by the provider, estimated here, or held
    # as an upper bound because the outcome is unknown. They add up to `totals.tokens`.
    accounting: "ChargeBreakdown"
    # Records by what happened at the provider (executed_with_usage, unknown, ...).
    outcomes: dict[str, int]
    allowance: WorkspaceAllowance
    my_allowance: UserAllowance


class ChargeBreakdown(BaseModel):
    provider_reported_tokens: int
    estimated_tokens: int
    held_tokens: int
    not_charged_records: int


class UsageEventOut(BaseModel):
    """One provider operation. No prompt, answer or document text is ever included."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    operation: UsageOperation
    provider: str
    model: str
    status: UsageStatus
    usage_source: UsageSource | None
    outcome: str | None = None
    input_tokens: int | None
    output_tokens: int | None
    estimated_input_tokens: int | None
    estimated_output_tokens: int | None
    charged_tokens: int
    estimated_cost: Decimal | None
    charged_cost: Decimal
    currency: str
    pricing_version: str
    conversation_id: uuid.UUID | None
    document_id: uuid.UUID | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    # Present for owners and admins, who see everyone's usage; members see only their own.
    user_id: uuid.UUID | None = None


class UsageHistory(BaseModel):
    items: list[UsageEventOut]
    total: int
    limit: int
    offset: int


class QuotaLimits(BaseModel):
    monthly_token_limit: int
    monthly_cost_limit: Decimal
    max_concurrent_operations: int


class QuotaPolicyOut(BaseModel):
    workspace_id: uuid.UUID
    currency: str
    # The limits in force: the system's, tightened by the workspace's own where set.
    effective: QuotaLimits
    # The system ceilings, which a workspace policy may only lower.
    system: QuotaLimits
    # The workspace's own settings (null: none set).
    workspace_monthly_token_limit: int | None
    workspace_monthly_cost_limit: Decimal | None
    workspace_max_concurrent_operations: int | None
    alert_threshold_percent: int
    can_edit: bool
    allowance: WorkspaceAllowance


class QuotaPolicyUpdate(BaseModel):
    """Set a field to a number to tighten that limit, or to null to use the system limit.
    Fields left out are unchanged."""

    model_config = ConfigDict(extra="forbid")

    monthly_token_limit: int | None = Field(default=None, ge=0, le=10**15)
    monthly_cost_limit: Decimal | None = Field(
        default=None, ge=0, le=Decimal("1000000000"), max_digits=14, decimal_places=4
    )
    max_concurrent_operations: int | None = Field(default=None, ge=1, le=10_000)
    alert_threshold_percent: int | None = Field(default=None, ge=1, le=100)


class MyUsage(BaseModel):
    allowance: UserAllowance
    currency: str
    today: UsageTotals
    # Your daily-usage warnings today.
    alerts: list["AlertOut"] = []


class MemberQuotaUpdate(BaseModel):
    """A member's own limits in this workspace. Null: no limit of their own."""

    model_config = ConfigDict(extra="forbid")

    monthly_token_limit: int | None = Field(default=None, ge=0, le=10**15)
    daily_question_limit: int | None = Field(default=None, ge=0, le=1_000_000)


class MemberQuotaOut(BaseModel):
    user_id: uuid.UUID
    monthly_token_limit: int | None
    daily_question_limit: int | None
    monthly_tokens_used: int
    updated_at: datetime | None


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    threshold_percent: int
    period_start: datetime
    used: Decimal
    limit_value: Decimal
    created_at: datetime


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID
    action: str
    target_user_id: uuid.UUID | None
    details: dict
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEventOut]
    total: int
