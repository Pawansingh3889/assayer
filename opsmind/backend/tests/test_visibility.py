"""Conditional visibility: the rules, and the engine acting on them.

The evaluation functions are pure, so most of this needs no database — but the skipping
itself is engine behaviour and is tested through a real run.
"""

import pytest
from pydantic import ValidationError

from app.conduct.engine import ConductEngine
from app.runs.enums import AnswerKind, RunStatus
from app.templates.enums import AnswerType
from app.templates.schemas import QuestionInput, TemplateCreate
from app.templates.service import TemplateService
from app.templates.visibility import is_satisfied, next_visible, remaining_possible
from tests.fakes import FakeLLM, move_on, record

IS = {"question": 0, "op": "is", "value": "Quality Manager"}
IS_NOT = {"question": 0, "op": "is_not", "value": "Quality Manager"}


# ------------------------------------------------------------------ the comparison


@pytest.mark.parametrize(
    ("answer", "satisfies_is"),
    [
        ({"option": "Quality Manager"}, True),
        ({"option": "quality manager"}, True),  # options match case-insensitively
        ({"option": "Line Operator"}, False),
        ({"text": "Quality Manager"}, True),
        ({"other": "Quality Manager"}, True),  # a write-in counts as the answer
        ({"options": ["Sage", "Quality Manager"]}, True),  # multi-select: "chose this"
        ({"options": ["Sage"]}, False),
    ],
)
def test_is_compares_against_what_was_recorded(answer: dict, satisfies_is: bool) -> None:
    assert is_satisfied(IS, answer) is satisfies_is
    assert is_satisfied(IS_NOT, answer) is not satisfies_is


@pytest.mark.parametrize("value", [{"yes_no": True}, {"yes_no": False}])
def test_yes_no_matches_the_word_an_creator_would_write(value: dict) -> None:
    condition = {"question": 0, "op": "is", "value": "yes"}
    assert is_satisfied(condition, value) is value["yes_no"]


def test_a_number_matches_however_it_was_stored() -> None:
    """4 and 4.0 are the same answer to an creator who typed 4."""
    condition = {"question": 0, "op": "is", "value": "4"}
    assert is_satisfied(condition, {"rating": 4})
    assert is_satisfied(condition, {"number": 4.0})


def test_an_unanswered_reference_satisfies_neither_operator() -> None:
    """A condition needs a premise. Without one the question stays hidden, rather than
    'is_not' quietly becoming true for everyone who was never asked."""
    assert is_satisfied(IS, None) is False
    assert is_satisfied(IS_NOT, None) is False


def test_a_decline_satisfies_neither_operator() -> None:
    """Declining records that they would not say, not that the answer is something
    else — so it must not open a question that hangs off the answer."""
    declined = {"unanswerable": "participant declined"}
    assert is_satisfied(IS, declined) is False
    assert is_satisfied(IS_NOT, declined) is False


# ------------------------------------------------------------------ walking questions


def _q(qid: str, position: int, show_when: dict | None = None) -> dict:
    return {"id": qid, "position": position, "text": f"q{position}", "show_when": show_when}


def test_next_visible_skips_a_question_whose_condition_fails() -> None:
    questions = [_q("a", 0), _q("b", 1, IS), _q("c", 2)]
    answers = {"a": {"option": "Line Operator"}}
    assert next_visible(1, questions, answers) == 2


def test_conditions_cascade_through_a_skipped_question() -> None:
    """b is hidden, so it records no answer, so c — which hangs off b — is hidden too."""
    questions = [_q("a", 0), _q("b", 1, IS), _q("c", 2, {"question": 1, "op": "is", "value": "x"})]
    answers = {"a": {"option": "Line Operator"}}
    assert next_visible(1, questions, answers) == 3  # nothing left: the run is done


def test_the_total_only_ever_shrinks() -> None:
    """A denominator that grew mid-survey would read as the survey getting longer the
    more you answer."""
    questions = [_q("a", 0), _q("b", 1, IS), _q("c", 2)]
    assert remaining_possible(0, questions, {}) == 3  # b's outcome is not known yet
    assert remaining_possible(1, questions, {"a": {"option": "Quality Manager"}}) == 2
    assert remaining_possible(1, questions, {"a": {"option": "Line Operator"}}) == 1  # b ruled out


# ------------------------------------------------------------------ the schema gate


def test_a_condition_must_point_at_an_earlier_question() -> None:
    """Questions are asked in order from answers already recorded, so a forward
    reference could never be true and would hide the question forever."""
    for bad in (1, 2):  # itself, and a later question
        with pytest.raises(ValidationError, match="not earlier"):
            TemplateCreate(
                title="T",
                questions=[
                    QuestionInput(text="q0", answer_type=AnswerType.short_text),
                    QuestionInput(
                        text="q1",
                        answer_type=AnswerType.short_text,
                        show_when={"question": bad, "op": "is", "value": "x"},
                    ),
                    QuestionInput(text="q2", answer_type=AnswerType.short_text),
                ],
            )


def test_a_condition_must_want_an_option_the_question_offers() -> None:
    """Otherwise the creator only finds the typo by running the survey and noticing the
    question never appears."""
    with pytest.raises(ValidationError, match="not an option"):
        TemplateCreate(
            title="T",
            questions=[
                QuestionInput(
                    text="Role?",
                    answer_type=AnswerType.single_select,
                    options=["Quality Manager", "Line Operator"],
                ),
                QuestionInput(
                    text="Outcomes?",
                    answer_type=AnswerType.long_text,
                    show_when={"question": 0, "op": "is", "value": "Quality Mangler"},
                ),
            ],
        )


def test_a_write_in_question_accepts_any_condition_value() -> None:
    """With allow_other the recorded answer genuinely can be anything."""
    TemplateCreate(
        title="T",
        questions=[
            QuestionInput(
                text="Role?",
                answer_type=AnswerType.single_select,
                options=["Quality Manager"],
                allow_other=True,
            ),
            QuestionInput(
                text="Outcomes?",
                answer_type=AnswerType.long_text,
                show_when={"question": 0, "op": "is", "value": "Shift Lead"},
            ),
        ],
    )


# ------------------------------------------------------------------ the engine


async def _published(session, creator):
    """Role -> (only for Quality Manager) outcomes -> rating."""
    svc = TemplateService(session)
    template = await svc.create_draft(
        TemplateCreate(
            title="Conditional",
            questions=[
                QuestionInput(
                    text="What is your role?",
                    answer_type=AnswerType.single_select,
                    options=["Quality Manager", "Line Operator"],
                ),
                QuestionInput(
                    text="What quality outcomes do you own?",
                    answer_type=AnswerType.long_text,
                    show_when={"question": 0, "op": "is", "value": "Quality Manager"},
                ),
                QuestionInput(text="Rate your onboarding", answer_type=AnswerType.rating),
            ],
        ),
        creator,
    )
    await svc.publish(template.id, creator)
    return template


async def test_the_engine_skips_a_question_whose_condition_fails(session, creator, participant):
    template = await _published(session, creator)
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(template.id, participant)

    llm = FakeLLM(record("Line Operator"), move_on())
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "line operator", participant)

    # Straight past the conditional question to the rating.
    assert run.current_question_index == 2
    assert run.messages[-1].content == "Rate your onboarding"

    llm = FakeLLM(record(4), move_on())
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "4", participant)
    assert run.status is RunStatus.completed
    answered = [a.question_text for a in run.answers if a.kind is AnswerKind.scripted]
    assert answered == ["What is your role?", "Rate your onboarding"]


async def test_the_engine_asks_the_question_when_the_condition_holds(session, creator, participant):
    template = await _published(session, creator)
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(template.id, participant)

    # q0 permits no probing, so the engine advances straight off the record turn and
    # that turn's own text is what the participant hears.
    llm = FakeLLM(record("Quality Manager", "Got it. What outcomes do you own?"))
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "QM", participant)

    assert run.current_question_index == 1  # the conditional question, not skipped
    # Nothing was skipped, so the model's own warmer phrasing is kept rather than being
    # replaced by the raw question text.
    assert run.messages[-1].content == "Got it. What outcomes do you own?"


async def test_a_completed_conditional_run_reads_n_of_n(session, creator, participant):
    """Counting the skipped question would leave every conditional run looking
    abandoned at '2 of 3'."""
    template = await _published(session, creator)
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(template.id, participant)
    skip = ConductEngine(session, llm=FakeLLM(record("Line Operator"), move_on()))
    run = await skip.handle_message(run.id, "line operator", participant)
    run = await ConductEngine(session, llm=FakeLLM(record(4), move_on())).handle_message(
        run.id, "4", participant
    )

    questions = await engine.questions(run)
    assert engine.progress(run, questions) == (2, 2)
