from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    # The application connects as a NON-OWNER role so row level security applies.
    # Migrations use the owner role instead; see MIGRATION_DATABASE_URL.
    database_url: str = "postgresql+asyncpg://clinic_app:devpass@127.0.0.1:55432/clinic"
    migration_database_url: str = "postgresql+psycopg://postgres@127.0.0.1:55432/clinic"

    # Model selection is a deployment decision, not a code change.
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    stt_provider: str = "openai"
    tts_provider: str = "openai"

    # Fernet key protecting OAuth refresh tokens at rest.
    token_encryption_key: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"
    oauth_redirect_base: str = "http://localhost:8000"

    default_clinic_slug: str = "darren-dental"
    hold_ttl_seconds: int = 300
    freebusy_cache_seconds: int = 60

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
