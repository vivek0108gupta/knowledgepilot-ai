from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (and `.env` in local development)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "KnowledgePilot AI"
    environment: str = "development"
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
    embedding_model: str = "text-embedding-3-small"
    # Must equal the size of the `document_chunks.embedding` column (1536).
    embedding_dimensions: int = 1536
    # Chunks sent per embedding request, and how hard to try when a request fails.
    embedding_batch_size: int = 64
    embedding_max_attempts: int = 5
    embedding_timeout_seconds: float = 30.0

    # The chat model answers will be generated with. An identifier only: nothing calls it
    # yet, and no code names a model directly, so changing models is a setting change.
    openai_chat_model: str = "gpt-4.1-mini"

    # Answering questions from documents (retrieval-augmented generation).
    chat_temperature: float = 0.2
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
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
