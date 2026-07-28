"""Conditional visibility.

A question may carry ``show_when: {question, op, value}``, where ``question`` is the
0-based position of an earlier question. The engine — never the model — decides which
questions a participant sees, exactly as it decides which question is current.

Two rules do most of the work:

- **A condition needs a recorded answer.** If the referenced question was skipped or
  declined, there is nothing to compare against, and the dependent question is hidden.
  Conditions therefore cascade: hiding a question hides anything hanging off it.
- **A decline is not a value.** ``{"unanswerable": ...}`` records that the participant
  would not answer, so it can neither satisfy ``is`` nor ``is_not``. Treating it as
  "not X" would show follow-on questions to someone who declined the premise.
"""

from typing import Any

from app.templates.enums import ShowWhenOp


def _comparable(value: dict[str, Any]) -> list[str] | None:
    """The recorded answer as strings to compare against, or None if there is nothing
    to compare. Multi-select yields every choice, so ``is`` reads as "chose this"."""
    if "unanswerable" in value:
        return None
    if "options" in value:  # multi_select, plus any write-ins alongside it
        chosen = [str(v) for v in value.get("options", [])]
        other = value.get("other") or []
        return chosen + [str(v) for v in (other if isinstance(other, list) else [other])]
    for key in ("text", "option", "other", "date"):
        if key in value:
            return [str(value[key])]
    if "yes_no" in value:
        # Creators write "yes"/"no"; both spellings of the underlying boolean match.
        return ["yes", "true"] if value["yes_no"] else ["no", "false"]
    for key in ("rating", "number"):
        if key in value:
            number = value[key]
            # 4 and 4.0 are the same answer to an creator writing "4".
            forms = {str(number)}
            if isinstance(number, float) and number.is_integer():
                forms.add(str(int(number)))
            return sorted(forms)
    return None


def is_satisfied(condition: dict[str, Any], answer: dict[str, Any] | None) -> bool:
    """Does a recorded answer satisfy this condition?

    Unanswered and undecidable both come back False, for either operator: the engine
    only shows a conditional question when its premise is positively established.
    """
    if answer is None:
        return False
    values = _comparable(answer)
    if values is None:
        return False
    wanted = str(condition.get("value", "")).strip().casefold()
    matched = any(v.strip().casefold() == wanted for v in values)
    return matched if condition.get("op") == ShowWhenOp.is_.value else not matched


def is_visible(
    question: dict[str, Any], answers: dict[str, dict[str, Any]], questions: list[dict[str, Any]]
) -> bool:
    """Should this question be asked, given the answers recorded so far?

    ``answers`` maps question id to its scripted answer value.
    """
    condition = question["show_when"]
    if not condition:
        return True
    position = condition.get("question")
    if not isinstance(position, int) or not 0 <= position < len(questions):
        # The schema refuses this on the way in, so a version carrying it was written
        # by an older build. Show the question rather than silently dropping it: an
        # extra question is recoverable, a missing one is invisible.
        return True
    return is_satisfied(condition, answers.get(questions[position]["id"]))


def next_visible(
    start: int, questions: list[dict[str, Any]], answers: dict[str, dict[str, Any]]
) -> int:
    """The index of the first question at or after ``start`` that should be asked.

    Returns ``len(questions)`` when nothing remains — which the engine reads as the run
    being complete.
    """
    index = start
    while index < len(questions) and not is_visible(questions[index], answers, questions):
        index += 1
    return index


def remaining_possible(
    current: int, questions: list[dict[str, Any]], answers: dict[str, dict[str, Any]]
) -> int:
    """How many questions from ``current`` onward could still be asked.

    A question is ruled out only once its condition is *decided* — the referenced
    question has been answered and disagrees, or was itself skipped. One whose
    reference is still ahead stays counted, so the total the participant sees only ever
    shrinks. A total that grew mid-survey would read as the survey getting longer the
    more you answer.
    """
    total = 0
    for index in range(current, len(questions)):
        question = questions[index]
        condition = question["show_when"]
        if not condition:
            total += 1
            continue
        position = condition.get("question")
        if not isinstance(position, int) or not 0 <= position < len(questions):
            total += 1
            continue
        referenced = questions[position]
        if referenced["id"] not in answers and position >= current:
            total += 1  # still upcoming; the outcome is not known yet
        elif is_satisfied(condition, answers.get(referenced["id"])):
            total += 1
    return total
