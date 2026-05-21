from __future__ import annotations

import abc
import json
import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.shared.worker_events import WorkerEvent


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteSessionResult:
    id: str | None
    kind: str | None
    status: str
    capture_method: str | None


@dataclass(frozen=True, slots=True)
class QuestionCapability:
    can_resume_session: bool
    can_fork_session: bool
    can_use_tools: bool
    can_stream_events: bool
    resume_mutates_source: bool
    fork_creates_remote_log: bool
    question_modes: tuple[str, ...]
    detection: str = "static"
    unavailable_reasons: dict[str, str] | None = None


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str | None:
        return None

    def required_executables(self) -> tuple[str, ...]:
        return ()

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self.build_healthcheck(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return shlex.join(self.build_startup_healthcheck(worker))

    @abc.abstractmethod
    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        raise NotImplementedError

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        raise NotImplementedError

    def question_capability(self, worker: WorkerConfig) -> QuestionCapability:
        return QuestionCapability(
            can_resume_session=True,
            can_fork_session=False,
            can_use_tools=True,
            can_stream_events=False,
            resume_mutates_source=True,
            fork_creates_remote_log=False,
            question_modes=("resume", "fresh_context"),
            unavailable_reasons={"fork": f"{self.type_name}_fork_unavailable"},
        )

    def build_question(
        self,
        worker: WorkerConfig,
        *,
        mode: str,
        prompt: str,
        source_session: str | None = None,
    ) -> DriverResult:
        capability = self.question_capability(worker)
        if mode not in capability.question_modes:
            raise ValueError(f"question mode not supported by {self.type_name}: {mode}")
        if mode == "fresh_context":
            return self.build_execute(worker, prompt, self.prepare_session())
        if mode == "resume":
            if not source_session:
                raise ValueError("resume question requires source_session")
            return DriverResult(argv=self.build_conclude(worker, prompt, source_session), session=source_session)
        raise ValueError(f"question mode not implemented by {self.type_name}: {mode}")

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        return session

    def remote_session_kind(self) -> str:
        return f"{self.type_name}_session"

    def session_capture_method(self, prepared_session: str | None, resolved_session: str | None) -> str:
        if prepared_session and resolved_session == prepared_session:
            return "prepared"
        if resolved_session:
            return "adapter_inferred"
        return "unavailable"

    def extract_session_provenance(self, session: str | None, stdout: str, stderr: str) -> RemoteSessionResult:
        resolved = self.extract_session(session, stdout, stderr)
        if resolved:
            return RemoteSessionResult(
                id=resolved,
                kind=self.remote_session_kind(),
                status="available",
                capture_method=self.session_capture_method(session, resolved),
            )
        return RemoteSessionResult(id=None, kind=None, status="missing", capture_method="unavailable")

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout

    def stream_event_projector(self, execution_id: str) -> "WorkerStreamProjector | None":
        return None


class WorkerStreamProjector:
    def feed(self, stream: str, text: str) -> list[WorkerEvent]:
        return []

    def close(self) -> list[WorkerEvent]:
        return []


class JsonLineStreamProjector(WorkerStreamProjector):
    def __init__(self, execution_id: str):
        self.execution_id = execution_id
        self._stdout_buffer = ""
        self._stderr_buffer = ""

    def feed(self, stream: str, text: str) -> list[WorkerEvent]:
        if stream == "stdout":
            return self._feed_buffer("_stdout_buffer", text)
        if stream == "stderr":
            return self._feed_buffer("_stderr_buffer", text)
        return []

    def close(self) -> list[WorkerEvent]:
        events: list[WorkerEvent] = []
        events.extend(self._drain_buffer("_stdout_buffer"))
        events.extend(self._drain_buffer("_stderr_buffer"))
        return events

    def _feed_buffer(self, attr: str, text: str) -> list[WorkerEvent]:
        buffered = getattr(self, attr) + text
        lines = buffered.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            setattr(self, attr, lines.pop())
        else:
            setattr(self, attr, "")
        return self._parse_lines(lines)

    def _drain_buffer(self, attr: str) -> list[WorkerEvent]:
        buffered = getattr(self, attr)
        setattr(self, attr, "")
        return self._parse_lines([buffered]) if buffered.strip() else []

    def _parse_lines(self, lines: list[str]) -> list[WorkerEvent]:
        events: list[WorkerEvent] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.extend(self.project_json_event(payload))
        return events

    def project_json_event(self, payload: dict[str, Any]) -> list[WorkerEvent]:
        return []


class SeedSessionDriver(WorkerDriver):
    def prepare_session(self) -> str | None:
        return str(uuid.uuid4())


class RegexSessionDriver(WorkerDriver):
    session_pattern = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None
