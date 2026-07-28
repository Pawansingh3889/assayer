"""A backup LLM client speaking the OpenAI Chat Completions API.

The primary client (``app.llm.client.LLMClient``) talks to Anthropic. This one talks to
any OpenAI-compatible server — vLLM, NVIDIA NIM, OpenRouter, Ollama, and similar — so a
model such as Nemotron/Hermes can stand in when the primary is unavailable.

It implements the same ``LLMProtocol`` surface and returns the same validated shapes, so
the conduct engine and generation service cannot tell which provider answered. As with the
primary, SDK/transport failures become one typed ``LLMError`` and a malformed or missing
tool call fails loudly rather than degrading.

A knowing deviation, recorded here so it is a decision rather than a discovery:
The rule is never to regex or parse structured data out of prose, and
``_salvage_from_content`` below does precisely that. It is confined to this module — the
Anthropic path never parses prose — and exists because the models these tiers reach are
free or locally served, and routinely write the tool call into the message text instead of
into ``tool_calls``. With no Anthropic key configured, every turn is served by a backup
tier, so refusing to salvage would mean refusing to run at all. The mitigation is that
salvage only ever *proposes* a tool call: the payload is validated against the same schema
as any other, and the engine rejects it identically if it does not fit.
"""

import json
import logging
import re
from collections.abc import Iterator
from typing import Any, cast

import httpx

from app.llm.base import LLMError, NoToolCallError, ToolTurn

logger = logging.getLogger("app.llm.openai_compat")


def _balanced_objects(text: str) -> Iterator[str]:
    """Every balanced top-level {...} in the text, in order.

    Brace-counting with string awareness — enough to lift JSON objects out of
    surrounding prose without a full parser.

    Two details earn their keep. The scan starts at the beginning of the text rather
    than at the first ``{``: skipping ahead means a brace inside an earlier quoted
    string ("the format is \"{name}\"") is mistaken for the start of an object, and the
    real call after it is never seen. And every object is yielded, not just the first,
    because models routinely emit something else first — a thinking object, an example —
    and the caller has no way to know which one is the tool call until it tries.
    """
    depth, start, in_string, escaped = 0, None, False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start : index + 1]
                start = None


# A local CPU-served model (the very case the backup exists for) can legitimately take
# well over a minute on a cold load or a long survey, so the read timeout is generous
# and configurable (LLM_BACKUP_TIMEOUT_SECONDS). Connecting, by contrast, should be
# near-instant — a short connect timeout keeps a *genuinely* unreachable endpoint from
# stalling a request for the full read window.
DEFAULT_TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 10.0


class OpenAICompatibleLLMClient:
    """Force one schema-constrained tool call out of an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url or not model:
            raise LLMError("Backup LLM is enabled but base_url/model are not configured.")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = httpx.Timeout(timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
        self._transport = transport  # injectable so tests need no network

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            # str(ReadTimeout) is usually empty, which once surfaced as a blank
            # "could not reach" while the model was merely slow — name the failure.
            logger.error("backup llm timed out: %r", exc)
            raise LLMError(
                f"Backup LLM timed out after {self._timeout.read}s — the model may be "
                "loading or too slow for the configured LLM_BACKUP_TIMEOUT_SECONDS."
            ) from exc
        except httpx.HTTPError as exc:
            # repr, not str: several httpx errors stringify to "".
            logger.error("backup llm call failed: %r", exc)
            raise LLMError(f"Could not reach the backup LLM: {exc!r}") from exc
        if response.status_code >= 400:
            logger.error("backup llm returned %s: %s", response.status_code, response.text[:200])
            raise LLMError(
                f"Backup LLM rejected the request ({response.status_code}): {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"Backup LLM returned a non-JSON body: {response.text[:200]}") from exc
        if not isinstance(body, dict):
            raise LLMError(f"Backup LLM returned {type(body).__name__}, expected a JSON object.")
        # Logged after parsing so the token usage is in reach — a backup tier can serve any
        # live turn, and until now those tokens were spent with no record at all. Format
        # matches the primary's so one grep finds every tier's spend.
        #
        # `.get` is not a no-fallbacks shrug here: usage is optional provider metadata that
        # is recorded and never acted on, unlike required data the no-fallbacks rule
        # is about. A tier that omits it logs usage=None, which is the honest answer.
        logger.info(
            "llm backup call model=%s status=%s usage=%s",
            self._model,
            response.status_code,
            body.get("usage"),
        )
        return cast("dict[str, Any]", body)

    @staticmethod
    def _salvage_from_content(said: Any) -> list[dict[str, Any]]:
        """Local models often write the tool call INTO the text instead of tool_calls.

        Recover a tool-call-shaped JSON object ({"name": ..., "arguments"/"parameters":
        {...}}) from the content: the whole text, a fenced ``` block, or any JSON object
        embedded in prose ("Sure! {...}"). The first candidate with that exact shape
        wins; anything else stays a hard failure.
        """
        if not isinstance(said, str):
            return []
        text = said.strip()

        candidates = [text]
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
        if fence:
            candidates.append(fence.group(1))
        candidates.extend(_balanced_objects(text))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict) or not isinstance(parsed.get("name"), str):
                continue
            arguments = parsed.get("arguments", parsed.get("parameters", {}))
            if not isinstance(arguments, dict):
                continue
            return [{"function": {"name": parsed["name"], "arguments": json.dumps(arguments)}}]
        return []

    @staticmethod
    def _first_tool_call(data: dict[str, Any], max_tokens: int | None = None) -> tuple[str, dict[str, Any], str]:
        """Pull (tool name, parsed arguments, spoken text) from the first choice.

        Every hop is shape-checked. These endpoints are third-party and occasionally
        answer with something that is JSON but not the Chat Completions shape; walking
        it optimistically raised AttributeError/KeyError, which is not an ``LLMError``
        and so aborted the whole failover chain instead of moving to the next provider.
        """
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise LLMError("Backup LLM returned no choices.")
        if not isinstance(choices[0], dict):
            raise LLMError("Backup LLM returned a malformed choice.")
        message = choices[0].get("message") or {}
        if not isinstance(message, dict):
            raise LLMError("Backup LLM returned a malformed message.")
        said = message.get("content")
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise LLMError("Backup LLM returned a malformed tool_calls field.")
        if not tool_calls:
            tool_calls = OpenAICompatibleLLMClient._salvage_from_content(said)
            if tool_calls:
                said = ""  # the content WAS the tool call; there is nothing spoken
        if not tool_calls:
            finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length" and max_tokens is not None:
                raise TruncatedTurnError(
                    f"Model hit the {max_tokens}-token limit before completing its tool call."
                )
            raise NoToolCallError("Backup LLM returned no tool call.")
        if not isinstance(tool_calls[0], dict):
            raise LLMError("Backup LLM returned a malformed tool call.")
        function = tool_calls[0].get("function") or {}
        if not isinstance(function, dict):
            raise LLMError("Backup LLM tool call has a malformed function field.")
        name = function.get("name")
        if not isinstance(name, str):
            raise LLMError("Backup LLM tool call is missing a name.")
        raw_arguments = function.get("arguments", "{}")
        try:
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except json.JSONDecodeError as exc:
            raise LLMError(f"Backup LLM tool arguments were not valid JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise LLMError("Backup LLM tool arguments were not a JSON object.")
        return name, cast("dict[str, Any]", arguments), said if isinstance(said, str) else ""

    @staticmethod
    def _as_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate Anthropic-style tools (name/description/input_schema) to OpenAI's shape."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                },
            }
            for tool in tools
        ]

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
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "tools": self._as_openai_tools(
                [{"name": tool_name, "description": tool_description, "input_schema": input_schema}]
            ),
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        }
        data = await self._post(payload)
        name, arguments, _ = self._first_tool_call(data, max_tokens)
        if name != tool_name:
            raise LLMError(f"Backup LLM called {name!r}, expected {tool_name!r}.")
        return arguments

    async def tool_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolTurn:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": self._as_openai_tools(tools),
            "tool_choice": "required",  # force exactly one tool; the model picks which
        }
        data = await self._post(payload)
        name, arguments, said = self._first_tool_call(data, max_tokens)
        return ToolTurn(text=said.strip(), tool_name=name, tool_input=arguments)
