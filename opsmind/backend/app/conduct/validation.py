"""Validate a model-supplied answer against the question's answer type.

This is the engine's gate: nothing reaches the database until it validates here, so a
confused model can never corrupt a run.
"""

import math
import re
from datetime import date
from typing import Any

from app.errors import AppError


class AnswerValidationError(AppError):
    status_code = 422
    code = "answer_invalid"


# What a JSON serializer can actually emit for a number. int()/float() alone are too
# permissive as a gate: they also parse Python-isms no serializer produces — "4_000",
# "nan", "Infinity", full-width digits ("４") — which would then sail through the type
# checks below wearing a numeric disguise. ASCII flag because \d otherwise matches any
# Unicode decimal digit.
_JSON_NUMBER = re.compile(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?", re.ASCII)


def _coerce(answer_type: str, raw: Any) -> Any:
    """Undo pure serialization artifacts, never semantic guesses.

    Weak models habitually stringify tool arguments ('"4"' for 4, '"true"' for true)
    or emit integral floats (4.0). Those carry the exact same information as the typed
    value, so coercing them is lossless. Natural language ("four", "yes") stays
    rejected — mapping words to values is the model's job, checked by the gate below.
    """
    if answer_type in ("rating", "number") and isinstance(raw, str):
        text = raw.strip()
        if _JSON_NUMBER.fullmatch(text):
            try:
                raw = int(text)  # int first: float would round 2**53+1
            except ValueError:
                parsed = float(text)
                # Fall through so "4.0" gets the same integral-float rule as 4.0.
                raw = int(parsed) if parsed.is_integer() else parsed
    if answer_type == "rating" and isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if answer_type == "yes_no" and isinstance(raw, str):
        lowered = raw.strip().casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return raw


def _canonical_option(raw: str, options: list[str]) -> str | None:
    """Case/whitespace-insensitive match to an option, returning its canonical text."""
    key = raw.strip().casefold()
    for option in options:
        if option.strip().casefold() == key:
            return option
    return None


def validate_answer(question: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Return the normalised value to store, or raise AnswerValidationError."""
    answer_type = question["answer_type"]
    options: list[str] = question["options"]
    allow_other = question["allow_other"]
    raw = _coerce(answer_type, raw)

    if answer_type == "yes_no":
        if not isinstance(raw, bool):
            raise AnswerValidationError("yes_no expects true or false")
        return {"yes_no": raw}

    if answer_type == "rating":
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 5:
            raise AnswerValidationError("rating expects a whole number from 1 to 5")
        return {"rating": raw}

    if answer_type == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise AnswerValidationError("number expects a numeric value")
        # NaN and infinity pass the isinstance check; stored, they poison every
        # average on the results page (and NaN is not even legal JSON).
        if isinstance(raw, float) and not math.isfinite(raw):
            raise AnswerValidationError("number must be finite")
        return {"number": raw}

    if answer_type in ("short_text", "long_text"):
        if not isinstance(raw, str) or not raw.strip():
            raise AnswerValidationError(f"{answer_type} expects non-empty text")
        return {"text": raw.strip()}

    if answer_type == "date":
        if not isinstance(raw, str):
            raise AnswerValidationError("date expects an ISO YYYY-MM-DD string")
        # Enforce the dashed form specifically: fromisoformat also accepts compact
        # ("20260303") and week-date ("2026-W10-2") forms, which would fragment the
        # same day across shapes in the results.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise AnswerValidationError("date must be a valid YYYY-MM-DD string")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise AnswerValidationError("date must be a valid YYYY-MM-DD string") from exc
        return {"date": parsed.isoformat()}

    if answer_type == "single_select":
        if not isinstance(raw, str):
            raise AnswerValidationError("single_select expects the option text")
        # A case/whitespace near-miss ("days" for "Days") is the option, not a write-in;
        # matching it canonically keeps the creator's results aggregatable.
        canonical = _canonical_option(raw, options)
        if canonical is not None:
            return {"option": canonical}
        if allow_other:
            # A write-in is text: same non-empty-and-trimmed rule the text answers enforce.
            write_in = raw.strip()
            if not write_in:
                raise AnswerValidationError("a write-in answer needs text")
            return {"other": write_in}
        raise AnswerValidationError(f"'{raw}' is not one of {options} and 'other' is not allowed")

    if answer_type == "multi_select":
        if not isinstance(raw, list) or not raw:
            raise AnswerValidationError("multi_select expects a non-empty list of option texts")
        chosen: list[str] = []
        other: list[str] = []
        for value in raw:
            if not isinstance(value, str):
                raise AnswerValidationError("multi_select values must be strings")
            canonical = _canonical_option(value, options)
            if canonical is not None:
                # ["Email", "email"] is Email chosen once; canonicalising and then
                # storing both would double-count the option in the results.
                if canonical not in chosen:
                    chosen.append(canonical)
            elif allow_other:
                write_in = value.strip()
                if not write_in:
                    raise AnswerValidationError("a write-in answer needs text")
                if write_in not in other:
                    other.append(write_in)
            else:
                raise AnswerValidationError(
                    f"'{value}' is not one of {options} and 'other' is not allowed"
                )
        result: dict[str, Any] = {"options": chosen}
        if other:
            result["other"] = other
        return result

    raise AnswerValidationError(f"unsupported answer type '{answer_type}'")
