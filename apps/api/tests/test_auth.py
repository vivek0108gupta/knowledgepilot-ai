import base64
import json
import logging
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from httpx import AsyncClient
from jwt import PyJWKClient

from app.core import security
from app.core.errors import AppError
from app.core.security import TokenVerifier
from tests.auth import AUDIENCE, ISSUER, JWKS, OTHER_PRIVATE_KEY, PRIVATE_KEY, auth, make_token

ME = "/api/v1/me"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_valid_token_identifies_the_user_from_sub(client: AsyncClient) -> None:
    user_id = uuid.uuid4()

    response = await client.get(ME, headers=auth(user_id, email="ada@example.com"))

    assert response.status_code == 200
    assert response.json() == {"id": str(user_id), "email": "ada@example.com"}


async def test_missing_header_is_401(client: AsyncClient) -> None:
    response = await client.get(ME)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_non_bearer_scheme_is_401(client: AsyncClient) -> None:
    response = await client.get(ME, headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert response.status_code == 401


async def test_garbage_token_is_401(client: AsyncClient) -> None:
    response = await client.get(ME, headers=bearer("not.a.jwt"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_expired_token_is_401(client: AsyncClient) -> None:
    response = await client.get(ME, headers=bearer(make_token(expires_in=-3600)))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"


async def test_token_signed_by_another_key_is_401(client: AsyncClient) -> None:
    # Same key ID, different private key: the signature cannot match the published key.
    response = await client.get(ME, headers=bearer(make_token(key=OTHER_PRIVATE_KEY)))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_tampered_payload_is_401(client: AsyncClient) -> None:
    header, _payload, signature = make_token().split(".")
    forged_claims = {"sub": str(uuid.uuid4()), "iss": ISSUER, "aud": AUDIENCE, "exp": 9999999999}
    forged = base64.urlsafe_b64encode(json.dumps(forged_claims).encode()).rstrip(b"=").decode()

    response = await client.get(ME, headers=bearer(f"{header}.{forged}.{signature}"))

    assert response.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil.example/auth/v1"},
        {"aud": "some-other-audience"},
        {"sub": "not-a-uuid"},
    ],
    ids=["wrong issuer", "wrong audience", "subject is not a UUID"],
)
async def test_wrong_claims_are_401(client: AsyncClient, claims: dict[str, str]) -> None:
    response = await client.get(ME, headers=bearer(make_token(**claims)))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


@pytest.mark.parametrize("claim", ["exp", "sub", "iss", "aud"])
async def test_missing_required_claim_is_401(client: AsyncClient, claim: str) -> None:
    response = await client.get(ME, headers=bearer(make_token(drop=(claim,))))

    assert response.status_code == 401


async def test_unknown_key_id_is_401(client: AsyncClient) -> None:
    response = await client.get(ME, headers=bearer(make_token(kid="rotated-away")))

    assert response.status_code == 401


async def test_token_without_key_id_is_401(client: AsyncClient) -> None:
    response = await client.get(ME, headers=bearer(make_token(kid=None)))

    assert response.status_code == 401


async def test_unsigned_token_is_401(client: AsyncClient) -> None:
    def encode(part: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    header = encode({"alg": "none", "typ": "JWT", "kid": "test-key"})
    payload = encode({"sub": str(uuid.uuid4()), "iss": ISSUER, "aud": AUDIENCE, "exp": 9999999999})

    response = await client.get(ME, headers=bearer(f"{header}.{payload}."))

    assert response.status_code == 401


async def test_hs256_token_signed_with_the_public_key_is_401(client: AsyncClient) -> None:
    """Algorithm confusion: the public key is public, so it must never work as an HMAC secret."""
    public_pem = PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    payload = {"sub": str(uuid.uuid4()), "iss": ISSUER, "aud": AUDIENCE, "exp": 9999999999}
    header = {"alg": "HS256", "typ": "JWT", "kid": "test-key"}

    def encode(part: dict[str, object]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=")

    signing_input = encode(header) + b"." + encode(payload)
    signature = jwt.algorithms.HMACAlgorithm(jwt.algorithms.HMACAlgorithm.SHA256).sign(
        signing_input, public_pem
    )
    token = (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()

    response = await client.get(ME, headers=bearer(token))

    assert response.status_code == 401


async def test_user_id_in_the_request_is_ignored(client: AsyncClient) -> None:
    """Identity comes from the token only; nothing the caller sends can override it."""
    real_user, claimed_user = uuid.uuid4(), uuid.uuid4()

    response = await client.get(
        ME,
        params={"user_id": str(claimed_user)},
        headers={**auth(real_user), "X-User-ID": str(claimed_user)},
    )

    assert response.json()["id"] == str(real_user)


async def test_rejected_token_is_not_logged(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    token = make_token(expires_in=-3600)

    with caplog.at_level(logging.DEBUG):
        await client.get(ME, headers=bearer(token))

    assert "Rejected token" in caplog.text
    assert token not in caplog.text
    assert token.split(".")[2] not in caplog.text


async def test_jwks_outage_is_503_not_401(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_self: PyJWKClient) -> None:
        raise jwt.PyJWKClientConnectionError("network down")

    monkeypatch.setattr(PyJWKClient, "fetch_data", fail)
    # A fresh verifier, so no cached keys hide the outage.
    monkeypatch.setattr(
        security, "get_token_verifier", lambda: TokenVerifier("https://x/jwks", ISSUER, AUDIENCE)
    )
    monkeypatch.setattr("app.api.deps.get_token_verifier", security.get_token_verifier)

    response = await client.get(ME, headers=bearer(make_token()))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_unavailable"


async def test_unknown_key_ids_cannot_force_repeated_jwks_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetches = 0

    def counting_fetch(_self: PyJWKClient) -> dict[str, object]:
        nonlocal fetches
        fetches += 1
        return JWKS

    monkeypatch.setattr(PyJWKClient, "fetch_data", counting_fetch)
    verifier = TokenVerifier("https://x/jwks", ISSUER, AUDIENCE)

    for _ in range(5):
        with pytest.raises(AppError):
            await verifier.verify(make_token(kid="made-up"))

    # One initial fetch, plus one forced refresh for the first unknown key ID. Without the
    # throttle, each of the five lookups would force its own refresh (six fetches).
    assert fetches == 2
