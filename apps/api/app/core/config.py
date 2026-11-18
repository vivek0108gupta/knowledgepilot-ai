import uuid
from decimal import Decimal
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default model prices for usage cost estimates: OpenAI's published list prices for the
# models this application uses, as known when written, in USD per million tokens. They are
# estimates (no discounts, cached input or later price changes); replace them, with a new
# version label, through USAGE_PRICING_JSON. See app/services/pricing.py.
DEFAULT_PRICING = {
    # A new version whenever the table changes; recorded usage keeps the version it used.
    "version": "openai-list-2026-11",
    "currency": "USD",
    # When this table took effect, and when it was last checked against
    # https://developers.openai.com/api/docs/pricing (cached-input prices are not modelled).
    "effective_from": "2026-11-12",
    "verified_on": "2026-11-12",
    "models": {
        "gpt-4.1-mini": {"input_per_million": "0.40", "output_per_million": "1.60"},
        # Candidates evaluated in Phase 5.7 (list prices checked 2026-11-12).
        "gpt-5.4-mini": {"input_per_million": "0.75", "output_per_million": "4.50"},
        "gpt-5.6-luna": {"input_per_million": "0.20", "output_per_million": "1.20"},
        "gpt-6-luna": {"input_per_million": "0.10", "output_per_million": "0.50"},
        "text-embedding-3-small": {"input_per_million": "0.02", "output_per_million": "0"},
    },
}


# Request parameters each evaluated chat model accepts (docs/phase-5.7-model-evaluation.md).
# Checked against the live API on 2026-11-12: gpt-6-luna and gpt-5.6-luna reject
# `temperature`; gpt-4.1-mini rejects `reasoning`. A model not listed (matched by the longest
# listed prefix) is sent neither, which every model accepts.
CHAT_MODEL_CAPABILITIES: dict[str, dict[str, bool]] = {
    "gpt-4.1-mini": {"temperature": True, "reasoning_effort": False},
    "gpt-5.4-mini": {"temperature": True, "reasoning_effort": True},
    "gpt-5.6-luna": {"temperature": False, "reasoning_effort": True},
    "gpt-6-luna": {"temperature": False, "reasoning_effort": True},
}

REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


def chat_model_capabilities(model: str) -> dict[str, bool]:
    best = max(
        (name for name in CHAT_MODEL_CAPABILITIES if model.startswith(name)), key=len, default=None
    )
    return CHAT_MODEL_CAPABILITIES.get(best, {}) if best else {}


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (and `.env` in local development)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "KnowledgePilot AI"
    environment: str = "development"
    # The interactive API docs (/docs, /redoc, /openapi.json). Off in production unless set.
    api_docs_enabled: bool | None = None
    # The largest request body any JSON endpoint accepts. Uploads have their own limit.
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)
    log_level: str = "INFO"

    # Local default matches docker-compose.yml, which publishes Postgres on host port 5434.
    database_url: str = (
        "postgresql+asyncpg://knowledgepilot:knowledgepilot@localhost:5434/knowledgepilot"
    )

    # Comma-separated list of origins allowed to call the API from a browser.
    cors_origins: str = "http://localhost:3000"

    # Supabase Auth. Only SUPABASE_URL is required; the other two are derived from it
    # unless set explicitly (for example, for a custom auth domain).
    supabase_url: str = ""
    supabase_jwks_url: str = ""
    supabase_issuer: str = ""
    supabase_jwt_audience: str = "authenticated"

    # Supabase Storage. The secret (or legacy service-role) key is server-only: it bypasses
    # storage policies, so it must never reach the browser. SecretStr keeps it out of logs
    # and error messages.
    supabase_secret_key: SecretStr = SecretStr("")
    supabase_storage_bucket: str = "knowledgepilot-documents"
    # How long a download link works. Short on purpose: a leaked link soon stops working.
    signed_url_ttl_seconds: int = 60

    # How many documents may be extracted at the same time in this process.
    processing_concurrency: int = 2

    # OpenAI, for embeddings. Server-only, like the storage key.
    openai_api_key: SecretStr = SecretStr("")
    # Database connections per API process: a pool of DB_POOL_SIZE kept open, plus up to
    # DB_MAX_OVERFLOW more under load. replicas x (size + overflow) must stay below the
    # server's max_connections (docs/phase-5.7-load-testing.md); use a pooler beyond that.
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    # Bearer token for GET /metrics (operators only). Empty: /metrics does not exist.
    metrics_token: SecretStr = SecretStr("")
    # A streamed answer sends an SSE comment after this many seconds without an event, so
    # proxies do not close a quiet stream (for example while the provider is retried).
    # 0 turns it off.
    sse_keepalive_seconds: float = Field(default=15.0, ge=0)
    # "text" (default) or "json": one JSON object per line, for log shippers.
    log_format: str = "text"
    embedding_model: str = "text-embedding-3-small"
    # Must equal the size of the `document_chunks.embedding` column (1536).
    embedding_dimensions: int = 1536
    # Chunks sent per embedding request, and how hard to try when a request fails.
    embedding_batch_size: int = 64
    embedding_max_attempts: int = 5
    embedding_timeout_seconds: float = 30.0

    # The chat model answers will be generated with. An identifier only: nothing calls it
    # yet, and no code names a model directly, so changing models is a setting change.
    # Chosen in Phase 5.7 (docs/phase-5.7-model-evaluation.md). Roll back by setting
    # OPENAI_CHAT_MODEL=gpt-4.1-mini (until its shutdown on 2027-01-14) or gpt-5.4-mini.
    openai_chat_model: str = "gpt-6-luna"

    # Answering questions from documents (retrieval-augmented generation).
    chat_temperature: float = 0.2
    # For models that support it (see CHAT_MODEL_CAPABILITIES): how much the model reasons
    # before answering; ignored for models that reject it. Empty: the model's own default.
    chat_reasoning_effort: str | None = "low"
    # Used once, automatically, if the configured model is reported as not found (for example
    # after its shutdown date). Empty: no fallback. Also the rollback target.
    openai_chat_fallback_model: str = "gpt-5.4-mini"
    chat_max_output_tokens: int = 1000
    chat_timeout_seconds: float = 45.0
    # The most a streamed answer may take from request to last word.
    chat_stream_timeout_seconds: float = 120.0
    chat_max_attempts: int = 3
    # Passages retrieved per question, and the most a request may ask for.
    chat_top_k: int = 5
    chat_max_top_k: int = 10
    # Passages less similar than this are not given to the model. The same scale as
    # SEARCH_MIN_SIMILARITY; an estimate, to be tuned against real questions.
    chat_min_similarity: float = 0.2
    # Token budgets for what the model is shown: retrieved passages, and earlier turns.
    chat_context_tokens: int = 6000
    chat_max_chunks_per_document: int = 3
    chat_history_messages: int = 10
    chat_history_tokens: int = 2000
    # How long one answer may hold a conversation before another request may take over.
    # Longer than the worst case of chat_timeout_seconds x chat_max_attempts plus backoff.
    chat_generation_lease_seconds: int = 300

    # Chunking, measured in tokens of the embedding model.
    chunk_size_tokens: int = 800
    chunk_overlap_tokens: int = 100

    # Semantic search limits.
    search_max_top_k: int = 20
    # Results less similar than this are left out, so an unrelated query returns nothing
    # instead of the least-bad passages. 0 disables the cut-off.
    search_min_similarity: float = 0.2

    # --- Security and cost controls (docs/security-cost-controls-architecture.md) ---------
    # Every limit is a default for local development; set them per deployment.
    rate_limits_enabled: bool = True
    # Requests per user (and, for chat, per workspace) in a sliding window. /chat and
    # /chat/stream share the chat limits.
    rate_limit_chat_per_minute: int = Field(default=10, ge=1)
    rate_limit_chat_per_workspace_per_minute: int = Field(default=60, ge=1)
    rate_limit_search_per_minute: int = Field(default=30, ge=1)
    rate_limit_upload_per_hour: int = Field(default=10, ge=1)

    # Answers being written at once, per user (across conversations), and AI operations of
    # any kind at once per workspace.
    max_concurrent_generations_per_user: int = Field(default=3, ge=1)
    max_concurrent_ai_operations_per_workspace: int = Field(default=10, ge=1)
    # Documents waiting for or undergoing ingestion at once per workspace. Uploads beyond
    # this are refused until some finish.
    max_ingestion_jobs_per_workspace: int = Field(default=5, ge=1)

    # Quotas. Tokens are input plus output, of every AI operation. Days and months are UTC.
    user_daily_token_limit: int = Field(default=500_000, ge=1)
    user_daily_chat_limit: int = Field(default=200, ge=1)
    workspace_monthly_token_limit: int = Field(default=5_000_000, ge=1)
    # In the pricing table's currency. Checked against estimated cost, not an invoice.
    workspace_monthly_cost_limit: Decimal = Field(default=Decimal("10.00"), ge=0)
    # Deployment-wide AI budget in USD (estimated from the price table), across every user and
    # workspace. Unset: no deployment-wide cap. Set them for a public demo, where anyone can
    # sign up and create workspaces (docs/phase-5.8a-free-portfolio-deployment.md).
    global_daily_cost_limit: Decimal | None = Field(default=None, ge=0)
    global_monthly_cost_limit: Decimal | None = Field(default=None, ge=0)
    # Lifetime limits per account, for a public demo: explicit searches and chat questions a
    # user may ever make. They never reset. Unset: no lifetime limit.
    user_lifetime_search_limit: int | None = Field(default=None, ge=0)
    user_lifetime_chat_limit: int | None = Field(default=None, ge=0)
    # Comma-separated user IDs not subject to the lifetime limits (for example the demo's
    # owner). User IDs are not secret.
    lifetime_limit_exempt_user_ids: str = ""

    # The most input a single answer may send to the model, as estimated before the call.
    chat_max_input_tokens: int = Field(default=12_000, ge=1000)

    # A reservation not finalised within this time is treated as abandoned (for example
    # after a crash): it stops counting as running, and its estimate stays charged.
    usage_reservation_ttl_seconds: int = Field(default=600, ge=60)
    ingestion_reservation_ttl_seconds: int = Field(default=3600, ge=60)
    # A question's embedding takes well under a second; its reservation lasts this long.
    query_reservation_ttl_seconds: int = Field(default=120, ge=30)
    # How often running streams and ingestions refresh their reservations, and how often
    # each API process sweeps for abandoned ones.
    usage_heartbeat_seconds: int = Field(default=30, ge=5)
    usage_sweep_interval_seconds: int = Field(default=60, ge=5)
    # Record an alert when this share of a monthly or daily limit is used (and at 100%).
    usage_alert_threshold_percent: int = Field(default=80, ge=1, le=100)

    # Durable ingestion jobs (app/services/jobs.py).
    ingestion_workers_per_workspace: int = Field(default=2, ge=1)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=20)
    # A running job not heard from for this long is taken back and retried.
    ingestion_job_lease_seconds: int = Field(default=600, ge=30)
    ingestion_retry_delay_seconds: int = Field(default=30, ge=0)
    ingestion_poll_seconds: float = Field(default=5.0, gt=0)
    # On shutdown, how long running jobs may finish before they are handed back to the queue.
    ingestion_shutdown_grace_seconds: float = Field(default=5.0, ge=0)

    # Replaces the default price table (app/services/pricing.py) without a code change.
    # JSON: {"version": "...", "currency": "USD", "models": {"<model prefix>":
    # {"input_per_million": "0.40", "output_per_million": "1.60"}}}.
    usage_pricing_json: str = ""

    @field_validator("chat_reasoning_effort", mode="before")
    @classmethod
    def valid_effort(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if value not in REASONING_EFFORTS:
            raise ValueError(f"CHAT_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}")
        return value

    @field_validator(
        "global_daily_cost_limit",
        "global_monthly_cost_limit",
        "user_lifetime_search_limit",
        "user_lifetime_chat_limit",
        mode="before",
    )
    @classmethod
    def blank_limit_means_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("lifetime_limit_exempt_user_ids")
    @classmethod
    def valid_exempt_users(cls, value: str) -> str:
        for raw in value.split(","):
            if raw.strip():
                try:
                    uuid.UUID(raw.strip())
                except ValueError as error:
                    raise ValueError(
                        "LIFETIME_LIMIT_EXEMPT_USER_IDS must be comma-separated user IDs (UUIDs)"
                    ) from error
        return value

    @field_validator("api_docs_enabled", mode="before")
    @classmethod
    def blank_means_unset(cls, value: object) -> object:
        # Compose and .env files pass an unset variable as "": that means "use the default".
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def check_limits(self) -> "Settings":
        # A lease must outlive the longest answer, and a reservation the lease, or a slow
        # answer would be counted as abandoned while it is still being written.
        if self.usage_reservation_ttl_seconds <= self.chat_generation_lease_seconds:
            raise ValueError("USAGE_RESERVATION_TTL_SECONDS must exceed the generation lease")
        if self.usage_heartbeat_seconds * 2 > min(
            self.usage_reservation_ttl_seconds, self.ingestion_reservation_ttl_seconds
        ):
            raise ValueError(
                "USAGE_HEARTBEAT_SECONDS must be at most half of every reservation TTL"
            )
        if self.chat_max_output_tokens < 1 or self.chat_max_output_tokens > 32_768:
            raise ValueError("CHAT_MAX_OUTPUT_TOKENS must be between 1 and 32768")
        return self

    @property
    def docs_enabled(self) -> bool:
        if self.api_docs_enabled is not None:
            return self.api_docs_enabled
        return self.environment.lower() != "production"

    @property
    def jwt_issuer(self) -> str:
        if self.supabase_issuer:
            return self.supabase_issuer
        return f"{self.supabase_url.rstrip('/')}/auth/v1" if self.supabase_url else ""

    @property
    def jwks_url(self) -> str:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        return f"{self.jwt_issuer}/.well-known/jwks.json" if self.jwt_issuer else ""

    @property
    def lifetime_limit_exempt_users(self) -> frozenset[uuid.UUID]:
        exempt = set()
        for raw in self.lifetime_limit_exempt_user_ids.split(","):
            if raw.strip():
                exempt.add(uuid.UUID(raw.strip()))
        return frozenset(exempt)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
