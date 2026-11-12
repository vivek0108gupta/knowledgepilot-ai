import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Response, status
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.deps import CurrentUser, DbSession
from app.api.v1.conversations import message_out
from app.api.v1.search import EmbedderDep
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.usage import UsageOperation
from app.schemas.conversation import ChatRequest, ChatResponse, MessageOut
from app.services import chat as service
from app.services import chat_stream, usage
from app.services import conversations as conversation_service
from app.services.llm import ChatModelProvider, get_chat_model

router = APIRouter(tags=["chat"])


def get_chat_model_provider() -> ChatModelProvider:
    """How routes reach the model. A callable, so a missing key fails only the requests that
    need it, and tests can put a fake in its place."""
    return get_chat_model


ChatModelDep = Annotated[ChatModelProvider, Depends(get_chat_model_provider)]


def requested_top_k(body: ChatRequest, settings: Settings) -> int:
    top_k = body.top_k or settings.chat_top_k
    if top_k > settings.chat_max_top_k:
        raise AppError(422, "validation_error", f"top_k can be at most {settings.chat_max_top_k}.")
    return top_k


@router.post("/conversations/{conversation_id}/chat/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_answer(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> dict[str, bool]:
    """Say that you are stopping the answer being written in your conversation. Send it just
    before closing the stream: the answer is then recorded as cancelled by you rather than as
    a lost connection. Closing the stream is what stops it; this only labels why."""
    await conversation_service.get_owned(session, conversation_id, user.id)
    await session.rollback()
    return {"cancel_requested": await usage.request_cancel(session, conversation_id, user.id)}


@router.post("/conversations/{conversation_id}/chat", status_code=status.HTTP_201_CREATED)
async def chat(
    conversation_id: uuid.UUID,
    body: ChatRequest,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    embedder_provider: EmbedderDep,
    chat_model_provider: ChatModelDep,
) -> ChatResponse:
    """Ask a question and get an answer from this conversation's workspace documents.

    Stores the question, finds relevant passages in the workspace the conversation belongs
    to, asks the model to answer from them alone, checks its citations against those
    passages, and stores the answer. Sending the same `client_message_id` again returns the
    stored answer (200) instead of producing a second one.
    """
    top_k = requested_top_k(body, get_settings())
    result = await service.ask(
        session,
        embedder_provider,
        chat_model_provider,
        conversation_id,
        user.id,
        body.message,
        body.client_message_id,
        body.document_ids,
        top_k,
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return ChatResponse(
        conversation_id=conversation_id,
        user_message=message_out(result.user_message),
        assistant_message=message_out(result.assistant_message),
    )


class EventStreamResponse(StreamingResponse):
    """A Server-Sent Events response that always cleans up.

    However the response ends (finished, the client disconnected, or it was never sent at
    all), the event generator is closed and `on_close` runs, shielded from the cancellation
    that a disconnect causes.
    """

    media_type = "text/event-stream"

    def __init__(
        self, events: AsyncGenerator[str], on_close: Callable[[], Awaitable[None]]
    ) -> None:
        super().__init__(
            events,
            headers={
                "Cache-Control": "no-cache, no-transform",
                # Proxies such as nginx must pass each event on as it is written.
                "X-Accel-Buffering": "no",
            },
        )
        self._events = events
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._events.aclose()
                await self._on_close()


def sse(event_id: int, name: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {name}\ndata: {payload}\n\n"


def message_json(message: MessageOut) -> dict[str, Any]:
    return message.model_dump(mode="json")


async def encode(stream: chat_stream.AnswerStream) -> AsyncGenerator[str]:
    """The answer's events in the wire format documented on `chat_stream_endpoint`."""
    event_id = 0
    async for event in stream.events():
        event_id += 1
        if isinstance(event, chat_stream.Started):
            yield sse(
                event_id,
                "message_started",
                {
                    "conversation_id": str(event.user_message.conversation_id),
                    "user_message": message_json(message_out(event.user_message)),
                    "assistant_message_id": str(event.assistant_message_id),
                    "replayed": event.replayed,
                },
            )
        elif isinstance(event, chat_stream.Delta):
            yield sse(event_id, "text_delta", {"text": event.text})
        elif isinstance(event, chat_stream.Completed):
            assistant = message_out(event.assistant_message)
            yield sse(
                event_id,
                "citations",
                {
                    "citations": [
                        citation.model_dump(mode="json") for citation in assistant.citations
                    ]
                },
            )
            event_id += 1
            yield sse(
                event_id,
                "message_completed",
                {
                    "assistant_message": message_json(assistant),
                    "model": event.assistant_message.model,
                    "input_tokens": event.assistant_message.input_tokens,
                    "output_tokens": event.assistant_message.output_tokens,
                    "replayed": event.replayed,
                },
            )
        else:
            error = event.error
            yield sse(
                event_id,
                "error",
                {
                    "code": error.code,
                    "message": error.message,
                    "status": error.status_code,
                    # The question is stored whenever an answer fails after the stream began.
                    "question_saved": True,
                },
            )


@router.post(
    "/conversations/{conversation_id}/chat/stream",
    response_class=EventStreamResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "An event stream."}},
)
async def chat_stream_endpoint(
    conversation_id: uuid.UUID,
    body: ChatRequest,
    user: CurrentUser,
    session: DbSession,
    embedder_provider: EmbedderDep,
    chat_model_provider: ChatModelDep,
) -> EventStreamResponse:
    """Ask a question and receive the answer as it is written, as Server-Sent Events.

    Takes the same body as `/chat` and applies the same checks. Anything that prevents
    answering from starting (sign-in, access, an archived conversation, an answer already in
    progress, a reused message ID, AI not configured) is an ordinary JSON error response.
    Once started, the response is `200 text/event-stream`, with these events, each with an
    increasing `id`:

    - `message_started`: `{conversation_id, user_message, assistant_message_id, replayed}`.
      The question is stored.
    - `text_delta` (any number): `{text}`. Provisional answer text, to be appended to what
      was shown. Source markers are left out, and it is not yet checked.
    - `citations`: `{citations}`. The checked citations of the final answer.
    - `message_completed`: `{assistant_message, model, input_tokens, output_tokens,
      replayed}`. The checked, stored answer. It replaces the provisional text, and its
      `[n]` markers match the citations.
    - `error`: `{code, message, status, question_saved}`. Answering failed; no answer was
      stored and the conversation is free. Sending the same `client_message_id` again
      retries.

    A stream ends after `message_completed` or `error`. If the client disconnects before
    then, the answer is abandoned: nothing is stored and the conversation is freed. Streams
    cannot be resumed; retrying with the same `client_message_id` returns the stored answer
    if there is one (`replayed: true`), or answers the stored question again.
    """
    settings = get_settings()
    top_k = requested_top_k(body, settings)
    # Fail before storing anything if the server cannot answer at all.
    embedder = embedder_provider()
    model = chat_model_provider()
    await service.admit(session, conversation_id, user.id)
    turn = await service.begin(
        session,
        conversation_id,
        user.id,
        body.message,
        body.client_message_id,
        operation=UsageOperation.CHAT_STREAM,
    )
    stream = chat_stream.AnswerStream(
        session,
        embedder,
        model,
        turn,
        user.id,
        body.message,
        top_k,
        body.document_ids,
        settings,
    )
    return EventStreamResponse(encode(stream), on_close=stream.close)
