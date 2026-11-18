from fastapi import APIRouter

from app.api.v1 import chat, conversations, documents, health, me, search, usage, workspaces

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(me.router)
api_router.include_router(workspaces.router)
api_router.include_router(documents.router)
api_router.include_router(search.router)
api_router.include_router(conversations.router)
api_router.include_router(chat.router)
api_router.include_router(usage.router)
