"""Configuration is checked at startup, not on a participant's first message.

Guards for the mistakes that are only ever mistakes:

- an LLM tier switched on but left half-filled — always invalid, refused by `Settings`
- no LLM tier at all — fatal in prod, a warning anywhere else
- the development signing key still in place in prod — refused outright
- `oidc` selected without an issuer to trust
"""

import logging

import pytest
from pydantic import ValidationError

from app.config import DEV_JWT_SECRET, Settings
from app.main import check_llm_configuration

DB = "postgresql+asyncpg://opsmind:opsmind@localhost:5432/opsmind"
REAL_SECRET = "a-real-deployment-signing-key-of-respectable-length"


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's real .env cannot leak into the assertion.
    return Settings(database_url=DB, _env_file=None, **overrides)


def _prod(**overrides) -> Settings:
    """A settings object that would be allowed to deploy.

    A prod deployment must carry its own signing key, so these tests supply one. That
    is not incidental scaffolding — it is the rule under test in `_the_dev_signing_key`
    below, and every other prod assertion depends on satisfying it first.
    """
    return _settings(app_env="prod", jwt_secret=REAL_SECRET, **overrides)


def test_a_fully_configured_tier_is_accepted():
    settings = _settings(
        llm_tier1_enabled=True,
        llm_tier1_base_url="https://api.cerebras.ai/v1",
        llm_tier1_model="llama-3.3-70b",
    )

    assert settings.llm_tier1_enabled is True


def test_configuring_no_tier_at_all_is_valid_settings():
    """Settings only checks the shape of what is switched on. Whether *any* tier exists
    is a deployment question, answered at startup by check_llm_configuration."""
    assert _settings().llm_tier1_enabled is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"llm_tier1_model": "llama-3.3-70b"}, "LLM_TIER1_BASE_URL"),
        ({"llm_tier1_base_url": "https://api.cerebras.ai/v1"}, "LLM_TIER1_MODEL"),
        ({}, "LLM_TIER1_BASE_URL and LLM_TIER1_MODEL"),
    ],
)
def test_an_enabled_tier_missing_its_fields_is_refused(overrides, expected):
    """This used to be an LLMError from the client constructor — which the HTTP layer
    renders as a calm 503, so a typo in .env was indistinguishable from a provider
    outage, on every request. Now it names the tier and the field, at startup."""
    with pytest.raises(ValidationError) as caught:
        _settings(llm_tier1_enabled=True, **overrides)

    assert expected in str(caught.value)


def test_a_disabled_tier_may_be_left_blank():
    """The shipped .env.example ships tier 2 switched off with fields half-filled.
    Validating a tier nobody will reach would refuse a perfectly good config."""
    assert _settings(llm_tier2_enabled=False, llm_tier2_base_url="").llm_tier2_enabled is False


def test_each_tier_is_checked_independently():
    with pytest.raises(ValidationError) as caught:
        _settings(
            llm_tier1_enabled=True,
            llm_tier1_base_url="https://api.cerebras.ai/v1",
            llm_tier1_model="llama-3.3-70b",
            llm_tier3_enabled=True,
            llm_tier3_base_url="http://ollama:11434/v1",
        )

    assert "LLM_TIER3_MODEL" in str(caught.value)


# ------------------------------------------------------ the startup check


def _deploy(monkeypatch, **overrides) -> None:
    """Pretend the process started with this configuration.

    Both names must be patched: `check_llm_configuration` reads settings from
    `app.main`, and `get_llm` reads them from `app.llm.factory`, because each module
    imported the function into its own namespace.
    """
    settings = _settings(**overrides)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.llm.factory.get_settings", lambda: settings)


def test_no_tier_configured_is_fatal_in_prod(monkeypatch):
    """The whole point: a deployment that can never answer a question must not boot
    looking healthy."""
    _deploy(monkeypatch, app_env="prod", jwt_secret=REAL_SECRET)

    with pytest.raises(RuntimeError, match="No LLM tier is configured"):
        check_llm_configuration()


def test_no_tier_configured_is_only_a_warning_outside_prod(monkeypatch, caplog):
    """The suite configures no tier on purpose — it fakes the model at the client
    boundary and must pass without a key. Raising here would turn every test red."""
    _deploy(monkeypatch, app_env="dev")

    with caplog.at_level(logging.WARNING, logger="app.main"):
        check_llm_configuration()

    assert "no usable LLM configuration" in caplog.text


def test_a_configured_tier_boots_without_touching_the_network(monkeypatch):
    """Constructing the chain must not call the provider. An unreachable Ollama is
    exactly what the failover chain exists to survive, so it cannot stop the service
    starting — this points tier 1 at an address nothing is listening on."""
    _deploy(
        monkeypatch,
        app_env="prod",
        jwt_secret=REAL_SECRET,
        llm_tier1_enabled=True,
        llm_tier1_base_url="http://127.0.0.1:9/v1",
        llm_tier1_model="llama-3.3-70b",
    )

    check_llm_configuration()


# ------------------------------------------------------------------ auth


def test_the_dev_signing_key_cannot_be_deployed():
    """The one that would matter most if it were missed. Anyone holding the published
    development key can mint a token for any user id — that is every account on the
    deployment — and nothing about the running service would look wrong."""
    with pytest.raises(ValidationError, match="JWT_SECRET is still the development key"):
        _settings(app_env="prod")


def test_the_dev_signing_key_is_fine_outside_prod():
    """It has to be. Every test in this suite signs tokens with it."""
    assert _settings(app_env="dev").jwt_secret == DEV_JWT_SECRET


def test_a_deployment_with_its_own_key_is_accepted():
    assert _prod().jwt_secret == REAL_SECRET


def test_oidc_without_an_issuer_is_refused():
    """Without an issuer and a key source there is nothing to check a token against,
    so any token of roughly the right shape would be trusted."""
    with pytest.raises(ValidationError, match="OIDC_ISSUER and OIDC_JWKS_URL"):
        _settings(auth_provider="oidc")


def test_oidc_does_not_require_a_local_signing_key():
    """Under oidc this service issues nothing, so the local key is irrelevant — and
    demanding one would imply a login endpoint that does not exist in that mode."""
    settings = _settings(
        app_env="prod",
        auth_provider="oidc",
        oidc_issuer="https://sso.example/realms/opsmind",
        oidc_jwks_url="https://sso.example/realms/opsmind/protocol/openid-connect/certs",
    )

    assert settings.jwt_secret == DEV_JWT_SECRET


def test_an_unknown_auth_provider_is_refused():
    with pytest.raises(ValidationError):
        _settings(auth_provider="ldap")
