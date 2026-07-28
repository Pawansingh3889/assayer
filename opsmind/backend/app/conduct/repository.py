"""All run, answer, and transcript queries."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.runs.enums import AnswerKind, RunStatus
from app.runs.models import Answer, RunMessage, SurveyRun
from app.templates.models import SurveyTemplateVersion

# Postgres SQLSTATE for "could not obtain lock" under FOR UPDATE NOWAIT.
LOCK_NOT_AVAILABLE = "55P03"


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, run: SurveyRun) -> None:
        self.session.add(run)

    def add_answer(self, answer: Answer) -> None:
        self.session.add(answer)

    def add_message(self, message: RunMessage) -> None:
        self.session.add(message)

    async def try_lock(self, run_id: UUID) -> bool:
        """Take the run's row lock for this transaction, or report it already held.

        ``NOWAIT`` rather than a plain wait: the lock is held across a model call, so a
        waiting request would block for the length of that call and then process a
        message the participant almost certainly sent by double-clicking. Failing fast
        lets the caller say so instead.

        Selects only the id, so the row lock never has to contend with the eager loads
        ``get`` performs.
        """
        stmt = select(SurveyRun.id).where(SurveyRun.id == run_id).with_for_update(nowait=True)
        try:
            await self.session.execute(stmt)
        except DBAPIError as exc:
            # 55P03 lock_not_available is the only NOWAIT outcome that means "busy".
            # Anything else is a real database failure and must not be swallowed.
            if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
                raise
            # Postgres aborts the transaction on a failed NOWAIT, so the session is
            # unusable until it is rolled back.
            await self.session.rollback()
            return False
        return True

    async def get(self, run_id: UUID) -> SurveyRun | None:
        stmt = (
            select(SurveyRun)
            .where(SurveyRun.id == run_id)
            .options(selectinload(SurveyRun.answers), selectinload(SurveyRun.messages))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_version(self, template_id: UUID) -> SurveyTemplateVersion | None:
        stmt = (
            select(SurveyTemplateVersion)
            .where(SurveyTemplateVersion.template_id == template_id)
            .order_by(SurveyTemplateVersion.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_version(self, version_id: UUID) -> SurveyTemplateVersion | None:
        return await self.session.get(SurveyTemplateVersion, version_id)

    async def count_answers(self, run_id: UUID, question_id: UUID, kind: AnswerKind) -> int:
        stmt = (
            select(func.count())
            .select_from(Answer)
            .where(Answer.run_id == run_id, Answer.question_id == question_id, Answer.kind == kind)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def in_progress_for(self, participant_id: UUID) -> list[tuple[SurveyRun, UUID, str]]:
        """This participant's unfinished runs, newest first, with the survey they belong to.

        Powers "continue where you left off": without it a participant who closed the tab
        can only press Start again, which opens a *second* run and leaves the first
        stranded in the creator's results as an abandoned half-answer.
        """
        stmt = (
            select(SurveyRun, SurveyTemplateVersion.template_id, SurveyTemplateVersion.definition)
            .join(
                SurveyTemplateVersion,
                SurveyRun.template_version_id == SurveyTemplateVersion.id,
            )
            .where(
                SurveyRun.participant_id == participant_id,
                SurveyRun.status == RunStatus.in_progress,
            )
            .order_by(SurveyRun.started_at.desc())
            .options(selectinload(SurveyRun.answers))
        )
        rows = (await self.session.execute(stmt)).all()
        return [(r[0], r[1], r[2].get("title", "")) for r in rows]
