"""Build the LLM client the app should use, from settings.

Assembles an ordered failover chain: each enabled tier in order (1, 2, 3).
Resolution:

- Two or more tiers configured -> a ``FailoverLLM`` chaining them in that order.
- Exactly one tier configured   -> that client alone.
- Nothing configured            -> an explicit error (no silent fallback).

The Anthropic client has been removed; all tiers are OpenAI-compatible.
"""

from app.config import get_settings
from app.llm.base import LLMProtocol
from app.llm.failover import FailoverLLM
from app.llm.openai_compat import OpenAICompatibleLLMClient


def get_llm() -> LLMProtocol:
    settings = get_settings()

    chain: list[LLMProtocol] = []
    for tier in (1, 2, 3):
        enabled = getattr(settings, f"llm_tier{tier}_enabled")
        if not enabled:
            continue
        base_url = getattr(settings, f"llm_tier{tier}_base_url")
        api_key = getattr(settings, f"llm_tier{tier}_api_key")
        model = getattr(settings, f"llm_tier{tier}_model")
        timeout = getattr(settings, f"llm_tier{tier}_timeout_seconds")
        # A tier enabled without base_url/model is a misconfiguration; the client
        # constructor raises on it, which is what we want — loudly, at startup,
        # rather than on the first turn that happens to reach this tier.
        chain.append(
            OpenAICompatibleLLMClient(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout,
            )
        )

    if not chain:
        raise RuntimeError(
            "No LLM tier is configured. Set LLM_TIER1_ENABLED=true and provide "
            "LLM_TIER1_BASE_URL and LLM_TIER1_MODEL (or tier 2/3)."
        )
    if len(chain) == 1:
        return chain[0]
    return FailoverLLM(*chain)
