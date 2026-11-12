import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import CurrentUser, DbSession
from app.models.conversation import ConversationStatus, Message
from app.schemas.conversation import (
    CitationOut,
    ConversationCreate,
    ConversationOut,
    ConversationPage,
    ConversationUpdate,
    MessageCreate,
    MessageOut,
    MessagePage,
)
from app.services import conversations as service
from app.services.conversations import ConversationView

router = APIRouter(tags=["conversations"])


def to_out(view: ConversationView) -> ConversationOut:
    conversation = view.conversation
    return ConversationOut(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        title=conversation.title,
        status=ConversationStatus(conversation.status),
        message_count=view.message_count,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


# Metadata keys a client may see. Everything else stored with a message stays internal.
PUBLIC_METADATA_KEYS = ("grounded",)


def message_out(message: Message) -> MessageOut:
    # Built field by field: token counts and the client's idempotency ID are internal, and so
    # are most metadata keys (the prompt version and retrieval scores, for instance).
    stored = message.message_metadata or {}
    public = {key: stored[key] for key in PUBLIC_METADATA_KEYS if key in stored}
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        sequence_number=message.sequence_number,
        metadata=public or None,
        model=message.model,
        citations=[CitationOut.model_validate(item) for item in stored.get("citations") or []],
        created_at=message.created_at,
    )


@router.post("/workspaces/{workspace_id}/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    body: ConversationCreate | None = None,
) -> ConversationOut:
    """Start a conversation in a workspace the caller belongs to. It is private to them."""
    title = (body or ConversationCreate()).title
    return to_out(await service.create(session, workspace_id, user.id, title))


@router.get("/workspaces/{workspace_id}/conversations")
async def list_conversations(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    status_filter: Annotated[
        ConversationStatus | None,
        Query(alias="status", description="`active` (default) or `archived`."),
    ] = ConversationStatus.ACTIVE,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationPage:
    """The caller's own conversations in this workspace, most recently active first.

    Other members' conversations are never included, and no message text is returned.
    """
    views, total = await service.list_for_user(
        session, workspace_id, user.id, status_filter, limit, offset
    )
    return ConversationPage(
        items=[to_out(view) for view in views], total=total, limit=limit, offset=offset
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> ConversationOut:
    return to_out(await service.get_view(session, conversation_id, user.id))


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationUpdate, user: CurrentUser, session: DbSession
) -> ConversationOut:
    """Rename a conversation. Only its title can be changed; it cannot be moved or reassigned."""
    return to_out(await service.rename(session, conversation_id, user.id, body.title))


@router.post("/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> ConversationOut:
    """Hide a conversation from the default list without deleting it."""
    return to_out(
        await service.set_status(session, conversation_id, user.id, ConversationStatus.ARCHIVED)
    )


@router.post("/conversations/{conversation_id}/restore")
async def restore_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> ConversationOut:
    return to_out(
        await service.set_status(session, conversation_id, user.id, ConversationStatus.ACTIVE)
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Response:
    """Delete a conversation and all of its messages."""
    await service.remove(session, conversation_id, user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        int | None, Query(ge=1, description="Only messages before this sequence number.")
    ] = None,
) -> MessagePage:
    """The most recent messages, oldest first. Pass `before` to page back through history."""
    messages, total, has_more = await service.list_messages(
        session, conversation_id, user.id, limit, before
    )
    return MessagePage(
        items=[message_out(message) for message in messages], total=total, has_more=has_more
    )


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def create_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    response: Response,
    user: CurrentUser,
    session: DbSession,
) -> MessageOut:
    """Add a message from the signed-in user.

    Always stored with the `user` role. Sending the same `client_message_id` again returns
    the message already stored (with 200) instead of creating a second one.
    """
    result = await service.add_user_message(
        session, conversation_id, user.id, body.content, body.client_message_id
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return message_out(result.message)
