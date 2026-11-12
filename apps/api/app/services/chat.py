"""Answering a question in a conversation. Sequencing only: each step lives elsewhere.

    conversations.py     who may use the conversation; storing messages
    chat_history.py      which earlier turns the model sees
    search.nearest       finding evidence, in this conversation's workspace only
    context_builder.py   what evidence the model sees, within a token budget
    prompts.py           the instructions
    llm.py               calling the model
    citations.py         checking the answer against the evidence

The workspace always comes from the conversation row the caller was authorised for. Nothing
in a request can point retrieval at another workspace.

One answer at a time per conversation, enforced with a lease on the conversation row
(`generating_message_id`, `generating_since`). It is taken and released in short
transactions, so no database lock or connection is held while waiting on OpenAI, and it
expires, so a crash cannot block a conversation for ever.

If answering fails, the question stays stored and no answer is invented. Sending the same
`client_message_id` again retries the answer for that stored question; once an answer exists,
it is returned instead of a second one.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.conversation import Conversation, ConversationStatus, Message, MessageRole
from app.services import chat_history
from app.services import conversations as conversation_service
from app.services import search as search_service
from app.services.citations import FinalAnswer, InvalidAnswer, finalize
from app.services.context_builder import Context, build_context
from app.services.embeddings import Embedder, EmbedderProvider, EmbeddingError
from app.services.llm import ChatModel, ChatModelProvider, Generation, LLMError, Turn
from app.services.prompts import (
    ANSWER_SCHEMA,
    ANSWER_SCHEMA_NAME,
    NO_ANSWER,
    PROMPT_VERSION,
    system_prompt,
)
from app.services.search import Match

logger = logging.getLogger(__name__)

# How much of the previous exchange is added to a follow-up question when searching.
FOLLOW_UP_CONTEXT_CHARACTERS = 1500

# Errors after which the user's question has been stored, so the client can offer a retry.
GENERATION_ERROR_CODES = frozenset(
    {
        "ai_unavailable",
        "ai_misconfigured",
        "ai_rejected",
        "ai_invalid_response",
        "ai_answer_too_long",
        "embeddings_unavailable",
        "answer_not_saved",
    }
)


@dataclass(frozen=True, slots=True)
class ChatResult:
    user_message: Message
    assistant_message: Message
    # True when this question had already been answered and the stored answer is returned.
    replayed: bool


@dataclass(frozen=True, slots=True)
class PendingAnswer:
    workspace_id: uuid.UUID
    user_message: Message
    reply: Message | None


@dataclass(frozen=True, slots=True)
class Answer:
    final: FinalAnswer
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    retrieval: dict[str, Any]


def ai_error(error: LLMError) -> AppError:
    if error.code == "unavailable":
        return AppError(
            503,
            "ai_unavailable",
            "The AI service is busy or unreachable. Your question was saved; try again shortly.",
        )
    if error.code == "misconfigured":
        return AppError(
            503,
            "ai_misconfigured",
            "Answering is not available right now. Your question was saved.",
        )
    if error.code == "too_long":
        return AppError(
            502,
            "ai_answer_too_long",
            "The answer was too long to finish. Your question was saved; try asking for less "
            "at once.",
        )
    if error.code == "invalid_response":
        return AppError(
            502,
            "ai_invalid_response",
            "The AI service returned an unusable answer. Your question was saved; try again.",
        )
    return AppError(
        502,
        "ai_rejected",
        "The AI service could not answer this question. Your question was saved; try "
        "rephrasing it.",
    )


def retrieval_queries(question: str, history: list[Turn]) -> list[str]:
    """What to search for. The question itself, and, for a follow-up, the question together
    with the end of the previous exchange, so "explain the second point" finds the passages
    that point came from. Both are searched; the best matches of either are used."""
    queries = [question]
    if history:
        recent = "\n".join(turn.content for turn in history[-2:])[-FOLLOW_UP_CONTEXT_CHARACTERS:]
        queries.append(f"{recent}\n{question}")
    return queries


async def find_reply(
    session: AsyncSession, conversation_id: uuid.UUID, user_message_id: uuid.UUID
) -> Message | None:
    return await session.scalar(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT.value,
            Message.message_metadata["in_reply_to"].astext == str(user_message_id),
        )
    )


async def begin(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    client_message_id: uuid.UUID | None,
) -> PendingAnswer:
    """Store the question (or find it, for a retry) and take the conversation's lease."""
    conversation = await conversation_service.get_owned(
        session, conversation_id, user_id, lock=True
    )
    workspace_id = conversation.workspace_id
    user_message: Message | None = None

    if client_message_id is not None:
        existing = await conversation_service.find_by_client_id(
            session, conversation.id, client_message_id
        )
        if existing is not None:
            if existing.content != question or existing.role != MessageRole.USER.value:
                await session.rollback()
                raise AppError(
                    409,
                    "idempotency_conflict",
                    "This message ID was already used for a different message.",
                )
            reply = await find_reply(session, conversation.id, existing.id)
            if reply is not None:
                session.expunge(existing)
                session.expunge(reply)
                await session.rollback()
                return PendingAnswer(workspace_id, existing, reply)
            user_message = existing  # stored, but its answer failed earlier: try again

    if conversation.status != ConversationStatus.ACTIVE.value:
        await session.rollback()
        raise conversation_service.archived()
    if conversation_service.generation_active(conversation):
        answering_this = (
            user_message is not None and conversation.generating_message_id == user_message.id
        )
        await session.rollback()
        if answering_this:
            raise AppError(
                409, "answer_in_progress", "The answer to this question is still being written."
            )
        raise conversation_service.busy()

    now = datetime.now(UTC)
    if user_message is None:
        sequence_number = await conversation_service.next_sequence(session, conversation.id)
        user_message = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            role=MessageRole.USER.value,
            content=question,
            sequence_number=sequence_number,
            client_message_id=client_message_id,
        )
        session.add(user_message)
        conversation_service.name_from_first_message(conversation, sequence_number, question)

    conversation.generating_message_id = user_message.id
    conversation.generating_since = now
    conversation.updated_at = now
    try:
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise AppError(
            409, "message_conflict", "The message could not be saved. Try again."
        ) from error
    await session.refresh(user_message)
    # Detach before ending the transaction: a rollback would otherwise expire it.
    session.expunge(user_message)
    await session.rollback()
    logger.info(
        "Answering message %d in conversation %s (%d characters)",
        user_message.sequence_number,
        conversation_id,
        len(question),
    )
    return PendingAnswer(workspace_id, user_message, None)


async def retrieve(
    session: AsyncSession,
    embedder: Embedder,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    queries: list[str],
    top_k: int,
    document_ids: list[uuid.UUID],
    min_similarity: float,
) -> list[Match]:
    """The best `top_k` passages for any of the queries, through the same isolated vector
    query that search uses. All queries are embedded in one request."""
    try:
        vectors = await embedder.embed(queries)
    except EmbeddingError as error:
        logger.error("Question embedding failed (retryable: %s)", error.retryable)
        raise search_service.embeddings_unavailable() from error

    best: dict[uuid.UUID, Match] = {}
    for vector in vectors:
        matches = await search_service.nearest(
            session, workspace_id, user_id, vector, top_k, document_ids, min_similarity
        )
        for match in matches:
            if match.chunk_id not in best or match.similarity > best[match.chunk_id].similarity:
                best[match.chunk_id] = match
    return sorted(best.values(), key=lambda match: match.similarity, reverse=True)[:top_k]


@dataclass(frozen=True, slots=True)
class Prepared:
    """Everything the model is given for one answer, or no context when nothing relevant was
    found (then no model call is made)."""

    context: Context | None
    instructions: str
    turns: list[Turn]
    retrieval: dict[str, Any]


def no_evidence_answer(retrieval: dict[str, Any]) -> Answer:
    return Answer(
        FinalAnswer(NO_ANSWER, [], grounded=False, reason="no_evidence"),
        model=None,
        input_tokens=None,
        output_tokens=None,
        retrieval={**retrieval, "used": 0},
    )


async def prepare(
    session: AsyncSession,
    embedder: Embedder,
    turn: PendingAnswer,
    user_id: uuid.UUID,
    question: str,
    top_k: int,
    document_ids: list[uuid.UUID],
    settings: Settings,
) -> Prepared:
    """History, retrieval and context: what the model will see."""
    history = await chat_history.recent_turns(
        session,
        turn.user_message.conversation_id,
        turn.user_message.sequence_number,
        max_messages=settings.chat_history_messages,
        token_budget=settings.chat_history_tokens,
    )
    await session.rollback()

    min_similarity = settings.chat_min_similarity
    matches = await retrieve(
        session,
        embedder,
        turn.workspace_id,
        user_id,
        retrieval_queries(question, history),
        top_k,
        document_ids,
        min_similarity,
    )
    retrieval: dict[str, Any] = {
        "top_k": top_k,
        "min_similarity": min_similarity,
        "candidates": len(matches),
        # Scores only, never text: for tuning CHAT_MIN_SIMILARITY.
        "similarities": [match.similarity for match in matches],
        "history_turns": len(history),
    }

    if not matches:
        await session.rollback()
        logger.info("No passage reached the similarity threshold; answering without the model")
        return Prepared(None, "", [], retrieval)

    context = build_context(
        matches,
        token_budget=settings.chat_context_tokens,
        max_per_document=settings.chat_max_chunks_per_document,
    )
    retrieval["used"] = len(context.sources)
    retrieval["context_tokens"] = context.tokens
    # Evidence first, then the question, in one user turn after the earlier conversation.
    latest = Turn(role="user", content=f"{context.text}\n\nQuestion:\n{question}")
    # End the read transaction: no connection is held while the model writes.
    await session.rollback()
    return Prepared(context, system_prompt(context.boundary), [*history, latest], retrieval)


def conclude(conversation_id: uuid.UUID, prepared: Prepared, generation: Generation) -> Answer:
    """Check the model's whole output against the evidence it was given."""
    assert prepared.context is not None
    try:
        final = finalize(generation, prepared.context)
    except InvalidAnswer as error:
        logger.error("Model output rejected: %s", error)
        raise ai_error(LLMError(str(error), code="invalid_response", retryable=False)) from error

    logger.info(
        "Answer for conversation %s: grounded=%s, %d citation(s), %d dropped, %d passage(s) used",
        conversation_id,
        final.grounded,
        len(final.citations),
        final.dropped_citations,
        len(prepared.context.sources),
    )
    return Answer(
        final,
        model=generation.model,
        input_tokens=generation.input_tokens,
        output_tokens=generation.output_tokens,
        retrieval=prepared.retrieval,
    )


async def produce(
    session: AsyncSession,
    embedder: Embedder,
    model: ChatModel,
    turn: PendingAnswer,
    user_id: uuid.UUID,
    question: str,
    top_k: int,
    document_ids: list[uuid.UUID],
    settings: Settings,
) -> Answer:
    prepared = await prepare(
        session, embedder, turn, user_id, question, top_k, document_ids, settings
    )
    if prepared.context is None:
        # Nothing relevant: answer honestly without spending a model call.
        return no_evidence_answer(prepared.retrieval)
    try:
        generation = await model.generate(
            instructions=prepared.instructions,
            turns=prepared.turns,
            schema=ANSWER_SCHEMA,
            schema_name=ANSWER_SCHEMA_NAME,
        )
    except LLMError as error:
        raise ai_error(error) from error
    return conclude(turn.user_message.conversation_id, prepared, generation)


async def save(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    user_message: Message,
    answer: Answer,
    message_id: uuid.UUID | None = None,
) -> Message:
    """Store the answer and release the lease, in one transaction."""
    conversation = await conversation_service.get_owned(
        session, conversation_id, user_id, lock=True
    )
    if conversation.generating_message_id != user_message.id:
        await session.rollback()
        raise AppError(
            409,
            "answer_superseded",
            "This answer took too long and was abandoned. Reload the conversation and try again.",
        )

    final = answer.final
    message = Message(
        id=message_id or uuid.uuid4(),
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT.value,
        content=final.content,
        sequence_number=await conversation_service.next_sequence(session, conversation.id),
        model=answer.model,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
        # Never the prompt or the passages themselves: only what is needed to show and trace
        # the answer.
        message_metadata={
            "in_reply_to": str(user_message.id),
            "grounded": final.grounded,
            "citations": final.citations,
            "prompt_version": PROMPT_VERSION,
            "reason": final.reason,
            "dropped_citations": final.dropped_citations,
            "unverified_quotes": final.unverified_quotes,
            "approximate_quotes": final.approximate_quotes,
            "unmarked_citations": final.unmarked_citations,
            "retrieval": answer.retrieval,
        },
    )
    session.add(message)
    conversation.generating_message_id = None
    conversation.generating_since = None
    conversation.updated_at = datetime.now(UTC)
    try:
        await session.commit()
    except Exception as error:
        await session.rollback()
        logger.exception("Could not store the answer in conversation %s", conversation_id)
        raise AppError(
            500,
            "answer_not_saved",
            "The answer could not be saved. Your question was kept; try again.",
        ) from error
    await session.refresh(message)
    session.expunge(message)
    await session.rollback()
    return message


async def release(
    session: AsyncSession, conversation_id: uuid.UUID, user_message_id: uuid.UUID
) -> None:
    """Give the conversation back after a failed answer. Never raises: the lease also expires."""
    try:
        await session.rollback()
        await session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.generating_message_id == user_message_id,
            )
            # Keep updated_at as it was: a failed answer is not activity.
            .values(
                generating_message_id=None,
                generating_since=None,
                updated_at=Conversation.updated_at,
            )
        )
        await session.commit()
    except Exception:
        logger.exception("Could not release the lease on conversation %s", conversation_id)


async def ask(
    session: AsyncSession,
    embedder_provider: EmbedderProvider,
    chat_model_provider: ChatModelProvider,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    question: str,
    client_message_id: uuid.UUID | None,
    document_ids: list[uuid.UUID],
    top_k: int,
) -> ChatResult:
    settings = get_settings()
    # Fail before storing anything if the server cannot answer at all.
    embedder = embedder_provider()
    model = chat_model_provider()

    turn = await begin(session, conversation_id, user_id, question, client_message_id)
    if turn.reply is not None:
        return ChatResult(turn.user_message, turn.reply, replayed=True)

    try:
        answer = await produce(
            session, embedder, model, turn, user_id, question, top_k, document_ids, settings
        )
        assistant = await save(session, conversation_id, user_id, turn.user_message, answer)
    except Exception:
        await release(session, conversation_id, turn.user_message.id)
        raise
    return ChatResult(turn.user_message, assistant, replayed=False)
