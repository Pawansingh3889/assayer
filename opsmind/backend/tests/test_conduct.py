"""Conduct engine tests. The LLM is faked at the client boundary, so every assertion
here is about the engine's own decisions: what it offers, what it accepts, what it
refuses, and where run state lives.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from app.conduct.engine import (
    MAX_FOLLOW_UPS,
    MAX_REPLIES,
    TRANSCRIPT_WINDOW,
    ConductEngine,
    _transcript,
)
from app.conduct.repository import RunRepository
from app.errors import ConflictError
from app.llm.base import LLMError, NoToolCallError, ToolTurn
from app.runs.enums import AnswerKind, MessageRole, RunStatus
from app.runs.models import RunMessage, SurveyRun
from app.templates.enums import AnswerType
from app.templates.schemas import QuestionInput, TemplateCreate, TemplateUpdate
from app.templates.service import TemplateService
from tests.fakes import FakeLLM
from tests.fakes import follow_up as _follow_up
from tests.fakes import move_on as _move_on
from tests.fakes import record as _record
from tests.fakes import reply as _reply


async def test_start_opens_with_the_first_question(session, participant, published):
    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    assert run.status is RunStatus.in_progress
    assert run.current_question_index == 0
    assert len(run.messages) == 1
    assert "What's your role?" in run.messages[0].content


async def test_records_answer_then_advances(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    # q0 permits probing, so after recording the engine loops once for the probe decision.
    llm = FakeLLM(_record("Line lead"), _move_on("Thanks. How was onboarding?"))
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "line lead", participant)

    assert llm.calls == 2
    assert run.current_question_index == 1
    scripted = [a for a in run.answers if a.kind is AnswerKind.scripted]
    assert len(scripted) == 1
    assert scripted[0].value == {"text": "Line lead"}
    assert run.messages[-1].content == "Thanks. How was onboarding?"


async def test_engine_withholds_follow_up_once_the_cap_is_spent(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    # Answer, then probe. Twice — that exhausts the cap.
    for reply in ("What does that involve?", "Anything else?"):
        llm = FakeLLM(_record("Line lead"), _follow_up(reply))
        run = await ConductEngine(session, llm=llm).handle_message(run.id, "…", participant)
        assert run.current_question_index == 0  # a follow-up must not advance the survey
        assert run.messages[-1].content == reply

    # The third answer lands with the cap spent, so ask_follow_up is no longer offered.
    llm = FakeLLM(_record("Not really"), _move_on())
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "no", participant)

    assert "ask_follow_up" not in llm.offered[-1]
    assert len([a for a in run.answers if a.kind is AnswerKind.follow_up]) == MAX_FOLLOW_UPS
    assert run.current_question_index == 1


async def test_follow_up_beyond_the_cap_is_rejected_in_code(session, participant, published):
    """Even if the model ignores the offered toolset, the engine refuses."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    for _ in range(MAX_FOLLOW_UPS):
        run = await ConductEngine(
            session, llm=FakeLLM(_record("x"), _follow_up("more?"))
        ).handle_message(run.id, "…", participant)

    llm = FakeLLM(_follow_up("one more?"))
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=llm).handle_message(run.id, "no", participant)
    assert llm.calls == 2  # the rejected probe and its one retry


async def test_the_cap_counts_probes_asked_not_answers_recorded(session, participant, published):
    """A participant who never answers a probe must still exhaust the budget.

    The cap is spent when the engine issues a follow-up. Counting recorded answers
    instead would let a model probe forever as long as no reply was ever recorded,
    each probe costing another paid call with the whole transcript resent.
    """
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    # Answer once, then probe — that is probe 1.
    first = FakeLLM(_record("Line lead"), _follow_up("What does that involve?"))
    run = await ConductEngine(session, llm=first).handle_message(run.id, "line lead", participant)

    # Reply evasively; the model probes again without recording anything. Probe 2.
    second = FakeLLM(_follow_up("Could you say a bit more?"))
    run = await ConductEngine(session, llm=second).handle_message(
        run.id, "dunno really", participant
    )

    assert run.probes_asked == {published_question_id(run): MAX_FOLLOW_UPS}
    assert not [a for a in run.answers if a.kind is AnswerKind.follow_up]  # nothing recorded

    # The budget is now spent even though no follow-up answer exists.
    third = FakeLLM(_follow_up("And anything else?"))
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=third).handle_message(run.id, "still dunno", participant)
    assert "ask_follow_up" not in third.offered[-1]


def published_question_id(run):
    """The first question's id, as the engine keys probes_asked by."""
    return next(iter(run.probes_asked))


async def test_invalid_value_retries_once_then_fails_loudly(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    first = FakeLLM(_record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=first).handle_message(run.id, "line lead", participant)
    assert run.current_question_index == 1  # now on the rating question

    llm = FakeLLM(_record("eleven"))  # a rating must be an int 1-5
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=llm).handle_message(run.id, "eleven!", participant)

    assert llm.calls == 2  # one retry, then loud failure
    assert run.current_question_index == 1  # no silent default, no advance
    assert len(run.answers) == 1


async def test_malformed_tool_output_is_rejected(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    garbage = ToolTurn(text="", tool_name="record_answer", tool_input={"nonsense": True})
    llm = FakeLLM(garbage)
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=llm).handle_message(run.id, "hello", participant)
    assert llm.calls == 2


async def test_unknown_tool_name_is_rejected(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    invented = ToolTurn(text="", tool_name="skip_survey", tool_input={})
    llm = FakeLLM(invented)
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=llm).handle_message(run.id, "hello", participant)
    assert llm.calls == 2


async def test_declining_a_question_advances_it(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    decline = ToolTurn(
        text="No problem. How was onboarding?",
        tool_name="flag_unanswerable",
        tool_input={"reason": "participant declined"},
    )
    run = await ConductEngine(session, llm=FakeLLM(decline)).handle_message(
        run.id, "rather not say", participant
    )
    assert run.current_question_index == 1
    assert run.answers[0].value == {"unanswerable": "participant declined"}


async def test_run_completes_after_the_final_question(session, participant, published):
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    first = FakeLLM(_record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=first).handle_message(run.id, "line lead", participant)
    # The rating question forbids follow-ups, so one recorded answer completes the run.
    llm = FakeLLM(_record(4, "Thanks, that's everything."))
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "four", participant)

    assert llm.calls == 1
    assert run.status is RunStatus.completed
    assert run.completed_at is not None
    assert [a.value for a in run.answers if a.kind is AnswerKind.scripted] == [
        {"text": "Line lead"},
        {"rating": 4},
    ]

    with pytest.raises(ConflictError):  # a finished run takes no more messages
        await ConductEngine(session, llm=FakeLLM()).handle_message(run.id, "more", participant)


async def test_republishing_leaves_an_in_flight_run_alone(session, creator, participant, published):
    """A run is bound to the version it started on, so creators can keep editing."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    first = FakeLLM(_record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=first).handle_message(run.id, "line lead", participant)

    # The creator rewrites the survey and republishes while the participant is mid-run.
    svc = TemplateService(session)
    await svc.update_draft(
        published.id,
        TemplateUpdate(
            title="Something else entirely",
            questions=[
                QuestionInput(text="A brand new question", answer_type=AnswerType.long_text)
            ],
        ),
        creator,
    )
    assert (await svc.publish(published.id, creator)).version == 2

    reloaded = ConductEngine(session, llm=FakeLLM())
    live = await reloaded.load(run.id, participant)
    assert [q["text"] for q in await reloaded.questions(live)] == [
        "What's your role?",
        "Rate your onboarding",
    ]
    assert live.current_question_index == 1  # position untouched by the republish

    # And it still completes against v1's questions, not the new ones.
    llm = FakeLLM(_record(4, "Thanks, that's everything."))
    live = await ConductEngine(session, llm=llm).handle_message(live.id, "four", participant)
    assert live.status is RunStatus.completed
    assert [a.value for a in live.answers if a.kind is AnswerKind.scripted] == [
        {"text": "Line lead"},
        {"rating": 4},
    ]


def _decline(reason: str = "participant declined", say: str = "No problem.") -> ToolTurn:
    return ToolTurn(text=say, tool_name="flag_unanswerable", tool_input={"reason": reason})


async def test_declining_a_follow_up_moves_the_survey_on(session, participant, published):
    """Found by a live run: "rather not say" to a probe used to exhaust the turn loop.

    flag_unanswerable was only offered before the scripted answer existed, so once a
    probe was outstanding the model had no way to say the participant had declined.
    """
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    probe = FakeLLM(_record("Line lead"), _follow_up("What does that involve?"))
    run = await ConductEngine(session, llm=probe).handle_message(run.id, "line lead", participant)

    llm = FakeLLM(_decline("would rather not say"))
    run = await ConductEngine(session, llm=llm).handle_message(
        run.id, "rather not say", participant
    )

    assert "flag_unanswerable" in llm.offered[0]  # offered while a probe is outstanding
    assert run.current_question_index == 1  # the survey moved on rather than stalling
    declined = [a for a in run.answers if a.kind is AnswerKind.follow_up]
    assert declined[0].value == {"unanswerable": "would rather not say"}
    assert declined[0].question_text == "What does that involve?"  # the probe, not the question


async def test_only_one_answer_is_recorded_per_participant_message(session, participant, published):
    """Recording neither advances nor spends a probe, so repeats would spin the loop."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    llm = FakeLLM(_record("Line lead"), _record("Line lead again"), _record("and again"))
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=llm).handle_message(run.id, "line lead", participant)

    assert "record_answer" in llm.offered[0]
    assert "record_answer" not in llm.offered[-1]  # withdrawn after the first record


async def test_briefing_dates_the_conversation_and_marks_optional_questions(
    session, creator, participant
):
    """Both found by a live run.

    The model resolved "the 3rd of March this year" against its training data and recorded
    2024-03-03. Separately, asked an optional question it had no way to know was optional,
    it turned "hard to say really" into a rating of 3 rather than letting it go.
    """
    svc = TemplateService(session)
    template = await svc.create_draft(
        TemplateCreate(
            title="Starters",
            questions=[
                QuestionInput(text="When did you start?", answer_type=AnswerType.date),
                QuestionInput(
                    text="Still here in two years?",
                    answer_type=AnswerType.rating,
                    required=False,
                ),
            ],
        ),
        creator,
    )
    await svc.publish(template.id, creator)

    fake = FakeLLM(_record("2026-03-03"), _move_on(), _record(3), _move_on())
    engine = ConductEngine(session, llm=fake)
    run = await engine.start_run(template.id, participant)
    await engine.handle_message(run.id, "3rd of March this year", participant)
    await engine.handle_message(run.id, "hard to say really", participant)

    now = datetime.now(UTC)
    assert f"Today is {now:%A}, {now.date().isoformat()}" in fake.briefings[0]
    assert "- This question is required" in fake.briefings[0]

    optional = fake.briefings[-1]
    assert "Today is" not in optional  # only where it can matter
    assert "This question is OPTIONAL" in optional


async def test_can_probe_before_any_answer_is_recorded(session, participant, published):
    """Found by a live run: the model wrote `"placeholder"` to unlock ask_follow_up.

    Probing used to require a scripted answer to exist, so a model wanting to clarify a
    vague reply had to invent one first. The reply to such a probe is the scripted answer,
    since the probe was asking for the question's own answer.
    """
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    probe = FakeLLM(_follow_up("Which team are you on day to day?"))
    run = await ConductEngine(session, llm=probe).handle_message(run.id, "hard to say", participant)

    assert "ask_follow_up" in probe.offered[0]
    assert not run.answers  # nothing invented to unlock the probe
    assert run.current_question_index == 0
    assert run.messages[-1].content == "Which team are you on day to day?"

    answer = FakeLLM(_record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=answer).handle_message(run.id, "line lead", participant)

    scripted = [a for a in run.answers if a.kind is AnswerKind.scripted]
    assert [a.value for a in scripted] == [{"text": "Line lead"}]
    assert run.current_question_index == 1


async def test_state_survives_a_reload(session, participant, published):
    """Run position lives in the database, not in the model's head or the client's."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    await ConductEngine(session, llm=FakeLLM(_record("Line lead"), _move_on())).handle_message(
        run.id, "line lead", participant
    )

    reloaded = await ConductEngine(session, llm=FakeLLM()).load(run.id, participant)
    assert reloaded.current_question_index == 1
    assert reloaded.status is RunStatus.in_progress
    assert [m.role.value for m in reloaded.messages] == ["assistant", "user", "assistant"]


# ---------------------------------------------------------------- tricky input


async def test_reply_speaks_without_recording_or_advancing(session, participant, published):
    """A participant asking a question back gets an answer, not a fabricated record."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    llm = FakeLLM(_reply("It just means your job title — whatever you'd call it."))
    run = await ConductEngine(session, llm=llm).handle_message(
        run.id, "what do you mean by role?", participant
    )

    assert "reply" in llm.offered[0]
    assert not run.answers
    assert run.current_question_index == 0
    assert run.messages[-1].content == "It just means your job title — whatever you'd call it."


async def test_reply_cap_withholds_the_tool(session, participant, published):
    """Replies are budgeted per question so a chatty model cannot stall the survey."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    for _ in range(MAX_REPLIES):
        run = await ConductEngine(session, llm=FakeLLM(_reply("Sure —"))).handle_message(
            run.id, "wait, what?", participant
        )

    llm = FakeLLM(_record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "line lead", participant)
    assert "reply" not in llm.offered[0]
    assert run.current_question_index == 1  # the survey still moves


async def test_cross_question_tool_call_is_rejected(session, participant, published):
    """Answering a different question than the current one (two answers at once, or a
    revision of an earlier answer) is refused; a placeholder id passes through."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    questions = await ConductEngine(session, llm=FakeLLM()).questions(run)
    other_id = questions[1]["id"]

    cross = ToolTurn(
        text="", tool_name="record_answer", tool_input={"question_id": other_id, "value": "4"}
    )
    llm = FakeLLM(cross)
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=llm).handle_message(run.id, "and I'd say 4", participant)
    assert llm.calls == 2  # rejected, retried, failed loudly
    assert not run.answers

    placeholder = ToolTurn(
        text="", tool_name="record_answer", tool_input={"question_id": "q", "value": "Line lead"}
    )
    run = await ConductEngine(session, llm=FakeLLM(placeholder, _move_on())).handle_message(
        run.id, "line lead", participant
    )
    assert [a.value for a in run.answers] == [{"text": "Line lead"}]


async def test_blank_unanswerable_reason_is_defaulted_not_rejected(session, participant, published):
    """The reason is metadata; a weak model omitting it should not burn the retry."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    bare = ToolTurn(text="", tool_name="flag_unanswerable", tool_input={"question_id": "q"})
    llm = FakeLLM(bare)
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "skip", participant)

    assert llm.calls == 1  # no retry spent
    assert run.current_question_index == 1
    assert run.answers[0].value == {"unanswerable": "participant declined"}


async def test_record_tool_carries_a_typed_value_schema(session, participant, published):
    """Constrained backends get a grammar for the value, not just prose."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    llm = FakeLLM(_record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=llm).handle_message(run.id, "line lead", participant)

    record = next(t for t in llm.tools_seen[0] if t["name"] == "record_answer")
    assert record["input_schema"]["properties"]["value"]["type"] == "string"  # short_text

    rating = FakeLLM(_record(4), _move_on())
    await ConductEngine(session, llm=rating).handle_message(run.id, "a solid 4", participant)
    record = next(t for t in rating.tools_seen[0] if t["name"] == "record_answer")
    value = record["input_schema"]["properties"]["value"]
    assert (value["type"], value["minimum"], value["maximum"]) == ("integer", 1, 5)


async def test_rejection_feedback_reaches_the_model_in_band(session, participant, published):
    """Small models weight the last user message, not a line at the tail of the system
    prompt — so the retry must carry the correction in the transcript itself."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)
    run = await ConductEngine(
        session, llm=FakeLLM(_record("Line lead"), _move_on())
    ).handle_message(run.id, "line lead", participant)

    llm = FakeLLM(_record("eleven"), _record(4), _move_on())
    await ConductEngine(session, llm=llm).handle_message(run.id, "eleven!", participant)

    retry_messages = llm.messages_seen[1]
    assert retry_messages[-1]["role"] == "user"
    assert "[engine]" in retry_messages[-1]["content"]
    assert "rejected" in retry_messages[-1]["content"]


def test_transcript_is_windowed_for_small_contexts():
    """A long run must not replay unbounded history: the briefing restates the current
    question every turn, so only a recent window is needed."""
    run = SimpleNamespace(
        messages=[RunMessage(role=MessageRole.user, content=f"message {i}") for i in range(30)]
    )
    windowed = _transcript(cast("SurveyRun", run))
    assert len(windowed) == TRANSCRIPT_WINDOW + 1
    assert windowed[0]["content"] == "[earlier conversation omitted]"
    assert windowed[-1]["content"] == "message 29"


def test_transcript_always_opens_on_a_user_turn():
    """Anthropic rejects a message list starting with the assistant, and every run opens
    with the engine's greeting — so a run's first few turns 400'd on the primary and fell
    through to a backup. Only the windowed path escaped, its head being a user message."""
    for length in range(1, TRANSCRIPT_WINDOW + 6):
        run = SimpleNamespace(
            messages=[
                RunMessage(
                    role=MessageRole.assistant if i % 2 == 0 else MessageRole.user,
                    content=f"message {i}",
                )
                for i in range(length)
            ]
        )
        sent = _transcript(cast("SurveyRun", run))
        assert sent[0]["role"] == "user", f"{length} stored messages opened on the assistant"

    # The real opening shape: greeting, then the participant's first reply.
    run = SimpleNamespace(
        messages=[
            RunMessage(role=MessageRole.assistant, content="Thanks for taking it. Your role?"),
            RunMessage(role=MessageRole.user, content="Data engineer"),
        ]
    )
    sent = _transcript(cast("SurveyRun", run))
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"].endswith("Your role?")  # the greeting is still replayed


def test_whitespace_only_messages_are_refused_at_the_boundary():
    """A blank message must 422 before it reaches the transcript or burns a model turn."""
    from pydantic import ValidationError

    from app.conduct.schemas import RunMessageRequest

    assert RunMessageRequest(content="  real words  ").content == "real words"
    with pytest.raises(ValidationError):
        RunMessageRequest(content="   ")


async def test_a_chatty_turn_with_no_tool_call_is_retried_once(session, participant, published):
    """A live run died mid-survey on 'Backup LLM returned no tool call' — the model was
    responsive, it just talked instead of acting. That is cheaply retryable with an
    in-band nudge; a second offence still fails loudly."""
    engine = ConductEngine(session, llm=FakeLLM())
    run = await engine.start_run(published.id, participant)

    recovers = FakeLLM(NoToolCallError("no tool"), _record("Line lead"), _move_on())
    run = await ConductEngine(session, llm=recovers).handle_message(
        run.id, "line lead", participant
    )
    assert recovers.calls == 3  # chatted, nudged retry recorded, then moved on
    assert "no tool was called" in recovers.messages_seen[1][-1]["content"]
    assert [a.value for a in run.answers] == [{"text": "Line lead"}]

    stubborn = FakeLLM(NoToolCallError("no tool"), NoToolCallError("still no tool"))
    with pytest.raises(LLMError):
        await ConductEngine(session, llm=stubborn).handle_message(run.id, "4", participant)
    assert stubborn.calls == 2  # one nudge, then loud failure — never an infinite loop


async def test_a_second_turn_is_refused_while_one_is_in_flight(
    engine, session, participant, published
):
    """The lock is the whole mechanism, so test it directly rather than by timing.

    A run holds its row for the length of a turn — which spans a model call — so the
    second arrival is told the run is busy instead of waiting on it.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await session.commit()

    async with AsyncSession(engine, expire_on_commit=False) as holder:
        assert await RunRepository(holder).try_lock(run.id)  # first turn owns the run

        async with AsyncSession(engine, expire_on_commit=False) as second:
            engine_two = ConductEngine(second, llm=FakeLLM(_record("Line lead")))
            with pytest.raises(ConflictError, match="already handling"):
                await engine_two.handle_message(run.id, "line lead", participant)


async def test_racing_messages_never_double_answer_one_question(
    engine, session, participant, published
):
    """A double-clicked send used to leave two scripted answers on one question, and the
    creator's results then read '2 of 2 answered' on a run whose second question was never
    asked. Whoever wins, the run must hold one scripted answer per question."""
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.runs.models import Answer

    run = await ConductEngine(session, llm=FakeLLM()).start_run(published.id, participant)
    await session.commit()

    class SlowLLM:
        """Holds the first turn open long enough for the second request to arrive.

        q0 permits probing, so the engine loops once after recording — hence move_on on
        the second call, exactly as the scripted fakes elsewhere do.
        """

        def __init__(self) -> None:
            self.calls = 0

        async def tool_turn(self, **_):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.05)
                return _record("Line lead")
            return _move_on()

        async def tool_call(self, **_):
            raise AssertionError("unused")

    async def turn(n: int):
        async with AsyncSession(engine, expire_on_commit=False) as s:
            try:
                await ConductEngine(s, llm=SlowLLM()).handle_message(run.id, f"m{n}", participant)
                return "ok"
            except ConflictError:
                return "refused"

    outcomes = await asyncio.gather(turn(1), turn(2))
    assert outcomes.count("refused") == 1, outcomes

    async with AsyncSession(engine, expire_on_commit=False) as s:
        rows = (await s.execute(select(Answer).where(Answer.run_id == run.id))).scalars().all()
    scripted = [a for a in rows if a.kind is AnswerKind.scripted]
    assert len(scripted) == 1
    assert len({a.question_id for a in scripted}) == 1
