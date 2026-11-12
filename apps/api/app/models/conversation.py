import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.workspace import utc_now

DEFAULT_CONVERSATION_TITLE = "New conversation"
MAX_TITLE_LENGTH = 120


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageRole(StrEnum):
    """Who wrote a message. There is deliberately no `system` role: prompts given to the
    model are never stored as conversation messages, so they can never be read back."""

    USER = "user"
    ASSISTANT = "assistant"


class Conversation(Base):
    """A chat thread. Private to the user who created it, inside one workspace."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {MAX_TITLE_LENGTH}", name="title_length"
        ),
        Index("ix_conversations_workspace_id", "workspace_id"),
        Index("ix_conversations_created_by", "created_by"),
        Index("ix_conversations_updated_at", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    # Set once at creation from the URL the creator was authorised for. Nothing updates it,
    # so a conversation cannot be moved to another workspace.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    # A Supabase Auth user ID from a verified token. Not a foreign key, for the same reason
    # as `workspaces.owner_id`.
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(
        String(MAX_TITLE_LENGTH), server_default=DEFAULT_CONVERSATION_TITLE
    )
    status: Mapped[str] = mapped_column(String(16), server_default=ConversationStatus.ACTIVE.value)
    # Stored as timestamptz: an absolute instant, written and read in UTC.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utc_now
    )
    # Set while an answer is being written: the user message being answered, and since when.
    # Only one answer is written at a time per conversation. It is a lease, not a lock: it
    # expires (CHAT_GENERATION_LEASE_SECONDS), so a crash mid-answer cannot block the
    # conversation for ever, and no database lock is held while the model is working.
    generating_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    generating_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_valid"),
        CheckConstraint("sequence_number >= 1", name="sequence_number_positive"),
        CheckConstraint("char_length(content) >= 1", name="content_not_empty"),
        # Message order is this number, never a timestamp: two messages can share a
        # timestamp, but never a position. Also serves every lookup by conversation_id.
        UniqueConstraint(
            "conversation_id", "sequence_number", name="uq_messages_conversation_id_sequence_number"
        ),
        # At most one message per client-generated ID in a conversation, so a request sent
        # twice (a retry, a double click) creates one message. Rows without an ID are exempt.
        Index(
            "uq_messages_conversation_id_client_message_id",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # Assigned by the server while holding a lock on the conversation. Starts at 1.
    sequence_number: Mapped[int] = mapped_column(Integer)
    client_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # For assistant messages later: citations and the like. Never prompts or credentials.
    # The attribute is `message_metadata` because SQLAlchemy reserves the name `metadata`.
    message_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    # Filled in by the answer-generation service for assistant messages.
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
