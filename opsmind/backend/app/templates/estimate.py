"""Estimated completion time.

Shown before a participant starts, so they can judge whether to begin now. Computed
from the question count and types — a rating is a tap, a long-text answer is a
paragraph, and pretending otherwise makes the number worse than useless.

Deliberately coarse. This is an estimate offered up front, not a promise: follow-ups
can add turns and conditional questions can remove them, so a precise-looking figure
would be false precision.
"""

from typing import Any

from app.templates.enums import AnswerType

# Rough seconds to read the question and answer it conversationally.
SECONDS: dict[AnswerType, int] = {
    AnswerType.yes_no: 8,
    AnswerType.rating: 8,
    AnswerType.single_select: 12,
    AnswerType.multi_select: 18,
    AnswerType.number: 12,
    AnswerType.date: 15,
    AnswerType.short_text: 20,
    AnswerType.long_text: 45,
}
DEFAULT_SECONDS = 20

# Follow-ups are capped per question but only fire when an answer merits probing, so a
# flat multiplier over the questions that permit them is closer than assuming none.
FOLLOW_UP_SECONDS = 15


def estimated_minutes(questions: list[dict[str, Any]]) -> int:
    """Whole minutes, rounded up, minimum 1 — "under a minute" reads as "no time at
    all" and a survey is never that."""
    total = 0
    for question in questions:
        try:
            answer_type = AnswerType(question["answer_type"])
        except ValueError:
            total += DEFAULT_SECONDS  # an unknown type is still a question to answer
            continue
        total += SECONDS.get(answer_type, DEFAULT_SECONDS)
        if question["allow_follow_ups"]:
            total += FOLLOW_UP_SECONDS
    return max(1, -(-total // 60))
