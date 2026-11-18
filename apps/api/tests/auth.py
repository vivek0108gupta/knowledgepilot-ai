"""Test tokens: real JWTs signed by a throwaway key, verified through a mocked JWKS."""

import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

KEY_ID = "test-key"
ISSUER = "https://test-project.supabase.co/auth/v1"
AUDIENCE = "authenticated"

# Generated fresh on every run, so no private key is ever stored in the repository.
PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())

JWKS: dict[str, Any] = {
    "keys": [
        {
            **ECAlgorithm.to_jwk(PRIVATE_KEY.public_key(), as_dict=True),
            "kid": KEY_ID,
            "alg": "ES256",
            "use": "sig",
        }
    ]
}


def make_token(
    user_id: uuid.UUID | str | None = None,
    *,
    key: Any = PRIVATE_KEY,
    algorithm: str = "ES256",
    kid: str | None = KEY_ID,
    expires_in: int = 3600,
    drop: tuple[str, ...] = (),
    **claims: Any,
) -> str:
    """Build an access token shaped like Supabase's. Override any claim to make it invalid."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(user_id or uuid.uuid4()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + expires_in,
        "role": "authenticated",
        "email": "user@example.com",
        **claims,
    }
    for name in drop:
        payload.pop(name, None)
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)


def auth(user_id: uuid.UUID | str, **claims: Any) -> dict[str, str]:
    """Request headers that authenticate as `user_id`."""
    return {"Authorization": f"Bearer {make_token(user_id, **claims)}"}
