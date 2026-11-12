import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

# Pin every setting the tests depend on before the app reads them, so the shell's
# environment and any local `.env` cannot change the outcome.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://knowledgepilot:knowledgepilot@localhost:5434/knowledgepilot_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["CORS_ORIGINS"] = "http://localhost:3000"
os.environ["SUPABASE_URL"] = "https://test-project.supabase.co"
os.environ["SUPABASE_JWKS_URL"] = ""
os.environ["SUPABASE_ISSUER"] = ""
os.environ["SUPABASE_JWT_AUDIENCE"] = "authenticated"
os.environ["OPENAI_API_KEY"] = ""
os.environ["OPENAI_CHAT_MODEL"] = "gpt-4.1-mini"
os.environ["SEARCH_MIN_SIMILARITY"] = "0.2"
os.environ["SEARCH_MAX_TOP_K"] = "20"
os.environ["CHUNK_SIZE_TOKENS"] = "800"
os.environ["CHUNK_OVERLAP_TOKENS"] = "100"
# Generous limits for the suite as a whole, so tests about other things are never refused.
# Tests of the limits themselves set their own values (tests/test_cost_controls.py).
for name, value in {
    "RATE_LIMITS_ENABLED": "true",
    "RATE_LIMIT_CHAT_PER_MINUTE": "100000",
    "RATE_LIMIT_CHAT_PER_WORKSPACE_PER_MINUTE": "100000",
    "RATE_LIMIT_SEARCH_PER_MINUTE": "100000",
    "RATE_LIMIT_UPLOAD_PER_HOUR": "100000",
    "MAX_CONCURRENT_GENERATIONS_PER_USER": "1000",
    "MAX_CONCURRENT_AI_OPERATIONS_PER_WORKSPACE": "1000",
    "MAX_INGESTION_JOBS_PER_WORKSPACE": "1000",
    "USER_DAILY_TOKEN_LIMIT": "1000000000",
    "USER_DAILY_CHAT_LIMIT": "100000",
    "WORKSPACE_MONTHLY_TOKEN_LIMIT": "1000000000",
    "WORKSPACE_MONTHLY_COST_LIMIT": "1000000",
    "USAGE_PRICING_JSON": "",
}.items():
    os.environ[name] = value

import asyncpg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from jwt import PyJWKClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.api.v1.chat import get_chat_model_provider  # noqa: E402
from app.api.v1.documents import get_storage_provider  # noqa: E402
from app.api.v1.search import get_embedder_provider  # noqa: E402
from app.core.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.services.processing import get_processing_queue, process_document  # noqa: E402
from tests.auth import JWKS  # noqa: E402
from tests.fakes import FakeChatModel, FakeEmbedder, FakeStorage, RecordingQueue  # noqa: E402

API_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def mock_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the test signing key instead of fetching Supabase's. No test touches the network."""
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda _self: JWKS)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in-process, without a database."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def create_test_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        # These tests empty tables. Refuse to run against anything not marked as a test database.
        raise RuntimeError(f"Refusing to run tests against database {url.database!r}.")
    try:
        connection = await asyncpg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            database="postgres",
            timeout=5,
        )
    except (OSError, asyncpg.PostgresError, TimeoutError) as error:
        raise RuntimeError(
            f"Integration tests need Postgres at {url.host}:{url.port}. "
            "Start it with `docker compose up -d db`."
        ) from error
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", url.database
        )
        if not exists:
            await connection.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def database() -> None:
    """Create the test database if needed and bring it to the latest migration, once per run."""
    asyncio.run(create_test_database())
    command.upgrade(Config(str(API_ROOT / "alembic.ini")), "head")


# A database connection that is accepted but never completes (seen with Docker's port
# forwarding) would otherwise make every test wait a minute per connection, which looks like
# a hang. Fail each attempt quickly instead, and stop the whole run on the first one.
DB_CONNECT_TIMEOUT_SECONDS = 10
DB_COMMAND_TIMEOUT_SECONDS = 60


@pytest.fixture
async def db_engine(database: None) -> AsyncIterator[AsyncEngine]:
    """A real Postgres engine with empty tables.

    NullPool opens a connection per use, so none is reused on another test's event loop.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={
            "timeout": DB_CONNECT_TIMEOUT_SECONDS,
            "command_timeout": DB_COMMAND_TIMEOUT_SECONDS,
        },
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE workspaces, rate_limit_buckets CASCADE"))
    except (TimeoutError, OSError) as error:
        await engine.dispose()
        url = make_url(TEST_DATABASE_URL)
        pytest.exit(
            f"The test database at {url.host}:{url.port} stopped responding "
            f"({type(error).__name__}). Check `docker compose ps`, then run the tests again.",
            returncode=3,
        )
    yield engine
    await engine.dispose()


@pytest.fixture
def storage() -> FakeStorage:
    """An in-memory stand-in for the private storage bucket."""
    return FakeStorage()


@pytest.fixture
def embedder() -> FakeEmbedder:
    """A deterministic stand-in for the OpenAI embedding API."""
    return FakeEmbedder()


@pytest.fixture
def chat_model() -> FakeChatModel:
    """A deterministic stand-in for the language model."""
    return FakeChatModel()


@pytest.fixture
def queue() -> RecordingQueue:
    """Records queued documents; tests run processing explicitly with `process`."""
    return RecordingQueue()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def process(session_factory: async_sessionmaker, storage: FakeStorage, embedder: FakeEmbedder):  # noqa: ANN201
    """Run the real processing pipeline for one document, against the test database."""

    async def run(document_id: str | uuid.UUID) -> None:
        await process_document(
            uuid.UUID(str(document_id)), session_factory, lambda: storage, lambda: embedder
        )

    return run


@pytest.fixture
async def db_client(
    session_factory: async_sessionmaker,
    storage: FakeStorage,
    queue: RecordingQueue,
    embedder: FakeEmbedder,
    chat_model: FakeChatModel,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client whose requests run against the real test database.

    Storage and the processing queue are replaced by fakes; nothing leaves this process.
    """

    async def real_session():  # noqa: ANN202
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = real_session
    app.dependency_overrides[get_storage_provider] = lambda: lambda: storage
    app.dependency_overrides[get_processing_queue] = lambda: queue
    app.dependency_overrides[get_embedder_provider] = lambda: lambda: embedder
    app.dependency_overrides[get_chat_model_provider] = lambda: lambda: chat_model
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
