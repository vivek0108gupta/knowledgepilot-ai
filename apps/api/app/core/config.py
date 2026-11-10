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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
