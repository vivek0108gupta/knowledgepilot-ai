from functools import lru_cache

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
