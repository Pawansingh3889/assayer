"""Pydantic v2 request/response schemas for templates."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.templates.enums import AnswerType, ShowWhenOp, TemplateStatus

SELECT_TYPES = {AnswerType.single_select, AnswerType.multi_select}


class ShowWhen(BaseModel):
    """A question's visibility condition.

    ``question`` is the **0-based position** of an earlier question, not its id. Draft
    edits replace every question row, so ids do not survive a save — a condition keyed
    on one would break the moment the creator edited anything. Positions are stable
    within a draft, and frozen for good once a version is published.
    """

    question: int = Field(ge=0)
    op: ShowWhenOp
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _value_has_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("show_when value cannot be blank")
        return text


class QuestionInput(BaseModel):
    text: str = Field(min_length=1)
    answer_type: AnswerType
    options: list[str] = Field(default_factory=list)
    allow_other: bool = False
    required: bool = True
    allow_follow_ups: bool = False
    show_when: ShowWhen | None = None

    @field_validator("text")
    @classmethod
    def _text_has_content(cls, value: str) -> str:
        # min_length counts characters, not content: "   " passes it and renders as a
        # question with nothing to read.
        text = value.strip()
        if not text:
            raise ValueError("question text cannot be blank")
        return text

    @field_validator("options")
    @classmethod
    def _options_are_distinct_and_readable(cls, values: list[str]) -> list[str]:
        """Options are matched case-insensitively when an answer comes back, so anything
        that collides under that rule is a choice the participant can never land on."""
        options: list[str] = []
        seen: set[str] = set()
        for value in values:
            option = value.strip()
            if not option:
                raise ValueError("an option cannot be blank")
            if option.casefold() in seen:
                raise ValueError(f"duplicate option '{option}'")
            seen.add(option.casefold())
            options.append(option)
        return options

    @model_validator(mode="after")
    def _check_options(self) -> "QuestionInput":
        if self.answer_type in SELECT_TYPES:
            if not self.options:
                raise ValueError(f"{self.answer_type.value} requires at least one option")
        elif self.options:
            raise ValueError(f"{self.answer_type.value} must not carry options")
        return self


class TemplateWrite(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    questions: list[QuestionInput] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title_has_content(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title cannot be blank")
        return title

    @model_validator(mode="after")
    def _conditions_can_actually_be_evaluated(self) -> "TemplateWrite":
        """A condition must point backwards at a question that can answer it.

        The engine walks questions in order and decides visibility from answers already
        recorded, so a condition on a later question — or on itself — could never be
        true and would silently hide the question forever. Cheaper to refuse the survey
        than to ship one with a question nobody can reach.
        """
        for position, question in enumerate(self.questions):
            condition = question.show_when
            if condition is None:
                continue
            if condition.question >= position:
                raise ValueError(
                    f"question {position + 1} is shown by a condition on question "
                    f"{condition.question + 1}, which is not earlier in the survey"
                )
            referenced = self.questions[condition.question]
            if referenced.options:
                # The value is compared against what was recorded, and a select can only
                # ever record one of its own options (or a write-in). A value that is
                # neither is a typo the creator will otherwise only discover by running
                # the survey and finding the question never appears.
                allowed = {o.casefold() for o in referenced.options}
                if condition.value.casefold() not in allowed and not referenced.allow_other:
                    raise ValueError(
                        f"question {position + 1}'s condition wants "
                        f"{condition.value!r}, which is not an option on question "
                        f"{condition.question + 1} ({', '.join(referenced.options)})"
                    )
        return self


# Create and update share the same shape (a full draft), but stay distinct types
# so the API and future divergence read clearly.
class TemplateCreate(TemplateWrite):
    pass


class TemplateUpdate(TemplateWrite):
    pass


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)


class RefineRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)


class QuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    position: int
    text: str
    answer_type: AnswerType
    options: list[str]
    allow_other: bool
    required: bool
    allow_follow_ups: bool
    show_when: ShowWhen | None = None


class TemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    status: TemplateStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    questions: list[QuestionRead]


class GeneratedTemplate(BaseModel):
    """A drafted or refined template plus the model's short note on what it did."""

    template: TemplateRead
    note: str


class TemplateSummary(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: TemplateStatus
    updated_at: datetime
    question_count: int
    # Only populated for the published list a participant chooses from; a draft has no
    # meaningful estimate because it is not what anyone will be asked.
    estimated_minutes: int | None = None


class TemplateVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template_id: UUID
    version: int
    published_at: datetime
