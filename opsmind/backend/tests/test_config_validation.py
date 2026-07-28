"""Configuration is checked at startup, not on a participant's first message.

Two separate guards, for two different mistakes:

- a tier switched on but left half-filled — always invalid, refused by `Settings`
- nothing configured at all — fatal in prod, a warning anywhere else
"""

import logging

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import check_llm_configuration

DB = "postgresql+asyncpg://opsmind:opsmind@localhost:5432/opsmind"


def _settings(**overrides) -> Settings:
    # _env_file=None so a developer's real .env cannot leak into the assertion.
    return Settings(database_url=DB, _env_file=None, **overrides)


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
    _deploy(monkeypatch, app_env="prod")

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
        llm_tier1_enabled=True,
        llm_tier1_base_url="http://127.0.0.1:9/v1",
        llm_tier1_model="llama-3.3-70b",
    )

    check_llm_configuration()
