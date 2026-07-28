"""Controlled vocabularies for runs, answers, and transcript messages."""

import enum


class RunStatus(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"
    abandoned = "abandoned"


class AnswerKind(str, enum.Enum):
    scripted = "scripted"
    follow_up = "follow_up"


class MessageRole(str, enum.Enum):
    assistant = "assistant"
    user = "user"
