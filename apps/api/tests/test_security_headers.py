"""Phase 5.7: hardening headers on every API response, including errors."""

import uuid

import pytest
from httpx import AsyncClient

import app.core.middleware as middleware_module
from app.core.config import Settings


async def test_every_response_is_hardened(client: AsyncClient) -> None:
    for response in (
        await client.get("/api/v1/health"),
        await client.get(f"/api/v1/workspaces/{uuid.uuid4()}"),  # 401
        await client.get("/no-such-route"),
    ):
        headers = response.headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
        assert headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in headers  # development


async def test_hsts_in_production(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        middleware_module, "get_settings", lambda: Settings(environment="production")
    )
    response = await client.get("/api/v1/health")
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")


def test_a_route_that_sets_its_own_caching_keeps_it() -> None:
    from starlette.responses import Response

    response = Response(headers={"Cache-Control": "no-cache, no-transform"})
    middleware_module.apply_security_headers("/api/v1/conversations/x/chat/stream", response)
    assert response.headers["cache-control"] == "no-cache, no-transform"


def test_the_development_docs_may_load_their_assets() -> None:
    from starlette.responses import Response

    response = Response()
    middleware_module.apply_security_headers("/docs", response)
    assert "content-security-policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"
