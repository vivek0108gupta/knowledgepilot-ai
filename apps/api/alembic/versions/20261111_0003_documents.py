"""Add documents and their extracted segments.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="uploaded", nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_documents_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "sha256", name="uq_documents_workspace_id_sha256"),
        sa.UniqueConstraint("storage_path", name="uq_documents_storage_path"),
        # op.f() marks a name as final, so the naming convention does not prefix it again.
        sa.CheckConstraint(
            "status IN ('uploaded', 'processing', 'ready', 'failed')",
            name=op.f("ck_documents_status_valid"),
        ),
        sa.CheckConstraint(
            f"size_bytes > 0 AND size_bytes <= {MAX_DOCUMENT_BYTES}",
            name=op.f("ck_documents_size_bytes_range"),
        ),
        sa.CheckConstraint(
            "storage_path LIKE workspace_id::text || '/' || id::text || '/%'",
            name=op.f("ck_documents_storage_path_owned"),
        ),
    )
    op.create_index("ix_documents_uploaded_by", "documents", ["uploaded_by"])
    op.create_index(
        "ix_documents_workspace_id_created_at", "documents", ["workspace_id", "created_at"]
    )
    op.create_index("ix_documents_workspace_id_status", "documents", ["workspace_id", "status"])

    op.create_table(
        "document_segments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_segments"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_segments_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "document_id", "position", name="uq_document_segments_document_id_position"
        ),
        sa.CheckConstraint(
            "kind IN ('page', 'paragraph', 'lines')",
            name=op.f("ck_document_segments_kind_valid"),
        ),
    )


def downgrade() -> None:
    op.drop_table("document_segments")
    op.drop_table("documents")
