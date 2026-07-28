"""Template routes. Thin: resolve the creator, call one service method, shape output."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_201_CREATED, HTTP_204_NO_CONTENT

from app.auth.dependencies import get_current_user, require_creator
from app.db.session import get_session
from app.templates.enums import TemplateStatus
from app.templates.generation import GenerationService
from app.templates.models import SurveyTemplate
from app.templates.schemas import (
    GeneratedTemplate,
    GenerateRequest,
    RefineRequest,
    TemplateCreate,
    TemplateRead,
    TemplateSummary,
    TemplateUpdate,
    TemplateVersionRead,
)
from app.templates.service import TemplateService
from app.users.models import User

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


def _summary(
    template: SurveyTemplate, question_count: int, estimated_minutes: int | None = None
) -> TemplateSummary:
    return TemplateSummary(
        id=template.id,
        title=template.title,
        description=template.description,
        status=template.status,
        updated_at=template.updated_at,
        question_count=question_count,
        estimated_minutes=estimated_minutes,
    )


@router.post("", response_model=TemplateRead, status_code=HTTP_201_CREATED)
async def create_template(
    data: TemplateCreate,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> TemplateRead:
    template = await TemplateService(session).create_draft(data, creator)
    return TemplateRead.model_validate(template)


@router.post("/generate", response_model=GeneratedTemplate, status_code=HTTP_201_CREATED)
async def generate_template(
    data: GenerateRequest,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> GeneratedTemplate:
    template, note = await GenerationService(session).generate_draft(data.prompt, creator)
    return GeneratedTemplate(template=TemplateRead.model_validate(template), note=note)


@router.get("", response_model=list[TemplateSummary])
async def list_templates(
    status: TemplateStatus | None = None,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> list[TemplateSummary]:
    rows = await TemplateService(session).list_drafts(status, creator)
    return [_summary(t, n) for t, n in rows]


# Declared before /{template_id} so the literal path wins the match. Open to any
# signed-in user: a published survey is what a participant is meant to be able to answer.
@router.get("/published", response_model=list[TemplateSummary])
async def list_published(
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TemplateSummary]:
    rows = await TemplateService(session).list_published()
    return [_summary(t, n, minutes) for t, n, minutes in rows]


@router.get("/{template_id}", response_model=TemplateRead)
async def get_template(
    template_id: UUID,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> TemplateRead:
    template = await TemplateService(session).get_draft(template_id, creator)
    return TemplateRead.model_validate(template)


@router.put("/{template_id}", response_model=TemplateRead)
async def update_template(
    template_id: UUID,
    data: TemplateUpdate,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> TemplateRead:
    template = await TemplateService(session).update_draft(template_id, data, creator)
    return TemplateRead.model_validate(template)


@router.delete("/{template_id}", status_code=HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: UUID,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> None:
    await TemplateService(session).delete_draft(template_id, creator)


@router.post("/{template_id}/refine", response_model=GeneratedTemplate)
async def refine_template(
    template_id: UUID,
    data: RefineRequest,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> GeneratedTemplate:
    template, note = await GenerationService(session).refine_draft(
        template_id, data.instruction, creator
    )
    return GeneratedTemplate(template=TemplateRead.model_validate(template), note=note)


@router.post(
    "/{template_id}/publish", response_model=TemplateVersionRead, status_code=HTTP_201_CREATED
)
async def publish_template(
    template_id: UUID,
    creator: User = Depends(require_creator),
    session: AsyncSession = Depends(get_session),
) -> TemplateVersionRead:
    version = await TemplateService(session).publish(template_id, creator)
    return TemplateVersionRead.model_validate(version)
