"""Which earlier turns of a conversation the model sees.

Only the most recent messages, within a count and a token budget, in their original order.
They help the model understand follow-up questions ("the second point"); they are never
offered to it as evidence. Only message text is sent: no metadata, no citations, nothing
internal.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Message, MessageRole
from app.services.chunking import Tokenizer, get_tokenizer
from app.services.llm import Turn


async def recent_turns(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    before_sequence: int,
    *,
    max_messages: int,
    token_budget: int,
    tokenizer: Tokenizer | None = None,
) -> list[Turn]:
    """Up to `max_messages` messages before `before_sequence`, newest kept first when the
    budget runs out, returned oldest first and always starting with a user turn."""
    if max_messages < 1 or token_budget < 1:
        return []
    tokenizer = tokenizer or get_tokenizer()
    rows = (
        await session.execute(
            select(Message.role, Message.content)
            .where(
                Message.conversation_id == conversation_id,
                Message.sequence_number < before_sequence,
                Message.role.in_([MessageRole.USER.value, MessageRole.ASSISTANT.value]),
            )
            .order_by(Message.sequence_number.desc())
            .limit(max_messages)
        )
    ).all()

    turns: list[Turn] = []
    used = 0
    for role, content in rows:
        cost = len(tokenizer.encode(content))
        if used + cost > token_budget:
            break
        turns.append(Turn(role=role, content=content))
        used += cost
    turns.reverse()

    # An answer without the question it answered would only confuse the model.
    while turns and turns[0].role != MessageRole.USER.value:
        turns.pop(0)
    return turns
