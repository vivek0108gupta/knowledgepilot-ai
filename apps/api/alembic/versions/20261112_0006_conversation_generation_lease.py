"""Add a generation lease to conversations.

Two nullable columns; existing rows are unaffected.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("generating_message_id", sa.Uuid(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("generating_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "generating_since")
    op.drop_column("conversations", "generating_message_id")
