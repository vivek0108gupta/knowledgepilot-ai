from httpx import AsyncClient

from app.core.db import get_session
from app.main import app


class FakeResult:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> str | None:
        return self.value


class FakeSession:
    """Stands in for a database session: returns a pgvector version, or raises."""

    def __init__(self, pgvector_version: str | None = None, fail: bool = False) -> None:
        self.pgvector_version = pgvector_version
        self.fail = fail

    async def execute(self, _statement: object) -> FakeResult:
        if self.fail:
            raise ConnectionError("database is down")
        return FakeResult(self.pgvector_version)


def use_session(session: FakeSession) -> None:
    app.dependency_overrides[get_session] = lambda: session


async def test_health_is_ok_without_a_database(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_reports_pgvector_version(client: AsyncClient) -> None:
    use_session(FakeSession(pgvector_version="0.8.0"))

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "pgvector": "0.8.0"}


async def test_ready_is_503_when_database_is_down(client: AsyncClient) -> None:
    use_session(FakeSession(fail=True))

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "database_unavailable"
    # The underlying exception text must not leak to the client.
    assert "database is down" not in response.text


async def test_ready_is_503_when_pgvector_is_missing(client: AsyncClient) -> None:
    use_session(FakeSession(pgvector_version=None))

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "pgvector_missing"


async def test_cors_allows_the_web_origin(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"Origin": "http://localhost:3000"})

    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_cors_ignores_an_unknown_origin(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in response.headers
