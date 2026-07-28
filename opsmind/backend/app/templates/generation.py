"""Natural-language template generation.

The creator describes a survey; the LLM drafts it through a schema-constrained tool
call. The engine validates the result and, on failure, retries once with the error
before failing loudly: validate, then act. A valid draft is persisted
so it lands in the same builder a hand-built one would.
"""

import logging
from typing import Any
from uuid import UUID

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMError, LLMProtocol
from app.llm.decoding import decode_stringified
from app.llm.factory import get_llm
from app.llm.prompts import load_prompt
from app.templates.models import SurveyTemplate
from app.templates.schemas import TemplateCreate, TemplateUpdate
from app.templates.service import TemplateService
from app.users.models import User

logger = logging.getLogger("app.templates.generation")

MAX_GENERATED_QUESTIONS = 20


class _DraftToolInput(TemplateCreate):
    """The generation tool's input — the template plus a short note. The note is a schema
    field, not free prose, because a forced tool call suppresses spoken text: asking for a
    sentence "alongside" the call reliably yields nothing, so it has to be part of the
    structured output. Validated back as a plain ``TemplateCreate`` (the note is ignored)."""

    note: str = Field(
        default="",
        description="One or two sentences for the creator: your main design choices, "
        "or what you changed.",
    )


_TOOL_NAME = "draft_survey_template"
_TOOL_DESCRIPTION = "Return a complete survey template as structured data, plus a short note."
_TOOL_SCHEMA: dict[str, Any] = _DraftToolInput.model_json_schema()
_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": _TOOL_DESCRIPTION,
    "input_schema": _TOOL_SCHEMA,
}


class GenerationService:
    def __init__(self, session: AsyncSession, llm: LLMProtocol | None = None) -> None:
        self.session = session
        self.llm: LLMProtocol = llm or get_llm()
        self.templates = TemplateService(session)

    async def generate_draft(self, prompt: str, creator: User) -> tuple[SurveyTemplate, str]:
        """Draft a new survey from a description. Returns the saved draft and the model's
        short note on what it built."""
        system = load_prompt("generate_template_v2")
        template_in, note = await self._draft(
            system, [{"role": "user", "content": prompt}], previous_error=None
        )
        template = await self.templates.create_draft(_without_catch_alls(template_in), creator)
        return template, note

    async def refine_draft(
        self, template_id: UUID, instruction: str, creator: User
    ) -> tuple[SurveyTemplate, str]:
        """Apply a follow-up instruction to an existing draft and return it revised, plus
        the model's note on what changed. The whole survey is re-drafted and re-validated,
        so a follow-up can never leave the draft in an invalid shape."""
        current = await self.templates.get_draft(template_id, creator)
        system = load_prompt("refine_template_v1")
        message = (
            f"{_describe(current)}\n\nRequested change: {instruction}\n\n"
            "Return the complete revised survey."
        )
        template_in, note = await self._draft(
            system, [{"role": "user", "content": message}], previous_error=None
        )
        updated = await self.templates.update_draft(
            template_id, _to_update(_without_catch_alls(template_in)), creator
        )
        return updated, note

    async def _draft(
        self, system: str, messages: list[dict[str, str]], previous_error: str | None
    ) -> tuple[TemplateCreate, str]:
        turn_messages = (
            messages
            if previous_error is None
            else [
                *messages,
                {
                    "role": "user",
                    "content": f"Your previous attempt was rejected: {previous_error}\n"
                    "Return a corrected survey.",
                },
            ]
        )
        turn = await self.llm.tool_turn(
            system=system, messages=turn_messages, tools=[_TOOL], max_tokens=4096
        )
        raw = _decode_stringified_fields(turn.tool_input)
        # Prefer the schema's note field; fall back to any spoken text a model does emit.
        note = str(raw.get("note") or turn.text or "").strip()
        error = _validation_error(raw)
        if error is None:
            return TemplateCreate.model_validate(raw), note
        if previous_error is None:
            logger.warning("generated template rejected, retrying: raw=%r error=%s", raw, error)
            return await self._draft(system, messages, previous_error=error)
        # The rejection reason describes the fault but not the draft, so keep the raw
        # payload before failing.
        logger.error("template generation failed after one retry: raw=%r error=%s", raw, error)
        raise LLMError(f"Model returned an invalid template after one retry: {error}")


def _decode_stringified_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Undo one JSON-encoding of the structured fields, a common small-model slip.

    Weaker backup models frequently emit ``"questions": "[{...}]"`` — the right list,
    wrapped in a string. That is a serialization artifact, not a content problem, so
    decode it (and the same slip on each question's ``options``) before validation.
    Anything that does not parse to the expected container type is left untouched for
    the validator to reject, so real junk still fails loudly.
    """

    def repaired_question(question: Any) -> Any:
        if not isinstance(question, dict):
            question = decode_stringified(question, dict)
        if not isinstance(question, dict) or "options" not in question:
            return question  # leave absent keys absent: the schema defaults
        # Options only mean anything on the select types. Small models decorate rating
        # questions with options like [1, 2, 3, 4, 5], which the schema (list[str])
        # rightly refuses — dropping them is lossless, so do that instead of burning
        # the retry.
        if question.get("answer_type") not in ("single_select", "multi_select"):
            return {**question, "options": []}
        return {**question, "options": decode_stringified(question["options"], list)}

    repaired = dict(raw)
    if "questions" in repaired:
        repaired["questions"] = decode_stringified(repaired["questions"], list)
    if isinstance(repaired.get("questions"), list):
        repaired["questions"] = [repaired_question(q) for q in repaired["questions"]]
    return repaired


_CATCH_ALL_OPTIONS = frozenset(
    {
        "other",
        "other (please specify)",
        "none of the above",
        "not applicable",
        "n/a",
        "prefer not to say",
    }
)


def _without_catch_alls(template_in: TemplateCreate) -> TemplateCreate:
    """Turn a literal "Other" option into the ``allow_other`` write-in it should have been.

    The prompt asks for this, but a catch-all silently discards the one part of an answer
    the creator could not anticipate, so it is worth guaranteeing rather than hoping for.
    A live run recorded ``{'option': 'Other'}`` and lost the participant's actual team.
    """
    for question in template_in.questions:
        kept = [o for o in question.options if o.strip().lower() not in _CATCH_ALL_OPTIONS]
        if len(kept) == len(question.options):
            continue
        if not kept:
            # Every option was a catch-all. Stripping them would leave a select with no
            # options — which the schema refuses on the way in, but assignment here runs
            # after validation and so is never re-checked. The invalid draft would reach
            # the builder, and the engine would offer the question with no enum at all,
            # quietly turning multiple choice into free text. Leave it for the creator.
            logger.info("kept catch-all options, nothing else to offer: %r", question.text)
            continue
        logger.info("replaced catch-all options with allow_other: %r", question.text)
        question.options = kept
        question.allow_other = True
    return template_in


def _to_update(template_in: TemplateCreate) -> TemplateUpdate:
    """Create and Update share a shape, but the service takes the Update type for edits."""
    return TemplateUpdate.model_validate(template_in.model_dump())


def _describe(template: SurveyTemplate) -> str:
    """Render the current draft as plain text for the model to revise."""
    lines = [
        f"Title: {template.title}",
        f"Description: {template.description or '(none)'}",
        "Questions:",
    ]
    for i, question in enumerate(sorted(template.questions, key=lambda q: q.position), start=1):
        parts = [f"{i}. [{question.answer_type.value}] {question.text}"]
        if question.options:
            parts.append(f"options: {', '.join(question.options)}")
        if question.allow_other:
            parts.append("allows a write-in")
        if not question.required:
            parts.append("optional")
        if question.allow_follow_ups:
            parts.append("follow-ups on")
        lines.append("  " + " · ".join(parts))
    return "\n".join(lines)


def _validation_error(raw: dict[str, Any]) -> str | None:
    try:
        template_in = TemplateCreate.model_validate(raw)
    except PydanticValidationError as exc:
        return str(exc)
    if not template_in.questions:
        # A weaker model (e.g. a free auto-routed one) can return a technically valid,
        # empty tool call — a title with no questions. Schema-valid, useless; burn the
        # retry rather than persist a survey with nothing to answer.
        return "no questions"
    if len(template_in.questions) > MAX_GENERATED_QUESTIONS:
        return f"too many questions (max {MAX_GENERATED_QUESTIONS})"
    return None
