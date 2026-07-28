"""The published snapshot is validated once, so no reader has to guess.

The bug these guard: with no ``required`` key, ``engine._briefing`` told the model the
question was required while ``router._to_read`` told the browser it was optional — the
same question, at the same moment, mandatory to the interviewer and skippable to the
participant. Nothing failed; the two halves simply disagreed, quietly.
"""

import uuid

import pytest

from app.conduct.engine import _briefing, _questions_of
from app.errors import ValidationError
from app.templates.snapshot import questions_of


def _snap(**kw: object) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "position": 0,
        "text": "What is your role?",
        "answer_type": "short_text",
        "options": [],
        "allow_other": False,
        "required": True,
        "allow_follow_ups": False,
        **kw,
    }


def test_a_question_missing_required_is_refused_not_guessed() -> None:
    """The exact shape that made the two readers disagree."""
    broken = _snap()
    del broken["required"]
    with pytest.raises(ValidationError) as exc:
        questions_of({"questions": [broken]})
    assert "required" in str(exc.value)


@pytest.mark.parametrize("field", ["id", "text", "answer_type", "options", "allow_other"])
def test_every_other_missing_field_is_refused_too(field: str) -> None:
    """Not a special case for one key — the whole shape is the contract."""
    broken = _snap()
    del broken[field]
    with pytest.raises(ValidationError) as exc:
        questions_of({"questions": [broken]})
    assert field in str(exc.value)


def test_the_error_names_the_field_and_the_question() -> None:
    """A malformed version is a fault to diagnose, so the message must locate it."""
    with pytest.raises(ValidationError) as exc:
        questions_of({"questions": [_snap(), _snap(position=1, answer_type="hologram")]})
    assert "1.answer_type" in str(exc.value)


def test_show_when_is_optional_because_older_versions_predate_it() -> None:
    """The one real default: absence means 'always shown', not a missing value."""
    without = _snap()
    assert "show_when" not in without
    assert questions_of({"questions": [without]})[0]["show_when"] is None


def test_a_definition_with_no_questions_key_fails_loudly() -> None:
    with pytest.raises(ValidationError):
        questions_of({})


def test_a_questions_value_that_is_not_a_list_fails_loudly() -> None:
    with pytest.raises(ValidationError) as exc:
        questions_of({"questions": {"0": _snap()}})
    assert "dict" in str(exc.value)


def test_questions_come_back_in_position_order() -> None:
    """Callers index by position; the stored order is not guaranteed to match."""
    out = questions_of({"questions": [_snap(position=2), _snap(position=0), _snap(position=1)]})
    assert [q["position"] for q in out] == [0, 1, 2]


def test_the_engine_loads_through_the_same_gate() -> None:
    """_questions_of is the conduct engine's only door to a snapshot."""
    broken = _snap()
    del broken["allow_follow_ups"]
    with pytest.raises(ValidationError):
        _questions_of({"questions": [broken]})


def test_a_validated_question_briefs_as_required() -> None:
    """The reader that used to default to True now reads a guaranteed key."""
    questions = questions_of({"questions": [_snap(required=True)]})
    state = {"scripted_recorded": False, "follow_ups_used": 0}
    assert "- This question is required" in _briefing(questions, 0, questions[0], state, None)


def test_a_validated_optional_question_briefs_as_optional() -> None:
    questions = questions_of({"questions": [_snap(required=False)]})
    state = {"scripted_recorded": False, "follow_ups_used": 0}
    assert "OPTIONAL" in _briefing(questions, 0, questions[0], state, None)
