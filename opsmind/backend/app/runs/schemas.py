"""Read schemas for runs.

The transcript and answer shapes live here with the models they describe; conducting
and results both read them.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.runs.enums import AnswerKind, MessageRole, RunStatus


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: MessageRole
    content: str
    created_at: datetime


class AnswerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    question_id: UUID
    kind: AnswerKind
    question_text: str
    value: dict[str, Any]
    answered_at: datetime


class RunSummary(BaseModel):
    id: UUID
    participant_name: str
    status: RunStatus
    version: int
    answered: int
    total: int
    started_at: datetime
    completed_at: datetime | None


class RunDetail(BaseModel):
    id: UUID
    participant_name: str
    status: RunStatus
    version: int
    started_at: datetime
    completed_at: datetime | None
    messages: list[MessageRead]
    answers: list[AnswerRead]
    # Follow-ups the engine issued, per question id. The cap is spent when a probe is
    # asked, not when a reply to one is recorded, so this is the only faithful record of
    # follow-up spend: a probe that drew out the scripted answer itself leaves no
    # follow-up answer row behind, and the results view would show no sign of it.
    follow_ups_asked: dict[UUID, int] = Field(default_factory=dict)
    # Null until an creator asks for one; the stretch AI summary is generated on request,
    # not as a side effect of the participant finishing.
    summary: dict[str, Any] | None = None
