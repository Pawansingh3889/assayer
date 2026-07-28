"""Service-level tests for template CRUD, publishing, and version immutability."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.errors import ConflictError, NotFoundError
from app.templates.enums import AnswerType, TemplateStatus
from app.templates.models import SurveyTemplateVersion
from app.templates.schemas import QuestionInput, TemplateCreate, TemplateUpdate
from app.templates.service import TemplateService


def _q(text: str, answer_type: AnswerType = AnswerType.short_text) -> QuestionInput:
    return QuestionInput(text=text, answer_type=answer_type)


async def test_create_and_get_draft(session, creator):
    svc = TemplateService(session)
    created = await svc.create_draft(
        TemplateCreate(title="T1", questions=[_q("q1"), _q("q2")]), creator
    )
    fetched = await svc.get_draft(created.id, creator)
    assert fetched.title == "T1"
    assert fetched.status is TemplateStatus.draft
    assert [q.text for q in fetched.questions] == ["q1", "q2"]
    assert [q.position for q in fetched.questions] == [0, 1]


async def test_publish_creates_version_one(session, creator):
    svc = TemplateService(session)
    t = await svc.create_draft(TemplateCreate(title="T", questions=[_q("q1")]), creator)
    version = await svc.publish(t.id, creator)
    assert version.version == 1
    assert (await svc.get_draft(t.id, creator)).status is TemplateStatus.published


async def test_republish_increments_and_v1_is_immutable(session, creator):
    svc = TemplateService(session)
    t = await svc.create_draft(TemplateCreate(title="Orig", questions=[_q("q1")]), creator)
    v1 = await svc.publish(t.id, creator)
    assert v1.version == 1

    # Edit the draft, then re-publish.
    await svc.update_draft(
        t.id, TemplateUpdate(title="Changed", questions=[_q("q1"), _q("q2")]), creator
    )
    v2 = await svc.publish(t.id, creator)
    assert v2.version == 2

    # v1's frozen snapshot must be untouched by the later edit — re-read from the db.
    fresh_v1 = await session.get(SurveyTemplateVersion, v1.id)
    assert fresh_v1 is not None
    assert fresh_v1.definition["title"] == "Orig"
    assert len(fresh_v1.definition["questions"]) == 1
    assert v2.definition["title"] == "Changed"
    assert len(v2.definition["questions"]) == 2


async def test_publish_empty_template_conflicts(session, creator):
    svc = TemplateService(session)
    t = await svc.create_draft(TemplateCreate(title="Empty"), creator)
    with pytest.raises(ConflictError):
        await svc.publish(t.id, creator)


async def test_get_missing_template_not_found(session, creator):
    with pytest.raises(NotFoundError):
        await TemplateService(session).get_draft(uuid4(), creator)


async def test_update_replaces_questions(session, creator):
    svc = TemplateService(session)
    t = await svc.create_draft(
        TemplateCreate(title="T", questions=[_q("a"), _q("b"), _q("c")]), creator
    )
    updated = await svc.update_draft(
        t.id, TemplateUpdate(title="T", questions=[_q("c"), _q("a")]), creator
    )
    assert [q.text for q in updated.questions] == ["c", "a"]
    assert [q.position for q in updated.questions] == [0, 1]


# The schema is the gate for both creating paths — the builder and the LLM draft — so
# these hold whichever one produced the template.


def test_blank_text_is_refused_and_real_text_is_stored_trimmed():
    """min_length counts characters, not content: "   " satisfies it and renders as a
    question with nothing to read."""
    with pytest.raises(ValidationError):
        QuestionInput(text="   ", answer_type=AnswerType.short_text)
    with pytest.raises(ValidationError):
        TemplateCreate(title="   ", questions=[_q("q")])
    padded = QuestionInput(text="  How long?  ", answer_type=AnswerType.short_text)
    assert padded.text == "How long?"
    assert TemplateCreate(title="  Shift feedback  ").title == "Shift feedback"


def test_a_blank_option_is_refused():
    """A blank option renders as an empty choice, and the answer gate matches "" to it —
    so it is selectable by an empty answer."""
    with pytest.raises(ValidationError):
        QuestionInput(text="q", answer_type=AnswerType.single_select, options=["Days", "   "])


def test_options_that_collide_case_insensitively_are_refused():
    """Answers are matched to options case-insensitively, so "days" alongside "Days" is a
    choice the participant can never land on and a count that can never be right."""
    with pytest.raises(ValidationError):
        QuestionInput(text="q", answer_type=AnswerType.single_select, options=["Days", "Days"])
    with pytest.raises(ValidationError):
        QuestionInput(text="q", answer_type=AnswerType.single_select, options=["Days", "days"])
    kept = QuestionInput(
        text="q", answer_type=AnswerType.single_select, options=[" Days ", "Nights"]
    )
    assert kept.options == ["Days", "Nights"]
