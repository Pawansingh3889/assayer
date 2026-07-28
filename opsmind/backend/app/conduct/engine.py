"""The conduct engine.

Deterministic by construction. The engine decides which question is current, whether the
run is complete, and how many follow-ups remain — every one of those from the database,
never from the model. The model chooses exactly one validated action per turn and phrases
what to say. Nothing reaches the run until it has validated.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.conduct.repository import RunRepository
from app.conduct.validation import AnswerValidationError, validate_answer
from app.errors import ConflictError, ForbiddenError, NotFoundError
from app.llm.base import LLMError, LLMProtocol, NoToolCallError, ToolTurn
from app.llm.factory import get_llm
from app.llm.prompts import load_prompt
from app.runs.enums import AnswerKind, MessageRole, RunStatus
from app.runs.models import REPLY_PREFIX, Answer, RunMessage, SurveyRun
from app.templates.snapshot import questions_of
from app.templates.visibility import next_visible, remaining_possible
from app.users.models import User

logger = logging.getLogger("app.conduct")

MAX_FOLLOW_UPS = 2
MAX_REPLIES = 2  # conversational replies per question (record nothing, advance nothing)
MAX_MODEL_TURNS = 3  # per participant message
TRANSCRIPT_WINDOW = 12  # messages replayed per turn; the briefing restates the question
_REJECTED = "run=%s question=%s tool=%s raw_input=%r raw_text=%r error=%s"
CLOSING_FALLBACK = "That's everything — thank you, your answers are saved."

RECORD = "record_answer"
FOLLOW_UP = "ask_follow_up"
UNANSWERABLE = "flag_unanswerable"
MOVE_ON = "move_on"
REPLY = "reply"


class ConductEngine:
    def __init__(self, session: AsyncSession, llm: LLMProtocol | None = None) -> None:
        self.session = session
        self._llm = llm
        self.repo = RunRepository(session)

    @property
    def llm(self) -> LLMProtocol:
        """Built on first use: starting and reading a run needs no model at all."""
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ---------------------------------------------------------------- lifecycle

    async def start_run(self, template_id: UUID, participant: User) -> SurveyRun:
        version = await self.repo.latest_version(template_id)
        if version is None:
            raise ConflictError("This template has no published version to answer.")
        questions = _questions_of(version.definition)
        if not questions:
            raise ConflictError("The published version has no questions.")

        run = SurveyRun(template_version_id=version.id, participant_id=participant.id)
        run.messages.append(
            RunMessage(
                role=MessageRole.assistant,
                content=_opening_text(version.definition, questions[0]),
            )
        )
        self.repo.add(run)
        await self.session.commit()
        return await self.load(run.id, participant)

    async def load(self, run_id: UUID, participant: User) -> SurveyRun:
        run = await self.repo.get(run_id)
        if run is None:
            raise NotFoundError("Run not found.")
        if run.participant_id != participant.id:
            raise ForbiddenError("This run belongs to another participant.")
        return run

    async def questions(self, run: SurveyRun) -> list[dict[str, Any]]:
        version = await self.repo.get_version(run.template_version_id)
        if version is None:
            raise NotFoundError("The run's template version is missing.")
        return _questions_of(version.definition)

    def progress(self, run: SurveyRun, questions: list[dict[str, Any]]) -> tuple[int, int]:
        """(answered, total) for the participant's progress indicator.

        With conditional visibility the total is not simply the question count: some
        questions will never be asked. It is what has been answered plus what can still
        be asked, so a question ruled out by a condition drops out of the denominator
        and a completed run always reads "n of n".
        """
        answers = _scripted_answers(run)
        answered = len(answers)
        remaining = remaining_possible(run.current_question_index, questions, answers)
        return answered, answered + remaining

    async def resumable(self, participant: User) -> list[tuple[SurveyRun, UUID, str, int, int]]:
        """This participant's unfinished runs, with the progress each one is at."""
        out: list[tuple[SurveyRun, UUID, str, int, int]] = []
        for run, template_id, title in await self.repo.in_progress_for(participant.id):
            version = await self.repo.get_version(run.template_version_id)
            questions = _questions_of(version.definition) if version else []
            answered, total = self.progress(run, questions)
            out.append((run, template_id, title, answered, total))
        return out

    async def handle_message(self, run_id: UUID, content: str, participant: User) -> SurveyRun:
        # One turn at a time per run. Without this, two messages arriving together — a
        # double-clicked send, or a client retrying after a timeout — both read the same
        # current question and the same probe budget, then both write: the run ends up
        # with two scripted answers for one question, and a follow-up cap of 2 can be
        # driven past 2 because the JSONB counter is a read-modify-write.
        if not await self.repo.try_lock(run_id):
            raise ConflictError("This run is already handling a message. Try again in a moment.")
        run = await self.load(run_id, participant)
        if run.status is not RunStatus.in_progress:
            raise ConflictError("This run is already finished.")
        questions = await self.questions(run)

        run.messages.append(RunMessage(role=MessageRole.user, content=content))
        await self.session.flush()

        utterance = await self._turn_loop(run, questions)
        run.messages.append(RunMessage(role=MessageRole.assistant, content=utterance))
        await self.session.commit()
        return await self.load(run_id, participant)

    # ------------------------------------------------------------------- engine

    async def _turn_loop(self, run: SurveyRun, questions: list[dict[str, Any]]) -> str:
        recorded = False
        for _ in range(MAX_MODEL_TURNS):
            question = questions[run.current_question_index]
            state = await self._state(run, question)
            # One participant message yields at most one answer, so recording is
            # withdrawn for the rest of the turn. Without this the model can record
            # repeatedly, and since recording neither advances nor spends a probe the
            # loop runs out of turns on a message that was never invalid.
            state["recorded_this_turn"] = recorded
            tools = _tools_for(question, state)
            turn = await self._decide(run, questions, question, state, tools, None)
            if turn.tool_name == RECORD:
                recorded = True
            utterance = await self._apply(run, questions, question, state, turn)
            if utterance is not None:
                return utterance
        raise LLMError("The model did not settle on an action for this turn.")

    async def _decide(
        self,
        run: SurveyRun,
        questions: list[dict[str, Any]],
        question: dict[str, Any],
        state: dict[str, Any],
        tools: list[dict[str, Any]],
        previous_error: str | None,
    ) -> ToolTurn:
        briefing = _briefing(questions, run.current_question_index, question, state, previous_error)
        messages = _transcript(run)
        if previous_error is not None:
            # Deliver the correction in-band too: small models weight the last user
            # message far above a line buried at the tail of the system prompt.
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        f"[engine] Your previous tool call was rejected: {previous_error}. "
                        "Choose again — call exactly one tool."
                    ),
                },
            ]
        try:
            turn = await self.llm.tool_turn(
                system=load_prompt("conduct_v2") + "\n\n" + briefing,
                messages=messages,
                tools=tools,
            )
        except NoToolCallError:
            # The model chatted instead of acting — responsive but off-script, so one
            # nudged retry is cheap. Timeouts and transport failures deliberately do
            # NOT retry here: doubling a 120-second wait helps nobody.
            if previous_error is not None:
                raise
            logger.warning("model returned no tool call, retrying: run=%s", run.id)
            return await self._decide(
                run,
                questions,
                question,
                state,
                tools,
                "no tool was called — you must call exactly one of the offered tools",
            )
        error = _rejection(question, state, tools, turn)
        if error is None:
            return turn
        if previous_error is None:
            logger.warning(
                "model action rejected, retrying: " + _REJECTED,
                run.id,
                question["id"],
                turn.tool_name,
                turn.tool_input,
                turn.text,
                error,
            )
            return await self._decide(run, questions, question, state, tools, error)
        # Second failure: the raw output is the only thing that explains why, so it is
        # logged before the turn fails.
        logger.error(
            "conduct turn failed after one retry: " + _REJECTED,
            run.id,
            question["id"],
            turn.tool_name,
            turn.tool_input,
            turn.text,
            error,
        )
        raise LLMError(f"Model produced an invalid action after one retry: {error}")

    async def _apply(
        self,
        run: SurveyRun,
        questions: list[dict[str, Any]],
        question: dict[str, Any],
        state: dict[str, Any],
        turn: ToolTurn,
    ) -> str | None:
        """Apply a validated action. Returns the assistant's utterance, or None to loop."""
        if turn.tool_name == FOLLOW_UP:
            # Spend the budget here, when the probe is issued. Reassigned rather than
            # mutated so SQLAlchemy sees the change to the JSONB column.
            key = question["id"]
            run.probes_asked = {**run.probes_asked, key: run.probes_asked.get(key, 0) + 1}
            await self.session.flush()
            return str(turn.tool_input["follow_up_text"]).strip()

        if turn.tool_name == REPLY:
            # Speaking costs a reply from the per-question cap but records nothing and
            # never advances — the current question stays current.
            key = f"{REPLY_PREFIX}{question['id']}"
            run.probes_asked = {**run.probes_asked, key: run.probes_asked.get(key, 0) + 1}
            await self.session.flush()
            return str(turn.tool_input["reply_text"]).strip()

        if turn.tool_name == RECORD:
            scripted = not state["scripted_recorded"]
            run.answers.append(
                Answer(
                    question_id=UUID(question["id"]),
                    kind=AnswerKind.scripted if scripted else AnswerKind.follow_up,
                    question_text=question["text"] if scripted else _last_assistant(run),
                    value=validate_answer(question, turn.tool_input["value"]),
                    answered_by=run.participant_id,
                )
            )
            await self.session.flush()
            if _may_probe(question, state["follow_ups_used"]):
                return None  # let the model decide: probe again, or move on
            return self._advance(run, questions, turn)

        if turn.tool_name == UNANSWERABLE:
            # Declining the question itself is a scripted answer; declining a probe is a
            # follow-up answer, and carries the wording the model invented for it.
            # A blank reason is pure metadata — default it rather than spend the retry.
            declined_probe = state["scripted_recorded"]
            reason = str(turn.tool_input.get("reason", "")).strip() or "participant declined"
            run.answers.append(
                Answer(
                    question_id=UUID(question["id"]),
                    kind=AnswerKind.follow_up if declined_probe else AnswerKind.scripted,
                    question_text=_last_assistant(run) if declined_probe else question["text"],
                    value={"unanswerable": reason},
                    answered_by=run.participant_id,
                )
            )
            await self.session.flush()
            return self._advance(run, questions, turn)

        return self._advance(run, questions, turn)  # move_on

    def _advance(self, run: SurveyRun, questions: list[dict[str, Any]], turn: ToolTurn) -> str:
        # Skip anything whose show_when condition is not satisfied by what has actually
        # been recorded. Deciding this in code, from the database, is the same rule that
        # governs which question is current: the model never gets a say in it.
        nxt = run.current_question_index + 1
        run.current_question_index = next_visible(nxt, questions, _scripted_answers(run))
        # The model wrote its closing line for the question it expected to come next. If
        # the engine has skipped past that one, those words describe a question nobody
        # will be asked, so they are replaced rather than spoken.
        skipped = run.current_question_index != nxt
        if run.current_question_index >= len(questions):
            run.status = RunStatus.completed
            run.completed_at = datetime.now(UTC)
            return CLOSING_FALLBACK if skipped else (turn.text or CLOSING_FALLBACK)
        asking = str(questions[run.current_question_index]["text"])
        return asking if skipped else (turn.text or asking)

    async def _state(self, run: SurveyRun, question: dict[str, Any]) -> dict[str, Any]:
        scripted = await self.repo.count_answers(run.id, UUID(question["id"]), AnswerKind.scripted)
        return {
            "scripted_recorded": scripted > 0,
            "follow_ups_used": run.probes_asked.get(question["id"], 0),
            # Replies share the probes JSONB under a prefixed key — same lifecycle,
            # no schema change, and question ids (UUIDs) can never collide with it.
            "replies_used": run.probes_asked.get(f"{REPLY_PREFIX}{question['id']}", 0),
        }


# ------------------------------------------------------------------- helpers


def _scripted_answers(run: SurveyRun) -> dict[str, dict[str, Any]]:
    """Each question's scripted answer, keyed by question id — what conditions read."""
    return {str(a.question_id): a.value for a in run.answers if a.kind is AnswerKind.scripted}


def _questions_of(definition: dict[str, Any]) -> list[dict[str, Any]]:
    # Validated at the boundary (see templates/snapshot.py), so every reader below can
    # subscript a snapshot question instead of deciding for itself what a missing key
    # would have meant.
    return questions_of(definition)


def _may_probe(question: dict[str, Any], follow_ups_used: int) -> bool:
    return bool(question["allow_follow_ups"]) and follow_ups_used < MAX_FOLLOW_UPS


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _opening_text(definition: dict[str, Any], first: dict[str, Any]) -> str:
    return f"Thanks for taking {definition['title']}. {first['text']}"


def _last_assistant(run: SurveyRun) -> str:
    for message in reversed(run.messages):
        if message.role is MessageRole.assistant:
            return message.content
    return "Follow-up"


def _transcript(run: SurveyRun) -> list[dict[str, str]]:
    """The last TRANSCRIPT_WINDOW messages, always opening on a user turn.

    Windowing is safe by construction: the briefing restates the current question, type,
    options, and budgets every turn, so distant history is never needed to act — and an
    unbounded replay overflows the small context of a local backup model long before a
    survey ends.

    The leading user turn is not cosmetic. Anthropic rejects a message list that starts
    with the assistant, and every run starts with the engine's opening question — so
    each of a run's first few turns 400'd on the primary and quietly fell through to a
    backup. Only the windowed path was safe, because its own head is a user message.
    """
    messages = [
        {"role": "assistant" if m.role is MessageRole.assistant else "user", "content": m.content}
        for m in run.messages
    ]
    if len(messages) > TRANSCRIPT_WINDOW:
        head: dict[str, str] = {"role": "user", "content": "[earlier conversation omitted]"}
        messages = [head, *messages[-TRANSCRIPT_WINDOW:]]
    if messages and messages[0]["role"] != "user":
        messages = [{"role": "user", "content": "[survey started]"}, *messages]
    return messages


def _value_schema(question: dict[str, Any]) -> dict[str, Any]:
    """A typed JSON schema for the answer value, so constrained backends get a grammar
    and weak models see the exact shape instead of guessing from prose."""
    answer_type = question["answer_type"]
    options: list[str] = question["options"]
    if answer_type == "rating":
        return {"type": "integer", "minimum": 1, "maximum": 5}
    if answer_type == "yes_no":
        return {"type": "boolean"}
    if answer_type == "number":
        return {"type": "number"}
    if answer_type == "date":
        return {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    if answer_type == "single_select":
        if options and not question["allow_other"]:
            return {"type": "string", "enum": options}
        return {"type": "string"}
    if answer_type == "multi_select":
        return {"type": "array", "items": {"type": "string"}, "minItems": 1}
    return {"type": "string"}  # short_text / long_text


def _tools_for(question: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Only offer the actions that are legal right now — the engine's first gate."""
    question_id = {"type": "string", "description": "The current question's id."}
    tools: list[dict[str, Any]] = []
    if not state.get("recorded_this_turn"):
        value_schema = {
            "description": "The answer, shaped for the question's type.",
            **_value_schema(question),
        }
        tools.append(
            {
                "name": RECORD,
                "description": "Record the participant's answer to what you just asked.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question_id": question_id,
                        "value": value_schema,
                    },
                    "required": ["question_id", "value"],
                },
            }
        )
    # Probing is not gated on an answer existing. Requiring one first meant a model that
    # wanted to clarify a vague reply had to record something to unlock the tool, and it
    # duly invented plausible values to get there. The cap still bounds it.
    if _may_probe(question, state["follow_ups_used"]):
        tools.append(
            {
                "name": FOLLOW_UP,
                "description": (
                    "Ask one short follow-up: to probe an answer just given, or to ask "
                    "plainly for an answer the reply did not contain."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question_id": question_id,
                        "follow_up_text": {"type": "string"},
                    },
                    "required": ["question_id", "follow_up_text"],
                },
            }
        )
    if state["scripted_recorded"]:
        tools.append(
            {
                "name": MOVE_ON,
                "description": "Nothing worth probing; go to the next question.",
                "input_schema": {
                    "type": "object",
                    "properties": {"question_id": question_id},
                    "required": ["question_id"],
                },
            }
        )
    # A participant sometimes asks a question back ("what do you mean by onboarding?")
    # or is plainly confused. Without a way to just speak, the model's only legal moves
    # are to record something (fabrication) or flag the question unanswerable (wrongly
    # giving up). Replying records nothing and does not advance; a per-question cap
    # keeps a stalling model from chatting instead of surveying.
    if state.get("replies_used", 0) < MAX_REPLIES:
        tools.append(
            {
                "name": REPLY,
                "description": (
                    "Answer the participant's own question or clear up their confusion, "
                    "then restate the current survey question. Records nothing."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question_id": question_id,
                        "reply_text": {"type": "string"},
                    },
                    "required": ["question_id", "reply_text"],
                },
            }
        )
    # Always available: a participant can decline the question itself, and equally can
    # decline a follow-up. Offering this only before the scripted answer left the model
    # with no way to say "they declined" once a probe was outstanding.
    tools.append(
        {
            "name": UNANSWERABLE,
            "description": (
                "The participant declined or cannot answer what you just asked, whether "
                "that was the question itself or a follow-up. The survey moves on."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"question_id": question_id, "reason": {"type": "string"}},
                "required": ["question_id", "reason"],
            },
        }
    )
    return tools


def _rejection(
    question: dict[str, Any],
    state: dict[str, Any],
    tools: list[dict[str, Any]],
    turn: ToolTurn,
) -> str | None:
    """Second gate: re-check the chosen action in code, whatever was offered."""
    allowed = {t["name"] for t in tools}
    if turn.tool_name not in allowed:
        return f"'{turn.tool_name}' is not available now; choose one of {sorted(allowed)}"

    # A tool aimed at a different question is the model answering something the engine
    # did not ask — two answers at once, or revising an earlier answer. Only a real id
    # counts as aiming: models that echo a placeholder ("q") are targeting the current
    # question and pass through, exactly as the engine will apply it.
    sent_id = str(turn.tool_input.get("question_id", "")).strip()
    if sent_id and sent_id != str(question["id"]) and _is_uuid(sent_id):
        return (
            f"only the current question ({question['id']}) can be acted on; earlier "
            "answers cannot be changed — acknowledge and continue with the current question"
        )

    if turn.tool_name == FOLLOW_UP:
        if not _may_probe(question, state["follow_ups_used"]):
            return "follow-ups are not permitted for this question, or the limit is spent"
        if not str(turn.tool_input.get("follow_up_text", "")).strip():
            return "follow_up_text must not be empty"
        return None

    if turn.tool_name == REPLY:
        if not str(turn.tool_input.get("reply_text", "")).strip():
            return "reply_text must not be empty"
        return None

    if turn.tool_name == RECORD:
        if "value" not in turn.tool_input:
            return "record_answer requires a value"
        try:
            validate_answer(question, turn.tool_input["value"])
        except AnswerValidationError as exc:
            return exc.message
        return None

    return None


def _briefing(
    questions: list[dict[str, Any]],
    index: int,
    question: dict[str, Any],
    state: dict[str, Any],
    previous_error: str | None,
) -> str:
    nxt = questions[index + 1]["text"] if index + 1 < len(questions) else None
    lines = [
        "ENGINE STATE (authoritative — do not contradict it):",
        f"- Question {index + 1} of {len(questions)}: {question['text']}",
        f"- Question id: {question['id']}",
        f"- Answer type: {question['answer_type']}",
    ]
    if question["options"]:
        lines.append(f"- Options: {question['options']}")
        lines.append(f"- Free-text 'other' allowed: {question['allow_other']}")
    if question["answer_type"] == "date":
        # Without this the model has no clock, and resolves "this year" against its
        # training data. A live run turned "the 3rd of March this year" into 2024-03-03.
        # The weekday is included because relative dates ("next Tuesday") need it, and
        # weekday arithmetic is exactly where small models slip.
        now = datetime.now(UTC)
        lines.append(f"- Today is {now:%A}, {now.date().isoformat()}")
    lines.append(
        "- This question is required"
        if question["required"]
        else "- This question is OPTIONAL: if they deflect, let it go rather than pressing"
    )
    lines.append(f"- Answer already recorded: {state['scripted_recorded']}")
    lines.append(
        f"- Follow-ups asked so far: {state['follow_ups_used']} of {MAX_FOLLOW_UPS}"
        if question["allow_follow_ups"]
        else "- Follow-ups: not permitted for this question"
    )
    lines.append(
        f"- Next question: {nxt}" if nxt else "- This is the final question; close warmly."
    )
    if previous_error:
        lines.append(f"- Your previous tool call was REJECTED: {previous_error}. Choose again.")
    return "\n".join(lines)
