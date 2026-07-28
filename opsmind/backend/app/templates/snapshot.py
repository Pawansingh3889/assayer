"""The read side of the published-snapshot contract.

``service._snapshot`` writes a version's frozen ``definition``; this module reads it.
Both halves live in ``templates`` because ``templates`` owns the shape — ``conduct``
and ``runs`` are consumers of it.

Why a model rather than a default per reader. The snapshot is a plain JSONB document,
so every reader had to decide for itself what a missing key meant, and they did not
agree: with no ``required`` key, ``engine._briefing`` told the model the question was
required while ``router._to_read`` told the browser it was optional — the same question,
at the same moment, mandatory to the interviewer and skippable to the participant. That
is the failure mode this guards against, and there were fourteen such defaults
across six files, each an independent guess.

Validating once, here, removes the guessing rather than settling it. A definition that
does not fit fails loudly at load with a typed error naming the field, and every reader
downstream can subscript freely because the shape is already guaranteed.

``questions_of`` returns dicts, not models, so the engine keeps its existing
``dict[str, Any]`` signatures — the gate is the point, not a rewrite of everything
behind it.
"""

from typing import Any

from pydantic import BaseModel, ValidationError

from app.errors import ValidationError as AppValidationError
from app.templates.enums import AnswerType
from app.templates.schemas import ShowWhen


class SnapshotQuestion(BaseModel):
    """One question as frozen at publish time.

    Every field is required except ``show_when``, which is genuinely optional: it
    arrived with conditional visibility and versions published before it do not carry
    the key. That default is a real one — the absence means "always shown" — not a
    shrug over data that should have been there.
    """

    id: str
    position: int
    text: str
    answer_type: AnswerType
    options: list[str]
    allow_other: bool
    required: bool
    allow_follow_ups: bool
    show_when: ShowWhen | None = None


def questions_of(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Validated, position-sorted questions from a version's ``definition``.

    Raises ``ValidationError`` (422) if the document does not match the contract. A
    published version is immutable, so this can only mean the snapshot was written by
    code that has since changed, or edited outside the application — either way it is
    a fault to surface, not to paper over on every read.
    """
    raw = definition.get("questions")
    if raw is None:
        raise AppValidationError("This published version has no questions list.")
    if not isinstance(raw, list):
        raise AppValidationError(
            f"This published version's questions are a {type(raw).__name__}, not a list."
        )
    questions = []
    for index, question in enumerate(raw):
        try:
            questions.append(SnapshotQuestion.model_validate(question))
        except ValidationError as exc:
            # Index included deliberately: validating question-by-question loses the
            # position Pydantic would have put in ``loc``, and "answer_type is wrong"
            # across a twenty-question survey names a fault without locating it.
            first = exc.errors()[0]
            field = ".".join(str(p) for p in first["loc"])
            raise AppValidationError(
                f"This published version is malformed: question {index}.{field} "
                f"— {first['msg']}."
            ) from exc
    return [q.model_dump(mode="json") for q in sorted(questions, key=lambda q: q.position)]
