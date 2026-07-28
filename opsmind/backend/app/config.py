"""Application settings, loaded from the environment.

Required settings have no default, so a missing value fails loudly at startup
rather than silently degrading. No silent fallbacks.
"""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        ..., description="Async SQLAlchemy URL, e.g. postgresql+asyncpg://user:pass@host/db"
    )

    # LLM tiers form an ordered failover chain: tier 1 is tried first, then tier 2,
    # then tier 3. Each is any OpenAI-compatible endpoint (Cerebras, Groq, OpenRouter,
    # a self-hosted server…). base_url/model are required when a tier is enabled —
    # enforced by the validator below — while api_key may be blank for keyless local
    # servers.
    #
    # The shipped default is cloud-first with a local backstop: tier 1 Cerebras,
    # tier 2 Groq, tier 3 Ollama. The examples follow that order.
    llm_tier1_enabled: bool = Field(False, description="Enable the first LLM tier")
    llm_tier1_base_url: str = Field(
        "", description="First tier base URL, e.g. https://api.cerebras.ai/v1"
    )
    llm_tier1_api_key: str = Field("", description="First tier API key (blank if not required)")
    llm_tier1_model: str = Field("", description="First tier model id, e.g. llama-3.3-70b")
    llm_tier1_timeout_seconds: float = Field(
        300.0, gt=0, description="Read timeout for the first tier, in seconds"
    )

    llm_tier2_enabled: bool = Field(False, description="Enable the second LLM tier")
    llm_tier2_base_url: str = Field(
        "", description="Second tier base URL, e.g. https://api.groq.com/openai/v1"
    )
    llm_tier2_api_key: str = Field("", description="Second tier API key (blank if not required)")
    llm_tier2_model: str = Field("", description="Second tier model id, e.g. llama-3.3-70b")
    llm_tier2_timeout_seconds: float = Field(
        300.0, gt=0, description="Read timeout for the second tier, in seconds"
    )

    llm_tier3_enabled: bool = Field(False, description="Enable the third LLM tier")
    llm_tier3_base_url: str = Field(
        "", description="Third tier base URL, e.g. http://ollama:11434/v1"
    )
    llm_tier3_api_key: str = Field("", description="Third tier API key (blank if not required)")
    llm_tier3_model: str = Field("", description="Third tier model id, e.g. llama3.1:8b")
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

    @model_validator(mode="after")
    def _enabled_tiers_are_fully_configured(self) -> "Settings":
        """An enabled tier missing base_url or model is never valid, so refuse it here.

        The client constructor already rejected it, but by then it was far too late to
        be useful: ``get_llm`` is built lazily, so the first participant message of the
        deployment constructed the chain, raised ``LLMError``, and was handled as a
        calm 503 — "the assistant is briefly unavailable". A typo in ``.env`` therefore
        looked exactly like a provider outage, on every request, forever.

        Failing here turns that into a startup error naming the tier and the field.
        """
        for tier in (1, 2, 3):
            if not getattr(self, f"llm_tier{tier}_enabled"):
                continue
            missing = [
                f"LLM_TIER{tier}_{field.upper()}"
                for field in ("base_url", "model")
                if not getattr(self, f"llm_tier{tier}_{field}")
            ]
            if missing:
                raise ValueError(
                    f"LLM tier {tier} is enabled but {' and '.join(missing)} "
                    f"{'are' if len(missing) > 1 else 'is'} not set."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
