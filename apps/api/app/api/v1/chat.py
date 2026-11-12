import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentUser, DbSession
from app.api.v1.conversations import message_out
from app.api.v1.search import EmbedderDep
from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.conversation import ChatRequest, ChatResponse
from app.services import chat as service
from app.services.llm import ChatModelProvider, get_chat_model

router = APIRouter(tags=["chat"])


def get_chat_model_provider() -> ChatModelProvider:
    """How routes reach the model. A callable, so a missing key fails only the requests that
    need it, and tests can put a fake in its place."""
    return get_chat_model


ChatModelDep = Annotated[ChatModelProvider, Depends(get_chat_model_provider)]


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
    settings = get_settings()
    top_k = body.top_k or settings.chat_top_k
    if top_k > settings.chat_max_top_k:
        raise AppError(422, "validation_error", f"top_k can be at most {settings.chat_max_top_k}.")

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
