"""All template, question, and version queries."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.templates.enums import TemplateStatus
from app.templates.models import SurveyQuestion, SurveyTemplate, SurveyTemplateVersion


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, template: SurveyTemplate) -> None:
        self.session.add(template)

    async def get(self, template_id: UUID) -> SurveyTemplate | None:
        stmt = (
            select(SurveyTemplate)
            .where(SurveyTemplate.id == template_id)
            .options(selectinload(SurveyTemplate.questions))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_summaries(
        self, status: TemplateStatus | None, created_by: UUID | None = None
    ) -> list[tuple[SurveyTemplate, int]]:
        counts = (
            select(SurveyQuestion.template_id, func.count().label("n"))
            .group_by(SurveyQuestion.template_id)
            .subquery()
        )
        stmt = (
            select(SurveyTemplate, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.template_id == SurveyTemplate.id)
            .order_by(SurveyTemplate.updated_at.desc())
        )
        if status is not None:
            stmt = stmt.where(SurveyTemplate.status == status)
        if created_by is not None:
            stmt = stmt.where(SurveyTemplate.created_by == created_by)
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], int(row[1])) for row in rows]

    async def delete(self, template: SurveyTemplate) -> None:
        await self.session.delete(template)

    async def next_version(self, template_id: UUID) -> int:
        stmt = select(func.coalesce(func.max(SurveyTemplateVersion.version), 0)).where(
            SurveyTemplateVersion.template_id == template_id
        )
        current = (await self.session.execute(stmt)).scalar_one()
        return int(current) + 1

    def add_version(self, version: SurveyTemplateVersion) -> None:
        self.session.add(version)

    async def list_published_latest(self) -> list[tuple[SurveyTemplate, dict[str, Any]]]:
        """Published surveys with the definition a participant would actually be asked.

        Not the draft. A draft keeps evolving after publication, so counting its
        questions advertised a survey that does not exist yet — three questions on the
        home page, two in the run.
        """
        stmt = (
            select(SurveyTemplate, SurveyTemplateVersion.definition)
            .join(SurveyTemplateVersion, SurveyTemplateVersion.template_id == SurveyTemplate.id)
            .where(SurveyTemplate.status == TemplateStatus.published)
            .distinct(SurveyTemplateVersion.template_id)
            # DISTINCT ON keeps the first row per template, so the highest version wins.
            .order_by(SurveyTemplateVersion.template_id, SurveyTemplateVersion.version.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        newest_first = sorted(rows, key=lambda r: r[0].updated_at, reverse=True)
        return [(r[0], r[1]) for r in newest_first]
