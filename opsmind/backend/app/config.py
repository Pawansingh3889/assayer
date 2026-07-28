"""Application settings, loaded from the environment.

Required settings have no default, so a missing value fails loudly at startup
rather than silently degrading. No silent fallbacks.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        ..., description="Async SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host/db"
    )

    # LLM tiers form an ordered failover chain: tier 1 is tried first, then tier 2,
    # then tier 3. Each is any OpenAI-compatible endpoint (Cerebras, Groq, OpenRouter,
    # a self-hosted server…). base_url/model are required when a tier is enabled;
    # api_key may be blank for keyless local servers.
    llm_tier1_enabled: bool = Field(False, description="Enable the first LLM tier")
    llm_tier1_base_url: str = Field(
        "", description="First tier base URL, e.g. http://localhost:11434/v1"
    )
    llm_tier1_api_key: str = Field("", description="First tier API key (blank if not required)")
    llm_tier1_model: str = Field("", description="First tier model id, e.g. llama-3.3-70b")
    llm_tier1_timeout_seconds: float = Field(
        300.0, gt=0, description="Read timeout for the first tier, in seconds"
    )

    llm_tier2_enabled: bool = Field(False, description="Enable the second LLM tier")
    llm_tier2_base_url: str = Field(
        "", description="Second tier base URL, e.g. https://api.cerebras.ai/v1"
    )
    llm_tier2_api_key: str = Field("", description="Second tier API key (blank if not required)")
    llm_tier2_model: str = Field("", description="Second tier model id, e.g. llama-3.3-70b")
    llm_tier2_timeout_seconds: float = Field(
        300.0, gt=0, description="Read timeout for the second tier, in seconds"
    )

    llm_tier3_enabled: bool = Field(False, description="Enable the third LLM tier")
    llm_tier3_base_url: str = Field(
        "", description="Third tier base URL, e.g. https://api.groq.com/openai/v1"
    )
    llm_tier3_api_key: str = Field("", description="Third tier API key (blank if not required)")
    llm_tier3_model: str = Field(
        "", description="Third tier model id, e.g. llama-3.3-70b-versatile"
    )
    llm_tier3_timeout_seconds: float = Field(
        300.0, gt=0, description="Read timeout for the third tier, in seconds"
    )

    app_env: str = Field("dev", description="dev | prod")
    frontend_origin: str = Field(
        "http://localhost:3000", description="Allowed CORS origin for the browser app"
    )
    log_level: str = Field(
        "INFO",
        description="Level for the app.* loggers. INFO keeps the per-call token-usage "
        "records; raise to WARNING to quieten them.",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
