"""Usage reliability: outcomes and recovery fields on usage, durable ingestion jobs, member
quotas, usage alerts and an audit log.

New tables and new nullable columns only. Existing usage rows are classified into the new
`outcome` column from what they already record; no existing value changes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUOTA_LIMITS_0007 = (
    "(monthly_token_limit IS NULL OR monthly_token_limit >= 0) "
    "AND (monthly_cost_limit IS NULL OR monthly_cost_limit >= 0) "
    "AND (max_concurrent_operations IS NULL OR max_concurrent_operations >= 1)"
)
QUOTA_LIMITS_0008 = (
    f"{QUOTA_LIMITS_0007} AND (alert_threshold_percent IS NULL "
    "OR alert_threshold_percent BETWEEN 1 AND 100)"
)


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_audit_events_workspace_id_workspaces"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_workspace_created",
        "audit_events",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "usage_alerts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("threshold_percent", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("limit_value", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('workspace_tokens', 'workspace_cost', 'user_daily_tokens')",
            name=op.f("ck_usage_alerts_kind_valid"),
        ),
        sa.CheckConstraint(
            "threshold_percent BETWEEN 1 AND 100", name=op.f("ck_usage_alerts_threshold_valid")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_usage_alerts_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_alerts")),
        sa.UniqueConstraint(
            "subject_id", "kind", "threshold_percent", "period_start", name="uq_usage_alerts_once"
        ),
    )
    op.create_index(op.f("ix_usage_alerts_user_id"), "usage_alerts", ["user_id"], unique=False)
    op.create_index(
        "ix_usage_alerts_workspace_created",
        "usage_alerts",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "workspace_member_quotas",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("monthly_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("daily_question_limit", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(monthly_token_limit IS NULL OR monthly_token_limit >= 0) AND "
            "(daily_question_limit IS NULL OR daily_question_limit >= 0)",
            name=op.f("ck_workspace_member_quotas_limits_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_member_quotas_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_workspace_member_quotas")),
    )
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'running') = (leased_until IS NOT NULL)",
            name=op.f("ck_ingestion_jobs_lease_matches_status"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_ingestion_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1", name=op.f("ck_ingestion_jobs_attempts_valid")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_ingestion_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_ingestion_jobs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_jobs")),
    )
    op.create_index(
        "ix_ingestion_jobs_claimable", "ingestion_jobs", ["status", "available_at"], unique=False
    )
    op.create_index(
        "ix_ingestion_jobs_workspace_status",
        "ingestion_jobs",
        ["workspace_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_ingestion_jobs_active_document",
        "ingestion_jobs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.add_column("usage_events", sa.Column("outcome", sa.String(length=32), nullable=True))
    op.add_column(
        "usage_events", sa.Column("provider_called_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("usage_events", sa.Column("prompt_tokens_sent", sa.Integer(), nullable=True))
    op.add_column(
        "usage_events", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "usage_events", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "usage_events", sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "usage_events", sa.Column("late_reconciled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workspace_quota_policies",
        sa.Column("alert_threshold_percent", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_usage_events_outcome_valid"),
        "usage_events",
        "outcome IS NULL OR outcome IN ('not_called', 'rejected', 'executed_with_usage', "
        "'executed_without_usage', 'unknown', 'partial_stream', 'cancelled_by_user', "
        "'client_disconnected', 'abandoned')",
    )
    op.drop_constraint(
        op.f("ck_workspace_quota_policies_limits_valid"), "workspace_quota_policies", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_workspace_quota_policies_limits_valid"),
        "workspace_quota_policies",
        QUOTA_LIMITS_0008,
    )
    # Classify existing rows from what they already record.
    op.execute(
        """
        UPDATE usage_events SET outcome = CASE
            WHEN status = 'expired' THEN 'abandoned'
            WHEN usage_source = 'provider' THEN 'executed_with_usage'
            WHEN usage_source = 'none' THEN 'not_called'
            WHEN status = 'cancelled' THEN 'client_disconnected'
            WHEN usage_source = 'estimated' AND status = 'failed' THEN 'partial_stream'
            WHEN usage_source = 'estimated' THEN 'executed_without_usage'
            WHEN usage_source = 'unavailable' THEN 'unknown'
        END
        WHERE status <> 'reserved'
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_workspace_quota_policies_limits_valid"), "workspace_quota_policies", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_workspace_quota_policies_limits_valid"),
        "workspace_quota_policies",
        QUOTA_LIMITS_0007,
    )
    op.drop_constraint(op.f("ck_usage_events_outcome_valid"), "usage_events", type_="check")
    op.drop_column("workspace_quota_policies", "alert_threshold_percent")
    op.drop_column("usage_events", "late_reconciled_at")
    op.drop_column("usage_events", "expired_at")
    op.drop_column("usage_events", "cancel_requested_at")
    op.drop_column("usage_events", "heartbeat_at")
    op.drop_column("usage_events", "prompt_tokens_sent")
    op.drop_column("usage_events", "provider_called_at")
    op.drop_column("usage_events", "outcome")
    op.drop_index(
        "uq_ingestion_jobs_active_document",
        table_name="ingestion_jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_ingestion_jobs_workspace_status", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_claimable", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_table("workspace_member_quotas")
    op.drop_index("ix_usage_alerts_workspace_created", table_name="usage_alerts")
    op.drop_index(op.f("ix_usage_alerts_user_id"), table_name="usage_alerts")
    op.drop_table("usage_alerts")
    op.drop_index("ix_audit_events_workspace_created", table_name="audit_events")
    op.drop_table("audit_events")
