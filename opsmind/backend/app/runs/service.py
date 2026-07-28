"""Reading run results back for creators.

Deliberately separate from the conduct engine: conducting is participant-owned and
refuses anyone else, while results are creator-facing and cross-participant.
"""

import csv
import io
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.runs.enums import AnswerKind
from app.runs.models import REPLY_PREFIX, SurveyRun
from app.runs.repository import ResultsRepository
from app.runs.schemas import AnswerRead, MessageRead, RunDetail, RunSummary
from app.templates.models import SurveyTemplate, SurveyTemplateVersion
from app.templates.repository import TemplateRepository
from app.templates.snapshot import questions_of
from app.templates.visibility import remaining_possible
from app.users.models import User

logger = logging.getLogger("app.runs.results")

EXPORT_COLUMNS = [
    "run_id",
    "participant",
    "run_status",
    "version",
    "question",
    "kind",
    "answer",
    "answered_at",
]


class ResultsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ResultsRepository(session)
        self.templates = TemplateRepository(session)

    async def list_runs(self, template_id: UUID, creator: User) -> list[RunSummary]:
        await self._owned_or_404(template_id, creator)
        rows = await self.repo.list_for_template(template_id)
        return [_summary(run, version, user) for run, version, user in rows]

    async def get_run(self, template_id: UUID, run_id: UUID, creator: User) -> RunDetail:
        await self._owned_or_404(template_id, creator)
        row = await self.repo.get_detail(run_id)
        if row is None:
            raise NotFoundError("Run not found.")
        run, version, user = row
        if version.template_id != template_id:
            raise NotFoundError("That run belongs to a different template.")
        return RunDetail(
            id=run.id,
            participant_name=user.display_name,
            status=run.status,
            version=version.version,
            started_at=run.started_at,
            completed_at=run.completed_at,
            messages=[MessageRead.model_validate(m) for m in run.messages],
            answers=[AnswerRead.model_validate(a) for a in run.answers],
            follow_ups_asked=follow_ups_asked(run),
            summary=run.summary,
        )

    async def export(self, template_id: UUID, creator: User) -> tuple[str, list[dict[str, Any]]]:
        """Every answer across every run, flattened to one row each, for download."""
        template = await self._owned_or_404(template_id, creator)
        rows: list[dict[str, Any]] = []
        for run, version, user in await self.repo.list_for_template(template_id):
            for answer in run.answers:
                rows.append(
                    {
                        "run_id": str(run.id),
                        "participant": user.display_name,
                        "run_status": run.status.value,
                        "version": version.version,
                        "question": answer.question_text,
                        "kind": answer.kind.value,
                        "answer": flatten_answer(answer.value),
                        "answered_at": answer.answered_at.isoformat(),
                    }
                )
        return template.title, rows

    async def _owned_or_404(self, template_id: UUID, creator: User) -> SurveyTemplate:
        """Responses carry participant names and verbatim transcripts, so they are
        readable only by the creator who created the survey. Someone else's template
        reads as absent rather than forbidden."""
        template = await self.templates.get(template_id)
        if template is None or template.created_by != creator.id:
            raise NotFoundError("Template not found.")
        return template


def follow_ups_asked(run: SurveyRun) -> dict[UUID, int]:
    """The follow-up probes the engine issued, per question id.

    ``probes_asked`` is the engine's own ledger and holds two things: follow-up counts
    keyed by question id, and reply counts under a ``reply:`` prefix sharing the same
    JSONB. That prefix is a storage detail, so only the probes cross the API boundary.
    """
    counts: dict[UUID, int] = {}
    for key, count in run.probes_asked.items():
        if key.startswith(REPLY_PREFIX):
            continue
        try:
            counts[UUID(key)] = count
        except ValueError:
            # Only the engine writes this column, so a key that is neither a question id
            # nor a reply marker means an engine bug — worth a line in the log, but not
            # worth failing an creator's whole results view over.
            logger.warning("skipping unrecognised probes_asked key: run=%s key=%r", run.id, key)
    return counts


def flatten_answer(value: dict[str, Any]) -> str:
    """One human-readable cell per stored answer value, whatever its shape."""
    if "text" in value:
        return str(value["text"])
    if "rating" in value:
        return str(value["rating"])
    if "number" in value:
        return str(value["number"])
    if "yes_no" in value:
        return "yes" if value["yes_no"] else "no"
    if "date" in value:
        return str(value["date"])
    if "option" in value:
        return str(value["option"])
    if "options" in value:  # before "other": a multi_select may carry both keys
        parts = [str(v) for v in value["options"]]
        parts += [f"(other) {v}" for v in value.get("other", [])]
        return "; ".join(parts)
    if "other" in value:
        return f"(other) {value['other']}"
    if "unanswerable" in value:
        return f"(declined) {value['unanswerable']}"
    return json.dumps(value)  # future shapes export verbatim rather than crash a download


def to_csv(rows: list[dict[str, Any]]) -> str:
    """RFC-4180 CSV with a UTF-8 BOM so Excel opens it with the right encoding."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return "\ufeff" + buffer.getvalue()


def _summary(run: SurveyRun, version: SurveyTemplateVersion, user: User) -> RunSummary:
    questions = questions_of(version.definition)
    answers = {str(a.question_id): a.value for a in run.answers if a.kind is AnswerKind.scripted}
    return RunSummary(
        id=run.id,
        participant_name=user.display_name,
        status=run.status,
        version=version.version,
        answered=len(answers),
        # Same denominator the participant sees: questions a condition ruled out were
        # never asked, so counting them would leave every conditional run looking
        # abandoned at "2 of 4".
        total=len(answers) + remaining_possible(run.current_question_index, questions, answers),
        started_at=run.started_at,
        completed_at=run.completed_at,
    )
