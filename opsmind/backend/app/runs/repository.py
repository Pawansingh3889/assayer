"""Queries for reading completed and in-flight runs back out."""

from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.runs.models import SurveyRun
from app.templates.models import SurveyTemplateVersion
from app.users.models import User

ResultRow = Row[tuple[SurveyRun, SurveyTemplateVersion, User]]


class ResultsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_template(self, template_id: UUID) -> list[ResultRow]:
        stmt = (
            select(SurveyRun, SurveyTemplateVersion, User)
            .join(SurveyTemplateVersion, SurveyRun.template_version_id == SurveyTemplateVersion.id)
            .join(User, SurveyRun.participant_id == User.id)
            .where(SurveyTemplateVersion.template_id == template_id)
            .order_by(SurveyRun.started_at.desc())
            .options(selectinload(SurveyRun.answers))
        )
        return list((await self.session.execute(stmt)).all())

    async def get_detail(self, run_id: UUID) -> ResultRow | None:
        stmt = (
            select(SurveyRun, SurveyTemplateVersion, User)
            .join(SurveyTemplateVersion, SurveyRun.template_version_id == SurveyTemplateVersion.id)
            .join(User, SurveyRun.participant_id == User.id)
            .where(SurveyRun.id == run_id)
            .options(selectinload(SurveyRun.answers), selectinload(SurveyRun.messages))
        )
        return (await self.session.execute(stmt)).first()
