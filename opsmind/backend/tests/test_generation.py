"""Generation tests with the LLM mocked at the client boundary (no API key needed)."""

from typing import Any

import pytest

from app.errors import NotFoundError
from app.llm.base import LLMError, ToolTurn
from app.templates.enums import TemplateStatus
from app.templates.generation import GenerationService


class FakeLLM:
    """Returns canned tool payloads in order (repeating the last), each wrapped in a
    ToolTurn with the given note — mirroring the real ``tool_turn`` the service now uses."""

    def __init__(self, *payloads: dict[str, Any], note: str = "Drafted it.") -> None:
        self._payloads = list(payloads)
        self._note = note
        self.calls = 0

    async def tool_turn(self, **_: Any) -> ToolTurn:
        payload = self._payloads[min(self.calls, len(self._payloads) - 1)]
        self.calls += 1
        return ToolTurn(text=self._note, tool_name="draft_survey_template", tool_input=payload)


_VALID: dict[str, Any] = {
    "title": "Onboarding",
    "description": "New starter survey",
    "questions": [
        {"text": "Your role?", "answer_type": "short_text"},
        {"text": "Systems used?", "answer_type": "multi_select", "options": ["ERP", "BI"]},
    ],
}
# A single-select with no options is schema-valid but fails our business validation.
_INVALID: dict[str, Any] = {
    "title": "X",
    "questions": [{"text": "q", "answer_type": "single_select"}],
}


async def test_generate_persists_valid_draft(session, creator):
    fake = FakeLLM(_VALID)
    template, _ = await GenerationService(session, llm=fake).generate_draft("onboarding", creator)
    assert fake.calls == 1
    assert template.title == "Onboarding"
    assert template.status is TemplateStatus.draft
    assert [q.text for q in template.questions] == ["Your role?", "Systems used?"]


async def test_generate_returns_the_models_note(session, creator):
    fake = FakeLLM(_VALID, note="Added a systems question to see what staff actually use.")
    _, note = await GenerationService(session, llm=fake).generate_draft("onboarding", creator)
    assert note == "Added a systems question to see what staff actually use."


async def test_note_from_the_tool_field_is_preferred_over_spoken_text(session, creator):
    """A forced tool call suppresses prose, so the note rides in the schema's note field;
    it's read from there (and ignored by template validation)."""
    payload = {**_VALID, "note": "Kept it to two quick questions."}
    _, note = await GenerationService(session, llm=FakeLLM(payload, note="")).generate_draft(
        "x", creator
    )
    assert note == "Kept it to two quick questions."


async def test_catch_all_options_become_a_write_in(session, creator):
    """A live run recorded `{'option': 'Other'}` and lost the participant's actual team."""
    fake = FakeLLM(
        {
            "title": "Onboarding",
            "questions": [
                {
                    "text": "Which team?",
                    "answer_type": "single_select",
                    "options": ["Sales", "Engineering", "Other"],
                },
                {
                    "text": "Which tools?",
                    "answer_type": "multi_select",
                    "options": ["ERP", "BI", "None of the above", "Prefer not to say"],
                },
                {
                    "text": "Which site?",
                    "answer_type": "single_select",
                    "options": ["Hull", "Leeds"],
                },
            ],
        }
    )
    template, _ = await GenerationService(session, llm=fake).generate_draft("teams", creator)

    team, tools, site = template.questions
    assert (team.options, team.allow_other) == (["Sales", "Engineering"], True)
    assert (tools.options, tools.allow_other) == (["ERP", "BI"], True)
    # Untouched: nothing to strip, so no write-in is silently opened up.
    assert (site.options, site.allow_other) == (["Hull", "Leeds"], False)


async def test_a_select_of_only_catch_alls_keeps_them_rather_than_emptying(session, creator):
    """Stripping every option would leave a select with none — which the schema refuses on
    the way in, but the strip runs after validation and is never re-checked. The engine
    would then offer the question with no enum, quietly turning it into free text."""
    fake = FakeLLM(
        {
            "title": "Onboarding",
            "questions": [
                {
                    "text": "Which team?",
                    "answer_type": "single_select",
                    "options": ["Other", "N/A", "Prefer not to say"],
                },
                {
                    "text": "Which tools?",
                    "answer_type": "multi_select",
                    "options": ["None of the above", "Not applicable"],
                },
            ],
        }
    )
    template, _ = await GenerationService(session, llm=fake).generate_draft("teams", creator)

    team, tools = template.questions
    assert team.options == ["Other", "N/A", "Prefer not to say"]
    assert tools.options == ["None of the above", "Not applicable"]
    assert [q.allow_other for q in (team, tools)] == [False, False]


async def test_generate_retries_once_then_succeeds(session, creator):
    fake = FakeLLM(_INVALID, _VALID)
    template, _ = await GenerationService(session, llm=fake).generate_draft("x", creator)
    assert fake.calls == 2
    assert template.title == "Onboarding"


async def test_generate_fails_loudly_after_retry(session, creator):
    fake = FakeLLM(_INVALID, _INVALID)
    with pytest.raises(LLMError):
        await GenerationService(session, llm=fake).generate_draft("x", creator)
    assert fake.calls == 2


async def test_a_title_with_no_questions_is_rejected_not_persisted(session, creator):
    """A weaker model can return a schema-valid but empty tool call: a title, no
    questions. Caught live from a free auto-routed backup model — schema-valid,
    useless, and previously created a survey with nothing to answer."""
    empty = {"title": "Team Retrospective Survey", "questions": []}
    fake = FakeLLM(empty, empty)
    with pytest.raises(LLMError):
        await GenerationService(session, llm=fake).generate_draft("x", creator)
    assert fake.calls == 2  # one retry, then loud failure


async def test_stringified_questions_are_decoded_before_validation(session, creator):
    """Small backup models emit the right structure JSON-encoded into a string
    ('"questions": "[{...}]"'). That is a serialization slip, not bad content —
    decode it instead of burning the retry (a live run 502'd on exactly this)."""
    import json

    stringified = {
        "title": "Onboarding",
        "description": "",
        "questions": json.dumps(
            [
                {"text": "Your role?", "answer_type": "short_text"},
                {
                    "text": "Which shift?",
                    "answer_type": "single_select",
                    # the same slip one level down
                    "options": json.dumps(["Days", "Nights"]),
                },
            ]
        ),
    }
    fake = FakeLLM(stringified)
    template, _ = await GenerationService(session, llm=fake).generate_draft("onboarding", creator)

    assert fake.calls == 1  # repaired, not retried
    assert [q.text for q in template.questions] == ["Your role?", "Which shift?"]
    assert template.questions[1].options == ["Days", "Nights"]


async def test_almost_json_with_model_corruptions_is_still_decoded(session, creator):
    """Two classic small-model corruptions of an otherwise-correct encoding must not
    defeat the repair: a literal newline inside a string value (invalid in strict
    JSON) and the Python-style \\' escape (never valid JSON). A live run failed on a
    stringified list that plain json.loads refused."""
    with_newline = '[{"text": "How often do you\nreview dashboards?", "answer_type": "short_text"}]'
    escaped_quote = (
        '[{"text": "Does the platform\\\'s feature set meet your needs?",'
        ' "answer_type": "yes_no"}]'
    )

    first, _ = await GenerationService(
        session, llm=FakeLLM({"title": "A", "questions": with_newline})
    ).generate_draft("a", creator)
    assert first.questions[0].text == "How often do you\nreview dashboards?"

    second, _ = await GenerationService(
        session, llm=FakeLLM({"title": "B", "questions": escaped_quote})
    ).generate_draft("b", creator)
    assert second.questions[0].text == "Does the platform's feature set meet your needs?"


async def test_a_string_that_is_not_json_still_fails_loudly(session, creator):
    """The repair only undoes a clean JSON encoding; real junk keeps failing."""
    junk = {"title": "X", "questions": "just some prose, not a list"}
    fake = FakeLLM(junk, junk)
    with pytest.raises(LLMError):
        await GenerationService(session, llm=fake).generate_draft("x", creator)
    assert fake.calls == 2  # one retry, then loud failure


async def test_options_on_non_select_questions_are_dropped_not_fatal(session, creator):
    """A live run failed 16 validations because the model decorated rating questions
    with options [1, 2, 3, 4, 5]. Options mean nothing off the select types, so they
    are dropped instead of burning the retry."""
    decorated = {
        "title": "Engagement",
        "questions": [
            {"text": "How satisfied are you?", "answer_type": "rating", "options": [1, 2, 3, 4, 5]},
            {"text": "Which team?", "answer_type": "single_select", "options": ["A", "B"]},
        ],
    }
    fake = FakeLLM(decorated)
    template, _ = await GenerationService(session, llm=fake).generate_draft("engagement", creator)

    assert fake.calls == 1  # repaired, not retried
    assert template.questions[0].options == []
    assert template.questions[1].options == ["A", "B"]  # select options untouched


# --- follow-up refinement ------------------------------------------------------


async def test_refine_updates_the_draft_in_place_and_returns_a_note(session, creator):
    original, _ = await GenerationService(session, llm=FakeLLM(_VALID)).generate_draft("x", creator)

    revised = {
        "title": "Onboarding (short)",
        "questions": [{"text": "Your role?", "answer_type": "short_text"}],
    }
    fake = FakeLLM(revised, note="Trimmed it to a single question.")
    updated, note = await GenerationService(session, llm=fake).refine_draft(
        original.id, "make it shorter", creator
    )

    assert updated.id == original.id  # same draft, revised in place
    assert updated.title == "Onboarding (short)"
    assert [q.text for q in updated.questions] == ["Your role?"]
    assert note == "Trimmed it to a single question."


async def test_refine_re_validates_so_a_bad_change_fails_loudly(session, creator):
    original, _ = await GenerationService(session, llm=FakeLLM(_VALID)).generate_draft("x", creator)

    fake = FakeLLM(_INVALID, _INVALID)  # every attempt invalid
    with pytest.raises(LLMError):
        await GenerationService(session, llm=fake).refine_draft(original.id, "break it", creator)


async def test_refine_refuses_another_creators_template(session, creator, other_creator):
    original, _ = await GenerationService(session, llm=FakeLLM(_VALID)).generate_draft("x", creator)

    with pytest.raises(NotFoundError):
        await GenerationService(session, llm=FakeLLM(_VALID)).refine_draft(
            original.id, "change it", other_creator
        )
