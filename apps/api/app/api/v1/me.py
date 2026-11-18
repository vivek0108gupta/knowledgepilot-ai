from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.schemas.workspace import Me

router = APIRouter(tags=["auth"])


@router.get("/me")
async def me(user: CurrentUser) -> Me:
    """Who the access token belongs to, as the API sees it."""
    return Me(id=user.id, email=user.email)
