"""Application settings, loaded from the environment.

Required settings have no default, so a missing value fails loudly at startup
rather than silently degrading. No silent fallbacks.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A known, published value, so it can never be mistaken for a secret somebody chose.
# The validator below refuses to let a prod deployment start while still using it.
DEV_JWT_SECRET = "dev-only-insecure-signing-key-do-not-deploy"


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
    llm_tier3_model: str = Field("", description="Third tier model id, e.g. llama3.2:3b")
    llm_tier3_timeout_seconds: float = Field(
        300.0, gt=0, description="Read timeout for the third tier, in seconds"
    )

    # ------------------------------------------------------------------ auth
    #
    # ``local`` issues and verifies its own tokens against a password, and needs no
    # second service — which is the point: an identity provider is another container,
    # roughly a gigabyte, and no deployment has asked for SSO yet. ``oidc`` verifies
    # tokens somebody else issued; the seam exists so that becomes a swap rather than
    # a rewrite. See app/auth/tokens.py.
    auth_provider: Literal["local", "oidc"] = Field(
        "local", description="local | oidc — who issues and verifies access tokens"
    )
    jwt_secret: str = Field(
        DEV_JWT_SECRET,
        description="HS256 signing key for locally-issued tokens. Must be changed in prod.",
    )
    jwt_issuer: str = Field("opsmind", description="`iss` claim on locally-issued tokens")
    jwt_ttl_minutes: int = Field(
        720, gt=0, description="How long a locally-issued access token stays valid"
    )

    # Only read when auth_provider is oidc.
    oidc_issuer: str = Field("", description="Expected `iss`, e.g. https://sso.example/realms/x")
    oidc_audience: str = Field("", description="Expected `aud` claim")
    oidc_jwks_url: str = Field("", description="Where the issuer publishes its signing keys")

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

    @model_validator(mode="after")
    def _auth_is_deployable(self) -> "Settings":
        """Refuse the two auth configurations that are only ever mistakes.

        Shipping the development signing key is the serious one. Anyone holding it can
        mint a token for any user id, which is every account on the deployment — and
        nothing about the running service would look wrong. It is a published constant
        precisely so this check can be exact rather than a guess at entropy.

        A local secret is pointless under ``oidc`` and vice versa, so each provider is
        checked only for what it actually reads.
        """
        if self.auth_provider == "local":
            if self.app_env == "prod" and self.jwt_secret == DEV_JWT_SECRET:
                raise ValueError(
                    "JWT_SECRET is still the development key. Set it to a long random "
                    "value before deploying — anyone with this key can sign in as anyone."
                )
            return self

        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER", self.oidc_issuer),
                ("OIDC_JWKS_URL", self.oidc_jwks_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"AUTH_PROVIDER=oidc requires {' and '.join(missing)}. "
                "Without them any token bearing the right shape would be trusted."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
