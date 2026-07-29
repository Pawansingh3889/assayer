"""The factory assembles the failover chain from settings, in the right order.

Settings are supplied directly, bypassing the real environment, so no network or API key
is touched. Every tier is an OpenAI-compatible client; the factory only decides how many
there are and in what order.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm import factory
from app.llm.base import LLMError
from app.llm.failover import FailoverLLM
from app.llm.openai_compat import OpenAICompatibleLLMClient


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"database_url": "postgresql+asyncpg://user:pass@localhost/db"}
    values.update(overrides)
    return Settings(**values)


_CEREBRAS = {
    "llm_tier1_enabled": True,
    "llm_tier1_base_url": "https://api.cerebras.ai/v1",
    "llm_tier1_model": "llama-3.3-70b",
}
_GROQ = {
    "llm_tier2_enabled": True,
    "llm_tier2_base_url": "https://api.groq.com/openai/v1",
    "llm_tier2_model": "llama-3.3-70b-versatile",
}
_OPENROUTER = {
    "llm_tier3_enabled": True,
    "llm_tier3_base_url": "https://openrouter.ai/api/v1",
    "llm_tier3_model": "openrouter/free",
}


def test_two_tiers_chain_in_configured_order(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(**_CEREBRAS, **_GROQ))

    llm = factory.get_llm()

    assert isinstance(llm, FailoverLLM)
    assert [type(c) for c in llm._clients] == [
        OpenAICompatibleLLMClient,
        OpenAICompatibleLLMClient,
    ]
    assert llm._clients[0]._base_url == "https://api.cerebras.ai/v1"  # Cerebras leads
    assert llm._clients[1]._base_url == "https://api.groq.com/openai/v1"  # Groq second


def test_three_tiers_chain_in_configured_order(monkeypatch):
    monkeypatch.setattr(
        factory, "get_settings", lambda: _settings(**_CEREBRAS, **_GROQ, **_OPENROUTER)
    )

    llm = factory.get_llm()

    assert isinstance(llm, FailoverLLM)
    assert [c._base_url for c in llm._clients] == [
        "https://api.cerebras.ai/v1",
        "https://api.groq.com/openai/v1",
        "https://openrouter.ai/api/v1",  # third tier, tried only if the first two fail
    ]


def test_a_single_configured_tier_is_used_bare(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(**_CEREBRAS))

    llm = factory.get_llm()

    assert isinstance(llm, OpenAICompatibleLLMClient)


def test_an_enabled_but_unconfigured_tier_never_reaches_the_factory():
    """This used to be caught one layer down, by the client constructor raising
    LLMError — and the HTTP layer renders any LLMError as a calm 503, so a typo in
    .env was indistinguishable from a provider outage, on every request.

    Settings refuses it now, so the failure arrives at startup naming the field.
    """
    with pytest.raises(ValidationError, match="LLM_TIER1_BASE_URL"):
        _settings(llm_tier1_enabled=True)


def test_the_client_still_guards_its_own_inputs():
    """Defence in depth. Settings is the boundary that matters, but the client must not
    assume settings was the only way it could have been constructed."""
    with pytest.raises(LLMError):
        OpenAICompatibleLLMClient(base_url="", api_key="", model="")


def test_no_tiers_configured_raises_error(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings())

    with pytest.raises(RuntimeError, match="No LLM tier is configured"):
        factory.get_llm()
