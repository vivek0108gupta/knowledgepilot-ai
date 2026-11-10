import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

# Pin settings the tests assert on, before the app reads them, so the shell's environment
# and any local `.env` cannot change the outcome.
os.environ["CORS_ORIGINS"] = "http://localhost:3000"

from app.main import app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in-process, with dependency overrides reset afterwards."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
