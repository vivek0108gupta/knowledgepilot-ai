"""Phase 5.8A: safeguards for the free public portfolio deployment.

- A deployment-wide AI budget, because anyone can sign up to a public demo.
- SSE keep-alive comments that never move the answer generator between tasks.
- Every application table is covered by the Supabase Data API lockdown (migration 0009).
- The container listens on the port the host provides.
"""

# `library` is a fixture shared with test_chat; tests take it as an argument.
# ruff: noqa: F811

import asyncio
import importlib.util
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.chat import KEEPALIVE, with_keepalive
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.base import Base
from app.models.usage import UsageOperation
from app.services import usage
from app.services.pricing import get_pricing
from tests.test_chat import library  # noqa: F401

API_ROOT = Path(__file__).resolve().parent.parent
MODEL = "gpt-4.1-mini"


async def reserve(session_factory: async_sessionmaker, library: dict, key: str) -> None:
    async with session_factory() as session:
        await usage.reserve(
            session,
            user_id=library["owner"],
            workspace_id=uuid.UUID(library["workspace"]),
            operation=UsageOperation.CHAT,
            model=MODEL,
            input_tokens=900,
            output_tokens=100,
            idempotency_key=key,
            ttl_seconds=600,
        )


@pytest.fixture
def budget(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    settings = get_settings()

    def apply(**values: object) -> None:
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return apply


class TestGlobalBudget:
    def one(self) -> Decimal:
        cost = get_pricing().cost(MODEL, 900, 100)
        assert cost is not None and cost > 0
        return cost

    async def test_off_by_default(self) -> None:
        settings = Settings(_env_file=None)
        assert (settings.global_daily_cost_limit, settings.global_monthly_cost_limit) == (
            None,
            None,
        )

    @pytest.mark.parametrize(
        ("setting", "period"),
        [("global_daily_cost_limit", "today"), ("global_monthly_cost_limit", "this month")],
    )
    async def test_the_whole_deployment_stops_at_its_budget(
        self, session_factory, library: dict, budget, setting: str, period: str
    ) -> None:
        # Room for two operations of this size, not three, across everyone.
        budget(**{setting: self.one() * Decimal("2.5")})
        await reserve(session_factory, library, "g1")
        await reserve(session_factory, library, "g2")

        with pytest.raises(AppError) as refused:
            await reserve(session_factory, library, "g3")

        assert (refused.value.status_code, refused.value.code) == (429, "service_budget_exhausted")
        assert period in refused.value.message
        assert refused.value.retry_after_seconds and refused.value.retry_after_seconds > 0

    async def test_it_counts_every_workspace(
        self, session_factory, library: dict, budget, db_client
    ) -> None:
        from tests.test_workspaces import create_workspace

        budget(global_daily_cost_limit=self.one() * Decimal("1.5"))
        await reserve(session_factory, library, "first")
        other_owner = uuid.uuid4()
        other = await create_workspace(db_client, other_owner, "Elsewhere")

        with pytest.raises(AppError) as refused:
            async with session_factory() as session:
                await usage.reserve(
                    session,
                    user_id=other_owner,
                    workspace_id=uuid.UUID(other),
                    operation=UsageOperation.CHAT,
                    model=MODEL,
                    input_tokens=900,
                    output_tokens=100,
                    idempotency_key="other",
                    ttl_seconds=600,
                )

        assert refused.value.code == "service_budget_exhausted"

    async def test_an_unpriced_model_is_refused_while_a_budget_is_set(
        self, session_factory, library: dict, budget
    ) -> None:
        budget(global_monthly_cost_limit=Decimal("5"))

        with pytest.raises(AppError) as refused:
            async with session_factory() as session:
                await usage.reserve(
                    session,
                    user_id=library["owner"],
                    workspace_id=uuid.UUID(library["workspace"]),
                    operation=UsageOperation.CHAT,
                    model="model-with-no-price",
                    input_tokens=900,
                    output_tokens=100,
                    idempotency_key="unpriced",
                    ttl_seconds=600,
                )

        assert (refused.value.status_code, refused.value.code) == (503, "usage_unavailable")

    def test_blank_means_unset_and_negative_is_refused(self) -> None:
        assert Settings(global_daily_cost_limit="").global_daily_cost_limit is None
        assert Settings(global_monthly_cost_limit="1.50").global_monthly_cost_limit == Decimal(
            "1.50"
        )
        with pytest.raises(ValueError):
            Settings(global_daily_cost_limit="-1")


class TestKeepalive:
    async def test_a_quiet_stream_gets_comments_and_every_event_still_arrives(self) -> None:
        async def slow() -> AsyncGenerator[str]:
            yield "a"
            await asyncio.sleep(0.25)
            yield "b"

        items = [item async for item in with_keepalive(slow(), 0.05)]

        assert [i for i in items if i != KEEPALIVE] == ["a", "b"]
        assert items.count(KEEPALIVE) >= 2
        assert KEEPALIVE.startswith(":") and KEEPALIVE.endswith("\n\n")  # an SSE comment

    async def test_the_source_always_runs_in_one_task(self) -> None:
        tasks: set[asyncio.Task] = set()

        async def recording() -> AsyncGenerator[str]:
            for item in "abc":
                tasks.add(asyncio.current_task())  # type: ignore[arg-type]
                await asyncio.sleep(0.02)
                yield item

        assert [i async for i in with_keepalive(recording(), 0.01) if i != KEEPALIVE] == list("abc")
        assert len(tasks) == 1

    async def test_errors_from_the_source_reach_the_response(self) -> None:
        async def failing() -> AsyncGenerator[str]:
            yield "a"
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            [item async for item in with_keepalive(failing(), 1)]

    async def test_closing_early_cancels_and_closes_the_source(self) -> None:
        closed = asyncio.Event()

        async def endless() -> AsyncGenerator[str]:
            try:
                while True:
                    await asyncio.sleep(0.01)
                    yield "x"
            finally:
                closed.set()

        stream = with_keepalive(endless(), 1)
        assert await anext(stream) == "x"
        await stream.aclose()  # the client left

        assert closed.is_set()

    async def test_zero_turns_it_off(self) -> None:
        async def quiet() -> AsyncGenerator[str]:
            await asyncio.sleep(0.05)
            yield "only"

        assert [i async for i in with_keepalive(quiet(), 0)] == ["only"]

    def test_the_setting(self) -> None:
        assert Settings.model_fields["sse_keepalive_seconds"].default == 15.0


class TestSupabaseLockdown:
    def test_every_application_table_is_covered(self) -> None:
        path = next(
            (API_ROOT / "alembic" / "versions").glob("*_0009_supabase_data_api_lockdown.py")
        )
        spec = importlib.util.spec_from_file_location("lockdown_0009", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        missing = set(Base.metadata.tables) - set(module.APP_TABLES)
        assert not missing, (
            f"New tables {sorted(missing)} would be readable through Supabase's Data API: "
            "lock them down in a migration (see migration 0009)."
        )

    async def test_it_does_nothing_without_supabase_roles(self, db_engine) -> None:
        from sqlalchemy import text

        async with db_engine.connect() as connection:
            roles = await connection.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
            )
            rls = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                    "AND relkind = 'r' AND relrowsecurity"
                )
            )
        assert roles == 0 and rls == 0


def test_the_container_listens_on_the_port_the_host_gives() -> None:
    dockerfile = (API_ROOT / "Dockerfile").read_text()
    assert "--port ${PORT:-8000}" in dockerfile
    assert "--host 0.0.0.0" in dockerfile
    assert "alembic upgrade head && exec uvicorn" in dockerfile


class TestConcurrentDeletion:
    """A reservation for something deleted meanwhile is reported as gone (404), never as a
    duplicate operation (409). Found while testing this phase: a conversation deleted while
    its question was starting answered 409."""

    @pytest.mark.parametrize(
        ("field", "code"),
        [("conversation_id", "conversation_not_found"), ("document_id", "document_not_found")],
    )
    async def test_a_deleted_subject_is_404(
        self, session_factory, library: dict, field: str, code: str
    ) -> None:
        with pytest.raises(AppError) as refused:
            async with session_factory() as session:
                await usage.reserve(
                    session,
                    user_id=library["owner"],
                    workspace_id=uuid.UUID(library["workspace"]),
                    operation=UsageOperation.QUERY_EMBEDDING,
                    model="text-embedding-3-small",
                    input_tokens=10,
                    output_tokens=0,
                    idempotency_key=f"gone-{field}",
                    ttl_seconds=120,
                    **{field: uuid.uuid4()},  # does not exist (deleted)
                )

        assert (refused.value.status_code, refused.value.code) == (404, code)

    async def test_a_real_duplicate_is_still_409(self, session_factory, library: dict) -> None:
        await reserve(session_factory, library, "same-key")

        with pytest.raises(AppError) as refused:
            await reserve(session_factory, library, "same-key")

        assert (refused.value.status_code, refused.value.code) == (409, "duplicate_operation")
