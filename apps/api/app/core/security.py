import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWK, PyJWKClient

from app.core.config import get_settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

# Asymmetric algorithms only. Accepting HS256 here would let an attacker sign a token using
# the public key as the HMAC secret.
ALLOWED_ALGORITHMS = ["ES256", "RS256"]

# An unknown key ID triggers a JWKS refetch (that is how key rotation is picked up). Without a
# floor on how often, anyone could make the API hammer Supabase by sending made-up key IDs.
MIN_SECONDS_BETWEEN_FORCED_REFRESHES = 30.0


@dataclass(frozen=True, slots=True)
class AuthUser:
    """The caller, as proven by a verified access token. Never built from request data."""

    id: uuid.UUID
    email: str | None


def unauthorized(code: str, message: str) -> AppError:
    return AppError(401, code, message, headers={"WWW-Authenticate": "Bearer"})


class ThrottledJWKClient(PyJWKClient):
    """A JWKS client that limits how often an unknown key ID can force a refetch."""

    def __init__(self, uri: str) -> None:
        super().__init__(uri, cache_jwk_set=True, lifespan=600, timeout=5)
        self._last_forced_refresh = float("-inf")

    def get_signing_keys(self, refresh: bool = False) -> list[PyJWK]:
        if refresh:
            now = time.monotonic()
            if now - self._last_forced_refresh < MIN_SECONDS_BETWEEN_FORCED_REFRESHES:
                refresh = False
            else:
                self._last_forced_refresh = now
        return super().get_signing_keys(refresh)


class TokenVerifier:
    """Verifies Supabase access tokens against the project's published signing keys."""

    def __init__(self, jwks_url: str, issuer: str, audience: str) -> None:
        self._jwks = ThrottledJWKClient(jwks_url)
        self._issuer = issuer
        self._audience = audience

    async def verify(self, token: str) -> AuthUser:
        # PyJWT's key fetch is blocking I/O, so keep it off the event loop.
        return await asyncio.to_thread(self._verify, token)

    def _verify(self, token: str) -> AuthUser:
        # Log why a token was rejected, never the token itself.
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError as error:
            logger.error("Could not fetch signing keys: %s", type(error).__name__)
            raise AppError(
                503, "auth_unavailable", "Sign-in is temporarily unavailable."
            ) from error
        except (jwt.PyJWKClientError, jwt.InvalidTokenError) as error:
            logger.info("Rejected token: %s", type(error).__name__)
            raise unauthorized("invalid_token", "The access token is invalid.") from error

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "iss", "aud"]},
                leeway=10,
            )
        except jwt.ExpiredSignatureError as error:
            logger.info("Rejected token: expired")
            raise unauthorized("token_expired", "The access token has expired.") from error
        except jwt.InvalidTokenError as error:
            logger.info("Rejected token: %s", type(error).__name__)
            raise unauthorized("invalid_token", "The access token is invalid.") from error

        try:
            user_id = uuid.UUID(str(claims["sub"]))
        except ValueError as error:
            logger.info("Rejected token: subject is not a UUID")
            raise unauthorized("invalid_token", "The access token is invalid.") from error

        email = claims.get("email")
        return AuthUser(id=user_id, email=email if isinstance(email, str) and email else None)


@lru_cache
def get_token_verifier() -> TokenVerifier:
    settings = get_settings()
    if not settings.jwks_url or not settings.jwt_issuer:
        raise AppError(503, "auth_not_configured", "Authentication is not configured.")
    return TokenVerifier(settings.jwks_url, settings.jwt_issuer, settings.supabase_jwt_audience)
