"""Shared vocabulary for LLM providers.

This module contains the protocol, data classes, and error types that are
provider-agnostic. Concrete clients implement ``LLMProtocol`` and return the
defined types; today that is ``app.llm.openai_compat.OpenAICompatibleLLMClient``,
one instance per configured tier, chained by ``app.llm.failover.FailoverLLM``.

Engines and tests depend only on this module, not on provider-specific code —
which is what made removing the Anthropic client a change of imports rather than
a change of behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.errors import AppError


class LLMError(AppError):
    """Base error for LLM provider issues."""

    status_code = 502
    code = "llm_error"


class NoToolCallError(LLMError):
    """The model produced a turn with no tool call at all.

    Distinguished from other LLM failures because it is cheaply retryable: the model
    is responsive, it just chatted instead of acting. Timeouts and transport errors
    stay plain LLMError so a retry never doubles a 120-second wait.
    """

    code = "llm_no_tool_call"


class TruncatedTurnError(LLMError):
    """The turn hit max_tokens before the model chose a tool.

    Deliberately NOT a NoToolCallError: the engine retries those with a nudge, and a
    retry at the same token budget truncates in exactly the same place. Failing over to
    another provider (or raising) beats spending a turn to learn nothing.
    """

    code = "llm_truncated_turn"


@dataclass(frozen=True)
class ToolTurn:
    """One model turn: what it said, and the single tool it chose."""

    text: str
    tool_name: str
    tool_input: dict[str, Any]


@runtime_checkable
class LLMProtocol(Protocol):
    """The surface the engines depend on. Tests substitute a fake at this boundary."""

    async def tool_call(
        self,
        *,
        system: str,
        prompt: str,
        tool_name: str,
        tool_description: str,
        input_schema: dict[str, Any],
        max_tokens: int = ...,
    ) -> dict[str, Any]: ...

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        max_tokens: int = ...,
    ) -> ToolTurn: ...
