"""Decoding model-emitted JSON, absorbing the corruptions small models reliably produce.

Shared by every caller that validates a schema-constrained tool call. These repairs are
strictly lossless serialization fixes — a value that does not parse is left untouched so
the schema still rejects real junk loudly.
"""

import json
from typing import Any


def tolerant_json(text: str) -> Any:
    """Parse model-emitted JSON, absorbing the two classic small-model corruptions.

    Strict first. Then ``strict=False``, which permits literal control characters (a
    model writing a question across two lines puts a real newline inside the string —
    invalid in strict JSON). Then repair ``\\'``: escaping an apostrophe is a Python
    habit, never valid JSON, and replacing it with a bare apostrophe is lossless.
    Returns None when nothing parses.
    """
    for candidate in (text, text.replace("\\'", "'")):
        for strict in (True, False):
            try:
                return json.loads(candidate, strict=strict)
            except json.JSONDecodeError:
                continue
    return None


def decode_stringified(value: Any, expected: type) -> Any:
    """Undo one JSON-encoding of a structured field.

    Weaker models frequently emit ``"questions": "[{...}]"`` — the right list, wrapped in
    a string. That is a serialization artifact, not a content problem. Anything that does
    not decode to ``expected`` is returned unchanged, for the validator to reject.
    """
    if not isinstance(value, str):
        return value
    decoded = tolerant_json(value)
    return decoded if isinstance(decoded, expected) else value
