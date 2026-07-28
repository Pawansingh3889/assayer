"""AI summary of a completed run.

The creator asks for it; the LLM returns it through a schema-constrained tool call; the
validated result is stored on ``survey_runs.summary`` so it is generated once. Two gates
sit between the model and the column: the schema, and a check that every quote is a
verbatim span of something the participant actually said. A summary is an creator-facing
artifact they will act on, and an invented quote is indistinguishable from a real one
once it is rendered next to the answers.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError
from app.llm.base import LLMError, LLMProtocol
from app.llm.decoding import decode_stringified
from app.llm.factory import get_llm
from app.llm.prompts import load_prompt
from app.runs.enums import AnswerKind, RunStatus
from app.runs.models import Answer, SurveyRun
from app.runs.repository import ResultsRepository
from app.runs.service import flatten_answer
from app.templates.repository import TemplateRepository
from app.users.models import User

logger = logging.getLogger("app.runs.summary")

MAX_KEY_FACTS = 8
MAX_QUOTES = 5
PROMPT_VERSION = "summarise_run_v1"


class Quote(BaseModel):
    question: str = Field(min_length=1)
    quote: str = Field(min_length=1)

    @field_validator("question", "quote")
    @classmethod
    def _has_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("cannot be blank")
        return text


class RunSummaryContent(BaseModel):
    """The stored shape. Only the headline is required: a run of three bare ratings has
    no facts worth listing and nothing worth quoting, and padding it would be invention."""

    headline: str = Field(min_length=1, max_length=300)
    key_facts: list[str] = Field(default_factory=list, max_length=MAX_KEY_FACTS)
    notable_quotes: list[Quote] = Field(default_factory=list, max_length=MAX_QUOTES)

    @field_validator("headline")
    @classmethod
    def _headline_has_content(cls, value: str) -> str:
        headline = value.strip()
        if not headline:
            raise ValueError("headline cannot be blank")
        return headline

    @field_validator("key_facts")
    @classmethod
    def _facts_are_readable(cls, values: list[str]) -> list[str]:
        facts: list[str] = []
        for value in values:
            fact = value.strip().lstrip("-•* ").strip()
            if not fact:
                raise ValueError("a key fact cannot be blank")
            facts.append(fact)
        return facts


_TOOL: dict[str, Any] = {
    "name": "summarise_run",
    "description": "Return a structured summary of one completed survey response.",
    "input_schema": RunSummaryContent.model_json_schema(),
}


class RunSummaryService:
    def __init__(self, session: AsyncSession, llm: LLMProtocol | None = None) -> None:
        self.session = session
        self._llm = llm
        self.repo = ResultsRepository(session)
        self.templates = TemplateRepository(session)

    @property
    def llm(self) -> LLMProtocol:
        """Built on first use, so reading a stored summary needs no model."""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    async def summarise(
        self, template_id: UUID, run_id: UUID, creator: User, refresh: bool = False
    ) -> RunSummaryContent:
        run = await self._run_for_creator(template_id, run_id, creator)
        if run.summary is not None and not refresh:
            # Already generated. Re-validated rather than trusted: the column is JSONB
            # and a summary written by an older prompt version may not match this shape.
            try:
                return RunSummaryContent.model_validate(run.summary)
            except PydanticValidationError:
                logger.warning("stored summary no longer validates, regenerating: run=%s", run.id)

        if run.status is not RunStatus.completed:
            raise ConflictError("Only a completed run can be summarised.")
        if not run.answers:
            raise ConflictError("This run has no answers to summarise.")

        content = await self._generate(run, previous_error=None)
        run.summary = {
            **content.model_dump(),
            "prompt_version": PROMPT_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        await self.session.commit()
        return content

    async def _generate(self, run: SurveyRun, previous_error: str | None) -> RunSummaryContent:
        messages = [{"role": "user", "content": _transcript_for(run)}]
        if previous_error is not None:
            messages.append(
                {
                    "role": "user",
                    "content": f"Your previous summary was rejected: {previous_error}\n"
                    "Return a corrected summary.",
                }
            )
        turn = await self.llm.tool_turn(
            system=load_prompt(PROMPT_VERSION), messages=messages, tools=[_TOOL], max_tokens=2048
        )
        raw = _decode_stringified_fields(turn.tool_input)
        # Fabricated quotes are dropped before validation, never after: mutating a
        # validated model skips Pydantic's checks and can break its own invariants.
        raw = _without_invented_quotes(raw, run)
        try:
            return RunSummaryContent.model_validate(raw)
        except PydanticValidationError as exc:
            error = str(exc)
        if previous_error is None:
            logger.warning("run summary rejected, retrying: run=%s error=%s", run.id, error)
            return await self._generate(run, previous_error=error)
        # The rejection names the fault but not the payload, so keep it before failing.
        logger.error(
            "run summary failed after one retry: run=%s raw=%r error=%s", run.id, raw, error
        )
        raise LLMError(f"Model returned an invalid summary after one retry: {error}")

    async def _run_for_creator(self, template_id: UUID, run_id: UUID, creator: User) -> SurveyRun:
        """Same ownership rule as the rest of results: someone else's survey reads as
        absent rather than forbidden."""
        template = await self.templates.get(template_id)
        if template is None or template.created_by != creator.id:
            raise NotFoundError("Template not found.")
        row = await self.repo.get_detail(run_id)
        if row is None:
            raise NotFoundError("Run not found.")
        run, version, _ = row
        if version.template_id != template_id:
            raise NotFoundError("That run belongs to a different template.")
        return cast("SurveyRun", run)  # unpacking a Row loses the element types


def _decode_stringified_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Undo one JSON-encoding of the list fields, the usual small-model slip."""
    decoded = dict(raw)
    for key in ("key_facts", "notable_quotes"):
        if key in decoded:
            decoded[key] = decode_stringified(decoded[key], list)
    if isinstance(decoded.get("notable_quotes"), list):
        decoded["notable_quotes"] = [decode_stringified(q, dict) for q in decoded["notable_quotes"]]
    return decoded


def _normalised(text: str) -> str:
    """Casefolded with runs of whitespace collapsed — models reflow quotes across lines
    and normalise spacing, which is not the same as inventing them."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _without_invented_quotes(raw: dict[str, Any], run: SurveyRun) -> dict[str, Any]:
    """Drop any quote that is not a verbatim span of a recorded answer.

    Dropped rather than rejected: the rest of the summary is usually sound, and
    ``notable_quotes`` has no floor, so losing one costs the creator nothing while
    keeping a fabricated one costs them a decision made on words nobody said.
    """
    quotes = raw.get("notable_quotes")
    if not isinstance(quotes, list) or not quotes:
        return raw
    said = "   ".join(_normalised(_answer_text(a)) for a in run.answers)
    kept: list[Any] = []
    for quote in quotes:
        text = quote.get("quote") if isinstance(quote, dict) else None
        if isinstance(text, str) and _normalised(text) and _normalised(text) in said:
            kept.append(quote)
        else:
            logger.warning("dropped an unsupported quote: run=%s quote=%r", run.id, text)
    return {**raw, "notable_quotes": kept}


def _answer_text(answer: Answer) -> str:
    return flatten_answer(answer.value)


def _transcript_for(run: SurveyRun) -> str:
    """The answers as the model should see them: scripted questions with their follow-ups
    beneath, in the order they were given. The chat transcript is deliberately left out —
    it carries the model's own phrasing, which is not evidence of what the participant said.
    """
    lines = ["Here is one completed response. Summarise it.", ""]
    for answer in run.answers:
        prefix = "  follow-up" if answer.kind is AnswerKind.follow_up else "Q"
        lines.append(f"{prefix}: {answer.question_text}")
        lines.append(
            f"{'  ' if answer.kind is AnswerKind.follow_up else ''}A: {_answer_text(answer)}"
        )
    return "\n".join(lines)
