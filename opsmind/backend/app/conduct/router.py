"""Run routes. Thin: resolve the participant, call one engine method, shape the response."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_201_CREATED

from app.auth.dependencies import require_participant
from app.conduct.engine import ConductEngine
from app.conduct.schemas import (
    CurrentQuestion,
    ResumableRun,
    RunMessageRequest,
    RunRead,
    StartRunRequest,
)
from app.db.session import get_session
from app.runs.models import SurveyRun
from app.runs.schemas import AnswerRead, MessageRead
from app.users.models import User

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


async def _to_read(engine: ConductEngine, run: SurveyRun) -> RunRead:
    questions = await engine.questions(run)
    current = None
    if run.current_question_index < len(questions):
        q = questions[run.current_question_index]
        current = CurrentQuestion(
            id=UUID(q["id"]),
            text=q["text"],
            answer_type=q["answer_type"],
            options=q["options"],
            allow_other=q["allow_other"],
            required=q["required"],
        )
    answered, total = engine.progress(run, questions)
    return RunRead(
        id=run.id,
        status=run.status,
        current_question=current,
        answered=answered,
        total=total,
        messages=[
            MessageRead.model_validate(m) for m in sorted(run.messages, key=lambda m: m.created_at)
        ],
        answers=[AnswerRead.model_validate(a) for a in run.answers],
    )


@router.post("", response_model=RunRead, status_code=HTTP_201_CREATED)
async def start_run(
    data: StartRunRequest,
    participant: User = Depends(require_participant),
    session: AsyncSession = Depends(get_session),
) -> RunRead:
    engine = ConductEngine(session)
    run = await engine.start_run(data.template_id, participant)
    return await _to_read(engine, run)


# Declared before /{run_id} so the literal path is never parsed as a run id.
@router.get("", response_model=list[ResumableRun])
async def my_unfinished_runs(
    participant: User = Depends(require_participant),
    session: AsyncSession = Depends(get_session),
) -> list[ResumableRun]:
    """The participant's own unfinished runs, so a survey left half-done can be resumed
    rather than restarted from scratch under a second run."""
    engine = ConductEngine(session)
    return [
        ResumableRun(
            id=run.id,
            template_id=template_id,
            title=title,
            answered=answered,
            total=total,
            started_at=run.started_at,
        )
        for run, template_id, title, answered, total in await engine.resumable(participant)
    ]


@router.get("/{run_id}", response_model=RunRead)
async def get_run(
    run_id: UUID,
    participant: User = Depends(require_participant),
    session: AsyncSession = Depends(get_session),
) -> RunRead:
    engine = ConductEngine(session)
    run = await engine.load(run_id, participant)
    return await _to_read(engine, run)


@router.post("/{run_id}/messages", response_model=RunRead)
async def post_message(
    run_id: UUID,
    data: RunMessageRequest,
    participant: User = Depends(require_participant),
    session: AsyncSession = Depends(get_session),
) -> RunRead:
    engine = ConductEngine(session)
    run = await engine.handle_message(run_id, data.content, participant)
    return await _to_read(engine, run)
