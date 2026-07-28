"""The gate every model-supplied answer passes through.

This is where a confused model gets stopped, so each answer type is checked for what it
accepts and, more importantly, what it refuses. No database and no LLM: pure contract.
"""

from typing import Any

import pytest

from app.conduct.validation import AnswerValidationError, validate_answer


def q(answer_type: str, *, options: list[str] | None = None, allow_other: bool = False) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "text": "A question",
        "answer_type": answer_type,
        "options": options or [],
        "allow_other": allow_other,
        "required": True,
        "allow_follow_ups": False,
    }


def rejects(question: dict, value: Any) -> None:
    with pytest.raises(AnswerValidationError):
        validate_answer(question, value)


def test_yes_no_takes_only_booleans():
    assert validate_answer(q("yes_no"), True) == {"yes_no": True}
    assert validate_answer(q("yes_no"), False) == {"yes_no": False}
    rejects(q("yes_no"), "yes")
    rejects(q("yes_no"), 1)


def test_rating_is_a_whole_number_one_to_five():
    assert validate_answer(q("rating"), 4) == {"rating": 4}
    rejects(q("rating"), 0)
    rejects(q("rating"), 6)
    rejects(q("rating"), 4.5)
    rejects(q("rating"), True)  # a bool is an int in Python; it is not a rating


def test_number_takes_int_or_float_but_not_a_bool():
    assert validate_answer(q("number"), 12) == {"number": 12}
    assert validate_answer(q("number"), 1.5) == {"number": 1.5}
    rejects(q("number"), True)


def test_serialization_artifacts_coerce_but_words_do_not():
    """Weak models stringify tool arguments ("4") and emit integral floats (4.0). Those
    are lossless serialization slips, coerced deterministically; natural language stays
    the model's job and is refused."""
    assert validate_answer(q("rating"), "4") == {"rating": 4}
    assert validate_answer(q("rating"), 4.0) == {"rating": 4}
    assert validate_answer(q("number"), "12") == {"number": 12}
    assert validate_answer(q("number"), "1.5") == {"number": 1.5}
    assert validate_answer(q("yes_no"), "true") == {"yes_no": True}
    assert validate_answer(q("yes_no"), "False") == {"yes_no": False}
    rejects(q("rating"), "four")
    rejects(q("rating"), "4.5")
    rejects(q("number"), "a dozen")
    rejects(q("yes_no"), "yep")


@pytest.mark.parametrize("answer_type", ["short_text", "long_text"])
def test_text_must_be_non_empty_and_is_stored_trimmed(answer_type: str):
    assert validate_answer(q(answer_type), "  Line lead  ") == {"text": "Line lead"}
    rejects(q(answer_type), "")
    rejects(q(answer_type), "   ")
    rejects(q(answer_type), 3)


def test_date_must_be_a_real_iso_date():
    assert validate_answer(q("date"), "2026-07-21") == {"date": "2026-07-21"}
    rejects(q("date"), "2026-13-01")
    rejects(q("date"), "21/07/2026")
    rejects(q("date"), "tomorrow")
    rejects(q("date"), 20260721)


def test_date_requires_the_dashed_form_specifically():
    """fromisoformat also accepts compact and week-date forms; storing those
    un-normalised would split the same day across shapes in the results."""
    rejects(q("date"), "20260721")
    rejects(q("date"), "2026-W30-2")


def test_single_select_takes_an_offered_option():
    question = q("single_select", options=["Days", "Nights"])
    assert validate_answer(question, "Nights") == {"option": "Nights"}


def test_select_case_near_miss_lands_on_the_canonical_option():
    """ "days" for option "Days" is the option, not a write-in: storing the canonical
    text keeps the creator's results aggregatable (a live-style miss put it in the
    'other' bucket and fragmented the counts)."""
    single = q("single_select", options=["Days", "Nights"], allow_other=True)
    assert validate_answer(single, "days") == {"option": "Days"}
    assert validate_answer(single, " NIGHTS ") == {"option": "Nights"}
    multi = q("multi_select", options=["Handover", "Rotas"], allow_other=True)
    assert validate_answer(multi, ["rotas", "handover"]) == {"options": ["Rotas", "Handover"]}


def test_single_select_refuses_a_near_miss_when_other_is_not_allowed():
    """The model must map to an offered option, not invent a nearby one."""
    question = q("single_select", options=["Day shift", "Night shift"])
    rejects(question, "nights")
    rejects(question, "Nights")
    rejects(question, 1)


def test_single_select_keeps_a_write_in_when_other_is_allowed():
    question = q("single_select", options=["Days", "Nights"], allow_other=True)
    assert validate_answer(question, "Split shift") == {"other": "Split shift"}


def test_multi_select_takes_a_subset_of_the_options():
    question = q("multi_select", options=["Handover", "Rotas", "Training"])
    assert validate_answer(question, ["Rotas", "Handover"]) == {"options": ["Rotas", "Handover"]}


def test_multi_select_separates_write_ins_from_offered_options():
    question = q("multi_select", options=["Handover", "Rotas"], allow_other=True)
    assert validate_answer(question, ["Rotas", "Parking"]) == {
        "options": ["Rotas"],
        "other": ["Parking"],
    }


def test_multi_select_refuses_unknown_values_and_empty_answers():
    question = q("multi_select", options=["Handover", "Rotas"])
    rejects(question, ["Rotas", "Parking"])
    rejects(question, [])
    rejects(question, "Rotas")
    rejects(question, [1, 2])


def test_number_must_be_finite():
    """NaN satisfies isinstance(raw, float), and float() happily parses "nan" and
    "Infinity"; stored, they poison every average on the results page — and NaN is
    not even legal JSON."""
    rejects(q("number"), float("nan"))
    rejects(q("number"), float("inf"))
    rejects(q("number"), float("-inf"))
    rejects(q("number"), "nan")
    rejects(q("number"), "Infinity")
    rejects(q("number"), "1e999")  # overflows to inf on the string path


def test_numeric_coercion_admits_json_shapes_not_python_ones():
    """int()/float() also parse Python-isms no JSON serializer emits — underscore
    separators, full-width digits. Those are not serialization slips."""
    rejects(q("number"), "4_000")
    rejects(q("rating"), "４")  # full-width digit; int() parses any Unicode decimal
    assert validate_answer(q("rating"), "4.0") == {"rating": 4}  # same info as 4.0
    assert validate_answer(q("number"), "9007199254740993") == {  # 2**53 + 1
        "number": 9007199254740993  # int first: the float path would round it
    }


def test_write_ins_need_text_like_any_other_text_answer():
    """allow_other must not open a hole the text types already close: an empty
    write-in stored as {'other': ''} is an answer with no content."""
    single = q("single_select", options=["Days"], allow_other=True)
    rejects(single, "")
    rejects(single, "   ")
    assert validate_answer(single, "  Split shift ") == {"other": "Split shift"}
    multi = q("multi_select", options=["Email"], allow_other=True)
    rejects(multi, ["Email", ""])
    rejects(multi, ["Email", "   "])


def test_multi_select_counts_each_option_once():
    """["Email", "email"] is Email chosen once; canonicalisation must not turn a
    case slip into a double-counted option in the results."""
    question = q("multi_select", options=["Email", "Chat"])
    assert validate_answer(question, ["Email", "Email"]) == {"options": ["Email"]}
    assert validate_answer(question, ["Email", "email", "Chat"]) == {"options": ["Email", "Chat"]}
    write_ins = q("multi_select", options=["Email"], allow_other=True)
    assert validate_answer(write_ins, ["Fax", "Fax"]) == {"options": [], "other": ["Fax"]}


def test_an_unknown_answer_type_is_refused_rather_than_guessed():
    rejects(q("telepathy"), "anything")
