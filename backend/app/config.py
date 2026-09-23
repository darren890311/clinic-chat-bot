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

    # Model selection is a deployment decision, not a code change. Switching
    # LLM_PROVIDER changes the vendor; the tools, prompt and guardrails are
    # identical either way.
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-5"
    openai_model: str = "gpt-5"
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # How many tool rounds one turn may take before the agent gives up and
    # hands over. A booking needs three or four; a loop needs stopping.
    agent_max_tool_rounds: int = 8

    # Recogniser and voice swap independently: a practice might want a cheap
    # transcriber and a good-sounding voice, or the reverse.
    stt_provider: str = "openai"
    tts_provider: str = "openai"
    stt_model: str = "gpt-4o-transcribe"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"

    # Fernet key protecting OAuth refresh tokens at rest.
    token_encryption_key: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"
    oauth_redirect_base: str = "http://localhost:8000"

    # Shared secret guarding the calendar-connection endpoints. There is no
    # staff login yet, and an unauthenticated endpoint that starts an OAuth flow
    # is an invitation to attach someone else's calendar to this clinic.
    admin_token: str = ""

    default_clinic_slug: str = "darren-dental"
    hold_ttl_seconds: int = 300
    freebusy_cache_seconds: int = 60

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


@lru_cache
def get_settings() -> Settings:
    return Settings()
