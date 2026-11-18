from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import AuthUser, get_token_verifier, unauthorized

# auto_error=False so a missing header produces our error format, not FastAPI's default.
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthUser:
    """Require a valid Supabase access token and return who it belongs to.

    This is the only source of the caller's identity. Routes must never read a user ID
    from the path, query, or body to decide who is asking.
    """
    if credentials is None:
        raise unauthorized("not_authenticated", "Sign in to continue.")
    return await get_token_verifier().verify(credentials.credentials)


CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
