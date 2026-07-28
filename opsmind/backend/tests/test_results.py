"""Reading results back: what an creator gets, and what they are refused.

Results cross participants by design, so the boundary that matters here is the template
a run belongs to, not the person who answered it.
"""

from uuid import UUID, uuid4

import pytest

from app.conduct.engine import ConductEngine
from app.errors import NotFoundError
from app.runs.enums import AnswerKind, RunStatus
from app.runs.models import REPLY_PREFIX
from app.runs.service import ResultsService
from app.templates.enums import AnswerType
from app.templates.schemas import QuestionInput, TemplateCreate, TemplateUpdate
from app.templates.service import TemplateService
from tests.fakes import FakeLLM, follow_up, move_on, record, reply


async def _answer_first(session, run, participant):
    llm = FakeLLM(record("Line lead"), move_on())
    return await ConductEngine(session, llm=llm).handle_message(run.id, "line lead", participant)


async def test_lists_who_answered_and_how_far_they_got(session, creator, participant, published):
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await _answer_first(session, run, participant)

    summaries = await ResultsService(session).list_runs(published.id, creator)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.id == run.id
    assert summary.participant_name == "Test Participant"
    assert summary.status is RunStatus.in_progress
    assert (summary.answered, summary.total) == (1, 2)
    assert summary.version == 1
    assert summary.completed_at is None


async def test_a_run_is_reported_against_the_version_it_answered(
    session, creator, participant, published
):
    """The creator rewrites the survey mid-run; the response must not be re-scored."""
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await _answer_first(session, run, participant)

    svc = TemplateService(session)
    await svc.update_draft(
        published.id,
        TemplateUpdate(
            title="Rewritten",
            questions=[QuestionInput(text="One question now", answer_type=AnswerType.long_text)],
        ),
        creator,
    )
    assert (await svc.publish(published.id, creator)).version == 2

    summary = (await ResultsService(session).list_runs(published.id, creator))[0]
    assert summary.version == 1
    assert summary.total == 2  # v1's question count, not v2's


async def test_detail_returns_the_answers_and_the_transcript(
    session, creator, participant, published
):
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await _answer_first(session, run, participant)

    detail = await ResultsService(session).get_run(published.id, run.id, creator)

    assert detail.participant_name == "Test Participant"
    assert [a.question_text for a in detail.answers] == ["What's your role?"]
    assert detail.answers[0].value == {"text": "Line lead"}
    assert [m.role.value for m in detail.messages] == ["assistant", "user", "assistant"]


async def test_a_follow_up_is_ordered_under_the_question_it_probed(
    session, creator, participant, published
):
    """Results attach a follow-up to its parent, so ordering and the shared id both matter."""
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    probe = FakeLLM(record("Line lead"), follow_up("What does that involve day to day?"))
    run = await ConductEngine(session, llm=probe).handle_message(run.id, "line lead", participant)
    reply = FakeLLM(record("Running the handover"), move_on())
    await ConductEngine(session, llm=reply).handle_message(run.id, "the handover", participant)

    answers = (await ResultsService(session).get_run(published.id, run.id, creator)).answers

    assert [a.kind.value for a in answers] == ["scripted", "follow_up"]
    assert answers[0].question_id == answers[1].question_id  # the follow-up's parent
    assert answers[0].answered_at < answers[1].answered_at
    assert answers[1].question_text == "What does that involve day to day?"


async def test_a_run_from_another_template_is_not_found(session, creator, participant, published):
    other = await TemplateService(session).create_draft(
        TemplateCreate(
            title="A different survey",
            questions=[QuestionInput(text="Anything?", answer_type=AnswerType.short_text)],
        ),
        creator,
    )
    await TemplateService(session).publish(other.id, creator)
    stray = await ConductEngine(session, llm=FakeLLM()).start_run(other.id, participant)

    with pytest.raises(NotFoundError):
        await ResultsService(session).get_run(published.id, stray.id, creator)


async def test_missing_template_is_not_found(session, creator, published):
    with pytest.raises(NotFoundError):
        await ResultsService(session).list_runs(uuid4(), creator)


async def test_export_flattens_every_answer_to_a_row(session, creator, participant, published):
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await _answer_first(session, run, participant)

    title, rows = await ResultsService(session).export(published.id, creator)

    assert title == "Onboarding check-in"
    assert len(rows) == 1
    row = rows[0]
    assert row["participant"] == "Test Participant"
    assert row["question"] == "What's your role?"
    assert row["answer"] == "Line lead"
    assert (row["kind"], row["version"], row["run_status"]) == ("scripted", 1, "in_progress")


async def test_export_is_scoped_to_the_owning_creator(
    session, creator, other_creator, participant, published
):
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await _answer_first(session, run, participant)

    with pytest.raises(NotFoundError):
        await ResultsService(session).export(published.id, other_creator)


def test_csv_export_is_excel_ready():
    """Header row, one line per answer, and a UTF-8 BOM so Excel decodes it right."""
    from app.runs.service import EXPORT_COLUMNS, to_csv

    rows = [
        {
            "run_id": "r1",
            "participant": "Rosa",
            "run_status": "completed",
            "version": 1,
            "question": 'She said "hi", twice',
            "kind": "scripted",
            "answer": "Days; Nights",
            "answered_at": "2026-07-24T12:00:00+00:00",
        }
    ]
    out = to_csv(rows)
    assert out.startswith("\ufeff")
    assert out.splitlines()[0] == "\ufeff" + ",".join(EXPORT_COLUMNS)
    assert '"She said ""hi"", twice"' in out  # embedded quotes survive per RFC 4180


def test_every_answer_shape_flattens_to_a_readable_cell():
    from app.runs.service import flatten_answer

    assert flatten_answer({"text": "Line lead"}) == "Line lead"
    assert flatten_answer({"rating": 4}) == "4"
    assert flatten_answer({"yes_no": False}) == "no"
    assert flatten_answer({"option": "Days"}) == "Days"
    assert flatten_answer({"options": ["A", "B"], "other": ["C"]}) == "A; B; (other) C"
    assert flatten_answer({"other": "Split shift"}) == "(other) Split shift"
    assert flatten_answer({"unanswerable": "declined"}) == "(declined) declined"
    assert flatten_answer({"mystery": 1}) == '{"mystery": 1}'  # future shapes never crash


async def test_follow_up_spend_is_visible_even_when_no_follow_up_answer_exists(
    session, creator, participant, published
):
    """The case that motivated exposing this at all.

    A probe is charged when the engine issues it, and a probe often draws out the
    scripted answer itself — so the run records one scripted answer and no follow-up
    row. Counting follow-up answers would report "never probed", which is how a live
    acceptance walkthrough twice concluded the feature was broken when it was not.
    """
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    probed = FakeLLM(follow_up("Which line do you run?"))
    run = await ConductEngine(session, llm=probed).handle_message(
        run.id, "bit of both", participant
    )

    detail = await ResultsService(session).get_run(published.id, run.id, creator)
    question_id = UUID(next(iter(run.probes_asked)))

    assert not [a for a in detail.answers if a.kind is AnswerKind.follow_up]
    assert detail.follow_ups_asked == {question_id: 1}


async def test_replies_are_not_reported_as_follow_ups(session, creator, participant, published):
    """Replies share the probes JSONB under a prefix. That is storage, not survey data,
    and an creator counting follow-ups must not see it."""
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    chatty = FakeLLM(reply("It means your job title."))
    run = await ConductEngine(session, llm=chatty).handle_message(
        run.id, "what do you mean?", participant
    )

    detail = await ResultsService(session).get_run(published.id, run.id, creator)

    assert any(k.startswith(REPLY_PREFIX) for k in run.probes_asked)  # it was stored
    assert detail.follow_ups_asked == {}  # but never surfaced


async def test_a_run_that_was_never_probed_reports_nothing(
    session, creator, participant, published
):
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await _answer_first(session, run, participant)

    detail = await ResultsService(session).get_run(published.id, run.id, creator)

    assert detail.follow_ups_asked == {}
