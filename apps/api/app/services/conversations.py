"""Conversations and their messages.

Conversations are private: only the user who created one can see or change it, and only
while they are still a member of its workspace. Both conditions are part of the single
query that loads a conversation (`get_owned`), so no function here can reach a conversation
without them. A conversation that exists but belongs to someone else gets the same 404 as
one that does not exist.

Nothing in this module calls a language model. It stores what users write and returns it.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.conversation import (
    DEFAULT_CONVERSATION_TITLE,
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
)
from app.models.workspace import WorkspaceMember
from app.services import workspaces as workspace_service

logger = logging.getLogger(__name__)

AUTO_TITLE_LENGTH = 60


@dataclass(frozen=True, slots=True)
class ConversationView:
    conversation: Conversation
    message_count: int


@dataclass(frozen=True, slots=True)
class CreatedMessage:
    message: Message
    # False when the same client_message_id had already been stored: nothing new was created.
    created: bool


def not_found() -> AppError:
    return AppError(404, "conversation_not_found", "Conversation not found.")


def busy() -> AppError:
    return AppError(
        409,
        "conversation_busy",
        "An answer is still being written in this conversation. Wait for it to finish.",
    )


def archived() -> AppError:
    return AppError(409, "conversation_archived", "Restore this conversation before adding to it.")


def generation_active(conversation: Conversation, now: datetime | None = None) -> bool:
    """True while an answer is being written in this conversation and its lease is current."""
    if conversation.generating_message_id is None or conversation.generating_since is None:
        return False
    lease = timedelta(seconds=get_settings().chat_generation_lease_seconds)
    return conversation.generating_since > (now or datetime.now(UTC)) - lease


async def next_sequence(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    """The next message number. Only safe while holding the conversation's row lock."""
    last = await session.scalar(
        select(func.max(Message.sequence_number)).where(Message.conversation_id == conversation_id)
    )
    return (last or 0) + 1


def name_from_first_message(conversation: Conversation, sequence_number: int, content: str) -> None:
    if sequence_number == 1 and conversation.title == DEFAULT_CONVERSATION_TITLE:
        # Only the untouched default is replaced; a title the user chose is left alone.
        conversation.title = title_from_message(content)


async def find_by_client_id(
    session: AsyncSession, conversation_id: uuid.UUID, client_message_id: uuid.UUID
) -> Message | None:
    return await session.scalar(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.client_message_id == client_message_id,
        )
    )


def title_from_message(content: str, limit: int = AUTO_TITLE_LENGTH) -> str:
    """A short title made from a first message, without calling a model.

    Collapses whitespace and, if the text is too long, ends at a word boundary with an
    ellipsis. A single word longer than the limit is cut, since there is nowhere else to.
    """
    text = re.sub(r"\s+", " ", content).strip()
    # Control and other invisible characters would be rejected by the title's own rules.
    text = "".join(character for character in text if character.isprintable())
    if not text:
        return DEFAULT_CONVERSATION_TITLE
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # Back up to the last whole word, unless the cut already falls exactly between two words.
    if text[limit - 1] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:.-") + "…"


def message_count() -> object:
    """A correlated count, so listing conversations with their counts is one query."""
    return (
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )


def owned(user_id: uuid.UUID) -> list[object]:
    """The conditions that make a conversation the caller's: they created it, and they are
    still a member of the workspace it lives in."""
    return [
        Conversation.created_by == user_id,
        WorkspaceMember.workspace_id == Conversation.workspace_id,
        WorkspaceMember.user_id == user_id,
    ]


async def get_owned(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> Conversation:
    """Load a conversation only if it is the caller's.

    With `lock=True` the conversation row is locked until the transaction ends. Everything
    that adds a message takes this lock first, which is what makes sequence numbers safe.
    """
    query = (
        select(Conversation)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Conversation.workspace_id)
        .where(Conversation.id == conversation_id, *owned(user_id))
    )
    if lock:
        query = query.with_for_update(of=Conversation)
    conversation = await session.scalar(query)
    if conversation is None:
        raise not_found()
    return conversation


async def create(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, title: str
) -> ConversationView:
    # 404 unless the caller is a member of this workspace.
    await workspace_service.get_access(session, workspace_id, user_id)
    conversation = Conversation(
        id=uuid.uuid4(), workspace_id=workspace_id, created_by=user_id, title=title
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    logger.info("Created conversation %s in workspace %s", conversation.id, workspace_id)
    return ConversationView(conversation, 0)


async def list_for_user(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    status: ConversationStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[ConversationView], int]:
    await workspace_service.get_access(session, workspace_id, user_id)

    conditions = [Conversation.workspace_id == workspace_id, Conversation.created_by == user_id]
    if status is not None:
        conditions.append(Conversation.status == status.value)

    total = await session.scalar(select(func.count()).select_from(Conversation).where(*conditions))
    rows = await session.execute(
        select(Conversation, message_count())
        .where(*conditions)
        # The ID breaks ties, so pages never repeat or skip a conversation.
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [ConversationView(row[0], row[1]) for row in rows], total or 0


async def get_view(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> ConversationView:
    row = (
        await session.execute(
            select(Conversation, message_count())
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Conversation.workspace_id)
            .where(Conversation.id == conversation_id, *owned(user_id))
        )
    ).one_or_none()
    if row is None:
        raise not_found()
    return ConversationView(row[0], row[1])


async def count_messages(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
        or 0
    )


async def rename(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID, title: str
) -> ConversationView:
    conversation = await get_owned(session, conversation_id, user_id, lock=True)
    conversation.title = title
    # Set explicitly, so renaming to the same title still counts as activity.
    conversation.updated_at = datetime.now(UTC)
    count = await count_messages(session, conversation.id)
    await session.commit()
    await session.refresh(conversation)
    return ConversationView(conversation, count)


async def set_status(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    status: ConversationStatus,
) -> ConversationView:
    conversation = await get_owned(session, conversation_id, user_id, lock=True)
    if conversation.status != status.value:
        conversation.status = status.value
        conversation.updated_at = datetime.now(UTC)
    count = await count_messages(session, conversation.id)
    await session.commit()
    await session.refresh(conversation)
    return ConversationView(conversation, count)


async def remove(session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    conversation = await get_owned(session, conversation_id, user_id, lock=True)
    # Messages go with it through the foreign key's ON DELETE CASCADE.
    await session.execute(delete(Conversation).where(Conversation.id == conversation.id))
    await session.commit()
    logger.info("Deleted conversation %s", conversation_id)


async def list_messages(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    limit: int,
    before: int | None,
) -> tuple[list[Message], int, bool]:
    """The newest `limit` messages (older than `before`, if given), in ascending order.

    Returns the messages, the conversation's total, and whether older ones remain.
    """
    conversation = await get_owned(session, conversation_id, user_id)
    conditions = [Message.conversation_id == conversation.id]
    if before is not None:
        conditions.append(Message.sequence_number < before)

    # One extra row tells us whether anything older exists, without a second query.
    newest_first = list(
        await session.scalars(
            select(Message)
            .where(*conditions)
            .order_by(Message.sequence_number.desc())
            .limit(limit + 1)
        )
    )
    has_more = len(newest_first) > limit
    total = await count_messages(session, conversation.id)
    return list(reversed(newest_first[:limit])), total, has_more


async def add_user_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
    client_message_id: uuid.UUID | None,
) -> CreatedMessage:
    """Store a message written by the signed-in user.

    The role is fixed here and cannot be supplied by a caller. Assistant messages will be
    written by the answer-generation service, never through this function.
    """
    # The row lock serialises every writer to this conversation: the second of two
    # simultaneous requests waits here, then sees the first one's message. That makes the
    # "next number" below safe, and makes the duplicate check reliable.
    conversation = await get_owned(session, conversation_id, user_id, lock=True)

    if client_message_id is not None:
        existing = await find_by_client_id(session, conversation.id, client_message_id)
        if existing is not None:
            same_message = existing.content == content and existing.role == MessageRole.USER.value
            # Detach the stored message before releasing the lock: ending the transaction
            # would otherwise expire it, and it is still needed for the response.
            session.expunge(existing)
            await session.rollback()
            if not same_message:
                raise AppError(
                    409,
                    "idempotency_conflict",
                    "This message ID was already used for a different message.",
                )
            return CreatedMessage(existing, created=False)

    if conversation.status != ConversationStatus.ACTIVE.value:
        raise archived()
    # A message added mid-answer would land between the question and its answer.
    if generation_active(conversation):
        raise busy()

    sequence_number = await next_sequence(session, conversation.id)

    message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        role=MessageRole.USER.value,
        content=content,
        sequence_number=sequence_number,
        client_message_id=client_message_id,
    )
    session.add(message)

    name_from_first_message(conversation, sequence_number, content)
    conversation.updated_at = datetime.now(UTC)

    try:
        await session.commit()
    except IntegrityError as error:
        # The unique constraints are the last line of defence if the lock were ever bypassed.
        await session.rollback()
        raise AppError(
            409, "message_conflict", "The message could not be saved. Try again."
        ) from error
    await session.refresh(message)
    # IDs and sizes only: message text is never logged.
    logger.info(
        "Stored message %d in conversation %s (%d characters)",
        sequence_number,
        conversation.id,
        len(content),
    )
    return CreatedMessage(message, created=True)
