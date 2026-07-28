"""Template, question, and immutable version models.

A ``SurveyTemplate`` is the mutable draft; its questions live in ``survey_questions``.
Publishing snapshots the draft into a ``SurveyTemplateVersion`` (frozen JSONB). Runs
reference versions, never the draft, so later edits can never mutate answered surveys.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.templates.enums import AnswerType, TemplateStatus


class SurveyTemplate(Base):
    __tablename__ = "survey_templates"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[TemplateStatus] = mapped_column(
        SAEnum(TemplateStatus, name="template_status"), default=TemplateStatus.draft
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    questions: Mapped[list["SurveyQuestion"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="SurveyQuestion.position",
    )


class SurveyQuestion(Base):
    __tablename__ = "survey_questions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    template_id: Mapped[UUID] = mapped_column(ForeignKey("survey_templates.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    answer_type: Mapped[AnswerType] = mapped_column(SAEnum(AnswerType, name="answer_type"))
    options: Mapped[list[str]] = mapped_column(JSONB, default=list)
    allow_other: Mapped[bool] = mapped_column(Boolean, default=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_follow_ups: Mapped[bool] = mapped_column(Boolean, default=False)
    # {"question": <0-based position of an earlier question>, "op": "is"|"is_not",
    # "value": "..."} or NULL for always-visible. Keyed by position, not id: a draft
    # edit replaces every question row, so ids do not survive a save.
    show_when: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    template: Mapped["SurveyTemplate"] = relationship(back_populates="questions")

    __table_args__ = (
        UniqueConstraint("template_id", "position", name="question_template_position"),
    )


class SurveyTemplateVersion(Base):
    """Immutable snapshot produced at publish time. Never updated in place."""

    __tablename__ = "survey_template_versions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    template_id: Mapped[UUID] = mapped_column(ForeignKey("survey_templates.id"))
    version: Mapped[int] = mapped_column(Integer)
    # Frozen definition: {title, description, questions: [...]} at publish time.
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (UniqueConstraint("template_id", "version", name="version_template_version"),)
