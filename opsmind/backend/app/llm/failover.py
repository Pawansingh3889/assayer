"""Provider failover: try each LLM in an ordered chain until one answers.

The chain runs in configured order — tier 1, then tier 2, then tier 3. The shipped
default is cloud-first with a local backstop: Cerebras, then Groq, then a local Ollama
that is never rate-limited and never out of credit. Every tier is an OpenAI-compatible
client;
"tier 2" means the next one tried, not a lesser class of provider. A tier is reached only
when every tier before it raises an ``LLMError`` (transport down, rate limited, credit
exhausted, malformed tool call). If every tier fails, the last error propagates: the
system still fails loudly, never silently degrading.

Rotating across tiers on failure is the whole redundancy story — there is no separate
retry or key-rotation layer beneath it.

``FailoverLLM`` itself satisfies ``LLMProtocol``, so callers can't tell a chain from a
single client, and ``factory.get_llm`` returns a bare client when only one tier is set.
"""

import logging
from typing import Any

from app.llm.base import LLMError, LLMProtocol, ToolTurn

logger = logging.getLogger("app.llm.failover")


class FailoverLLM:
    """Chain two or more ``LLMProtocol`` clients as primary then backups, tried in order."""

    def __init__(self, *clients: LLMProtocol) -> None:
        if not clients:
            raise ValueError("FailoverLLM needs at least one client")
        self._clients = clients

    async def tool_call(
        self,
        *,
        system: str,
        prompt: str,
        tool_name: str,
        tool_description: str,
        input_schema: dict[str, Any],
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        errors: list[LLMError] = []
        for tier, client in enumerate(self._clients, start=1):
            try:
                return await client.tool_call(
                    system=system,
                    prompt=prompt,
                    tool_name=tool_name,
                    tool_description=tool_description,
                    input_schema=input_schema,
                    max_tokens=max_tokens,
                )
            except LLMError as exc:
                self._note(tier, "tool_call", exc)
                errors.append(exc)
        raise errors[-1]

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolTurn:
        errors: list[LLMError] = []
        for tier, client in enumerate(self._clients, start=1):
            try:
                return await client.tool_turn(
                    system=system, messages=messages, tools=tools, max_tokens=max_tokens
                )
            except LLMError as exc:
                self._note(tier, "tool_turn", exc)
                errors.append(exc)
        raise errors[-1]

    def _note(self, tier: int, op: str, exc: LLMError) -> None:
        count = len(self._clients)
        if tier < count:
            logger.warning("LLM tier %d/%d failed on %s, trying next: %s", tier, count, op, exc)
        else:
            logger.warning("LLM tier %d/%d (last) failed on %s: %s", tier, count, op, exc)
