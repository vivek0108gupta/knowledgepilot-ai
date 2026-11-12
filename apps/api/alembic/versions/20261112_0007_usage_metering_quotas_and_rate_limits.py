"""Add usage metering, workspace quota policies and rate-limit buckets.

Three new tables; no existing table or row is changed. Usage keeps its history when a
workspace, conversation, message or document is deleted (SET NULL); a workspace's quota
policy goes with it (CASCADE).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("policy", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0", name=op.f("ck_rate_limit_buckets_count_non_negative")),
        sa.PrimaryKeyConstraint(
            "policy", "subject_id", "window_start", name=op.f("pk_rate_limit_buckets")
        ),
    )
    op.create_table(
        "workspace_quota_policies",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("monthly_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("monthly_cost_limit", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("max_concurrent_operations", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(monthly_token_limit IS NULL OR monthly_token_limit >= 0) AND "
            "(monthly_cost_limit IS NULL OR monthly_cost_limit >= 0) AND "
            "(max_concurrent_operations IS NULL OR max_concurrent_operations >= 1)",
            name=op.f("ck_workspace_quota_policies_limits_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_quota_policies_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", name=op.f("pk_workspace_quota_policies")),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("usage_source", sa.String(length=16), nullable=True),
        sa.Column("reserved_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_output_tokens", sa.Integer(), nullable=True),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost", sa.Numeric(precision=16, scale=8), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(precision=16, scale=8), nullable=True),
        sa.Column("charged_cost", sa.Numeric(precision=16, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pricing_version", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'reserved') = (completed_at IS NULL)",
            name=op.f("ck_usage_events_completed_at_matches_status"),
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_usage_events_currency_valid")),
        sa.CheckConstraint(
            "operation IN ('chat', 'chat_stream', 'query_embedding', 'document_embedding')",
            name=op.f("ck_usage_events_operation_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'completed', 'failed', 'cancelled', 'expired')",
            name=op.f("ck_usage_events_status_valid"),
        ),
        sa.CheckConstraint(
            "usage_source IS NULL OR "
            "usage_source IN ('provider', 'estimated', 'unavailable', 'none')",
            name=op.f("ck_usage_events_source_valid"),
        ),
        sa.CheckConstraint(
            "(reserved_cost IS NULL OR reserved_cost >= 0) AND "
            "(estimated_cost IS NULL OR estimated_cost >= 0) AND "
            "charged_cost >= 0",
            name=op.f("ck_usage_events_cost_non_negative"),
        ),
        sa.CheckConstraint(
            "reserved_input_tokens >= 0 AND "
            "reserved_output_tokens >= 0 AND "
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0) AND "
            "(estimated_input_tokens IS NULL OR estimated_input_tokens >= 0) AND "
            "(estimated_output_tokens IS NULL OR estimated_output_tokens >= 0) AND "
            "charged_tokens >= 0",
            name=op.f("ck_usage_events_tokens_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_usage_events_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_usage_events_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_usage_events_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_usage_events_workspace_id_workspaces"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_usage_events_idempotency_key")),
        sa.UniqueConstraint("message_id", name=op.f("uq_usage_events_message_id")),
    )
    op.create_index(
        "ix_usage_events_open",
        "usage_events",
        ["status", "expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'reserved'"),
    )
    op.create_index(
        "ix_usage_events_user_created", "usage_events", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_usage_events_workspace_created",
        "usage_events",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_usage_events_workspace_model", "usage_events", ["workspace_id", "model"], unique=False
    )
    op.create_index(
        "ix_usage_events_workspace_operation",
        "usage_events",
        ["workspace_id", "operation"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_workspace_operation", table_name="usage_events")
    op.drop_index("ix_usage_events_workspace_model", table_name="usage_events")
    op.drop_index("ix_usage_events_workspace_created", table_name="usage_events")
    op.drop_index("ix_usage_events_user_created", table_name="usage_events")
    op.drop_index(
        "ix_usage_events_open",
        table_name="usage_events",
        postgresql_where=sa.text("status = 'reserved'"),
    )
    op.drop_table("usage_events")
    op.drop_table("workspace_quota_policies")
    op.drop_table("rate_limit_buckets")
