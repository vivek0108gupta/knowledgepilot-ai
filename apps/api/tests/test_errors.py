import logging
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import AppError
from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A fresh app with extra routes that fail in each way the API must handle."""
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/teapot")
    async def teapot() -> None:
        raise AppError(418, "teapot", "I am a teapot.")

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_unhandled_error_becomes_a_generic_500(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.request"):
        response = await client.get("/boom", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "secret internal detail" not in response.text
    # The browser can only read the error if CORS headers survive the failure.
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    # The cause is logged, with its traceback, for whoever investigates.
    assert "Unhandled error on GET /boom" in caplog.text
    assert "secret internal detail" in caplog.text


async def test_app_error_keeps_its_status_and_code(client: AsyncClient) -> None:
    response = await client.get("/teapot")

    assert response.status_code == 418
    assert response.json()["error"]["code"] == "teapot"
    assert response.json()["error"]["message"] == "I am a teapot."


async def test_unknown_route_uses_the_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_validation_error_lists_the_bad_fields(client: AsyncClient) -> None:
    response = await client.get("/items/abc")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "path.item_id"


async def test_request_id_is_generated_and_matches_the_body(client: AsyncClient) -> None:
    response = await client.get("/teapot")

    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert response.json()["error"]["request_id"] == request_id


async def test_safe_request_id_from_the_caller_is_kept(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "trace-123"})

    assert response.headers["x-request-id"] == "trace-123"


async def test_unsafe_request_id_from_the_caller_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "bad id\twith spaces"})

    assert response.headers["x-request-id"] != "bad id\twith spaces"
    assert len(response.headers["x-request-id"]) == 32
