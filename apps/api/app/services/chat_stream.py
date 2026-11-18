"""Answering a question while the answer is being written, for Server-Sent Events.

The same pipeline as chat.ask, step for step: chat.begin stores the question and takes the
lease, chat.prepare retrieves and builds the context, the model writes, chat.conclude checks
the whole output, chat.save stores it. The only difference is that the model's text is passed
on as it is written, through provisional.ProvisionalText, before it has been checked.

That text is provisional. The answer that counts is the checked one in `Completed`, which is
the one stored; clients replace what they showed with it. If the checks reject the output, the
stored answer is the standard "not found" reply, and the provisional text is never stored.

When the client goes away (or stops the answer), the stream is closed: generation stops
upstream, nothing is stored, and the lease is released at once, so the question can be
answered again with the same client_message_id. Once storing has begun it is finished, even if
the client has gone: the answer is then there on reload.

Usage: one streamed answer is one usage record, reserved in `chat.begin` and finalised
exactly once, never one per text piece. A completed answer records the provider's counts
from its final event. A stream that fails or is abandoned has no such event, so it records
an estimate: the prompt that was sent and the text received so far.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.models.conversation import Message
from app.models.usage import UsageStatus
from app.services import chat, usage
from app.services.embeddings import Embedder
from app.services.llm import ChatModel, Generation, LLMError, TextDelta
from app.services.prompts import ANSWER_SCHEMA, ANSWER_SCHEMA_NAME
from app.services.provisional import AnswerFieldReader, ProvisionalText

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Started:
    user_message: Message
    # The ID the answer will be stored under.
    assistant_message_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class Delta:
    """Provisional text, to be replaced by the checked answer."""

    text: str


@dataclass(frozen=True, slots=True)
class Completed:
    """The checked answer, as stored."""

    assistant_message: Message
    replayed: bool


@dataclass(frozen=True, slots=True)
class Failed:
    error: AppError


StreamEvent = Started | Delta | Completed | Failed


def unexpected_failure() -> AppError:
    return AppError(
        500,
        "answer_failed",
        "Something went wrong while answering. Your question was saved; try again.",
    )


class AnswerStream:
    """One answer being streamed. `events` yields Started, any number of Delta, then
    Completed or Failed. `close` releases the lease if no answer was stored; it is safe to
    call more than once, and is called however the stream ends, including when `events` is
    never iterated at all."""

    def __init__(
        self,
        session: AsyncSession,
        embedder: Embedder,
        model: ChatModel,
        turn: chat.PendingAnswer,
        user_id: uuid.UUID,
        question: str,
        top_k: int,
        document_ids: list[uuid.UUID],
        settings: Settings,
    ) -> None:
        self._session = session
        self._embedder = embedder
        self._model = model
        self._turn = turn
        self._user_id = user_id
        self._question = question
        self._top_k = top_k
        self._document_ids = document_ids
        self._settings = settings
        self._conversation_id = turn.user_message.conversation_id
        self.assistant_message_id = turn.reply.id if turn.reply is not None else uuid.uuid4()
        # Nothing to release once an answer is stored, or when replaying a stored one.
        self._settled = turn.reply is not None
        # Set when answering failed, as opposed to being abandoned by the client.
        self._error_code: str | None = None

    async def close(self) -> None:
        if self._settled:
            return
        self._settled = True
        # Shielded: this runs while the request is being cancelled, when the client has gone.
        failed = self._error_code is not None
        with anyio.CancelScope(shield=True):
            await chat.release(
                self._session,
                self._conversation_id,
                self._turn.user_message.id,
                self._turn.meter,
                status=UsageStatus.FAILED if failed else UsageStatus.CANCELLED,
                error_code=self._error_code if failed else "client_disconnected",
            )
        logger.info("Answer in conversation %s ended without being stored", self._conversation_id)

    async def events(self) -> AsyncIterator[StreamEvent]:
        turn = self._turn
        try:
            yield Started(turn.user_message, self.assistant_message_id, turn.reply is not None)
            if turn.reply is not None:
                yield Completed(turn.reply, replayed=True)
                return

            prepared = await chat.prepare(
                self._session,
                self._embedder,
                turn,
                self._user_id,
                self._question,
                self._top_k,
                self._document_ids,
                self._settings,
            )
            if prepared.context is None:
                answer = chat.no_evidence_answer(prepared.retrieval)
            else:
                provisional = ProvisionalText(prepared.context.boundary)
                reader = AnswerFieldReader()
                generation: Generation | None = None
                meter = turn.meter
                if meter is not None:
                    meter.started(prepared.instructions, prepared.turns, streamed=True)
                    await usage.mark_called(
                        self._session, meter.reservation_id, meter.prompt_tokens
                    )
                beat = anyio.current_time()
                try:
                    async with aclosing(
                        self._model.stream(
                            instructions=prepared.instructions,
                            turns=prepared.turns,
                            schema=ANSWER_SCHEMA,
                            schema_name=ANSWER_SCHEMA_NAME,
                        )
                    ) as upstream:
                        async for item in upstream:
                            if isinstance(item, TextDelta):
                                if meter is not None:
                                    meter.receive(item.text)
                                    # Keep the reservation alive while text keeps coming.
                                    if (
                                        anyio.current_time() - beat
                                        >= self._settings.usage_heartbeat_seconds
                                    ):
                                        beat = anyio.current_time()
                                        await usage.heartbeat(
                                            self._session,
                                            meter.reservation_id,
                                            self._settings.usage_reservation_ttl_seconds,
                                        )
                                text = provisional.feed(reader.feed(item.text))
                                if text:
                                    yield Delta(text)
                            else:
                                generation = item
                                if meter is not None:
                                    meter.reported(
                                        item.input_tokens, item.output_tokens, item.model, item.text
                                    )
                except LLMError as error:
                    if meter is not None:
                        meter.reported(error.input_tokens, error.output_tokens, error.model)
                        meter.maybe_executed = error.maybe_executed
                    raise chat.ai_error(error) from error
                if generation is None:
                    raise chat.ai_error(
                        LLMError("no final output", code="invalid_response", retryable=False)
                    )
                if provisional.withheld:
                    logger.warning("Stopped showing an answer that repeated the instructions")
                answer = chat.conclude(self._conversation_id, prepared, generation)

            # Once begun, storing is finished even if the client goes away meanwhile.
            with anyio.CancelScope(shield=True):
                message = await chat.save(
                    self._session,
                    self._conversation_id,
                    self._user_id,
                    turn.user_message,
                    answer,
                    message_id=self.assistant_message_id,
                    meter=turn.meter,
                )
            self._settled = True
            yield Completed(message, replayed=False)
        except AppError as error:
            self._error_code = error.code
            await self.close()
            yield Failed(error)
        except Exception:
            logger.exception("Streaming an answer failed in conversation %s", self._conversation_id)
            self._error_code = "answer_failed"
            await self.close()
            yield Failed(unexpected_failure())
        finally:
            await self.close()
