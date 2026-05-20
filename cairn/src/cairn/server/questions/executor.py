from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cairn.server.models import RemoteSessionProvenance
from cairn.server.questions.models import QuestionThread
from cairn.server.transcripts.models import TranscriptEvent


class QuestionExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class QuestionExecution:
    answer_text: str
    events: list[TranscriptEvent]
    question_session: RemoteSessionProvenance | None = None


class QuestionExecutor(Protocol):
    def execute(self, thread: QuestionThread, message: str) -> QuestionExecution: ...


class DefaultQuestionExecutor:
    def execute(self, thread: QuestionThread, message: str) -> QuestionExecution:
        raise QuestionExecutionError("server question execution has moved to dispatcher question jobs")
