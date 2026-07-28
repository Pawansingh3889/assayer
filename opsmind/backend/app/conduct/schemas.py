"""Request and response schemas for conducting a run."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from app.runs.enums import RunStatus
from app.runs.schemas import AnswerRead, MessageRead


class StartRunRequest(BaseModel):
    template_id: UUID


class RunMessageRequest(BaseModel):
    # strip_whitespace makes "   " fail min_length: a blank message must 422 here, not
    # reach the transcript and burn a model turn on nothing.
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class CurrentQuestion(BaseModel):
    id: UUID
    text: str
    answer_type: str
    options: list[str]
    allow_other: bool
    required: bool


class RunRead(BaseModel):
    id: UUID
    status: RunStatus
    current_question: CurrentQuestion | None
    answered: int
    total: int
    messages: list[MessageRead]
    answers: list[AnswerRead]


class ResumableRun(BaseModel):
    """An unfinished run on the participant's own home, so they can pick it back up.

    Without this the only affordance is Start, which opens a *second* run and strands
    the first in the creator's results as an abandoned half-answer.
    """

    id: UUID
    template_id: UUID
    title: str
    answered: int
    total: int
    started_at: datetime
