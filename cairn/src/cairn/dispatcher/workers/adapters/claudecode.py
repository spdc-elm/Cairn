from __future__ import annotations

import json
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, JsonLineStreamProjector, QuestionCapability, SeedSessionDriver, WorkerRuntimeContext
from cairn.shared.worker_events import WorkerEvent, session_event


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"
    _required_executables = ("claude",)

    def required_executables(self) -> tuple[str, ...]:
        return self._required_executables

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self._healthcheck_argv(worker)

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self._healthcheck_argv(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return " ".join(self._healthcheck_argv(worker))

    def _healthcheck_argv(self, worker: WorkerConfig) -> list[str]:
        return [
            "claude",
            "--bare",
            "--tools",
            "",
            "--model",
            worker.env["ANTHROPIC_MODEL"],
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "-p",
            "Reply exactly: OK",
        ]

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None, runtime_context: WorkerRuntimeContext | None = None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--model",
                worker.env["ANTHROPIC_MODEL"],
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "-p",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str, runtime_context: WorkerRuntimeContext | None = None) -> list[str]:
        return [
            "claude",
            "--model",
            worker.env["ANTHROPIC_MODEL"],
            "-r",
            session,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "-p",
            "--",
            prompt,
        ]

    def question_capability(self, worker: WorkerConfig) -> QuestionCapability:
        return QuestionCapability(
            can_resume_session=True,
            can_fork_session=True,
            can_use_tools=True,
            can_stream_events=True,
            resume_mutates_source=True,
            fork_creates_remote_log=True,
            question_modes=("fork", "resume", "fresh_context"),
            unavailable_reasons={},
        )

    def build_question(
        self,
        worker: WorkerConfig,
        *,
        mode: str,
        prompt: str,
        source_session: str | None = None,
        runtime_context: WorkerRuntimeContext | None = None,
    ) -> DriverResult:
        if mode != "fork":
            return super().build_question(worker, mode=mode, prompt=prompt, source_session=source_session, runtime_context=runtime_context)
        if not source_session:
            raise ValueError("fork question requires source_session")
        return DriverResult(
            argv=[
                "claude",
                "--model",
                worker.env["ANTHROPIC_MODEL"],
                "--resume",
                source_session,
                "--fork-session",
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "-p",
                "--",
                prompt,
            ],
            session=None,
        )

    def remote_session_kind(self) -> str:
        return "claude_session"

    def session_capture_method(self, prepared_session: str | None, resolved_session: str | None) -> str:
        if prepared_session and resolved_session == prepared_session:
            return "prepared"
        if resolved_session:
            return "stdout_event"
        return "unavailable"

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            for key in ("session_id", "sessionId"):
                session_id = event.get(key)
                if isinstance(session_id, str) and session_id:
                    return session_id
            if event.get("type") == "system":
                for key in ("session_id", "sessionId"):
                    session_id = event.get(key)
                    if isinstance(session_id, str) and session_id:
                        return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        result_text: str | None = None
        assistant_text: str | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "result":
                result = event.get("result")
                if isinstance(result, str) and result:
                    result_text = result
            elif event_type == "assistant":
                message = event.get("message")
                text = self._message_text(message)
                if text:
                    assistant_text = text
        if result_text and result_text.strip():
            return result_text.strip()
        if assistant_text and assistant_text.strip():
            return assistant_text.strip()
        return stdout

    def stream_event_projector(self, execution_id: str) -> "ClaudeCodeStreamProjector":
        return ClaudeCodeStreamProjector(execution_id)

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _message_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)


class ClaudeCodeStreamProjector(JsonLineStreamProjector):
    def __init__(self, execution_id: str):
        super().__init__(execution_id)
        self._seq = 0

    def project_json_event(self, payload: dict[str, Any]) -> list[WorkerEvent]:
        event_type = payload.get("type")
        events: list[WorkerEvent] = []
        session_id = self._session_id(payload)
        if session_id:
            events.append(
                session_event(
                    kind="claude_session",
                    session_id=session_id,
                    status="available",
                    capture_method="stdout_event",
                    event_key=f"{self.execution_id}:session:{session_id}",
                )
            )
        if event_type == "assistant":
            text = ClaudeCodeDriver._message_text(payload.get("message"))
            if text:
                events.append(self._assistant_message(text, status="running"))
        elif event_type == "result":
            result = payload.get("result")
            if isinstance(result, str) and result.strip():
                events.append(self._assistant_message(result.strip(), status="success", final=True))
        elif event_type == "stream_event":
            events.extend(self._project_stream_event(payload.get("event")))
        return events

    def _project_stream_event(self, stream_event: Any) -> list[WorkerEvent]:
        if not isinstance(stream_event, dict):
            return []
        stream_type = stream_event.get("type")
        if stream_type == "content_block_delta":
            delta = stream_event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    return [self._assistant_message(text, status="running", stream_delta=True)]
        if stream_type == "content_block_start":
            block = stream_event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return [
                    WorkerEvent(
                        event_type="tool",
                        payload={
                            "name": block.get("name") or "tool",
                            "status": "running",
                            "input": block.get("input") or {},
                            "stream_key": f"{self.execution_id}:tool-call:{block.get('id') or stream_event.get('index') or 0}",
                        },
                        event_key=self._event_key("tool"),
                    )
                ]
        return []

    def _assistant_message(
        self,
        text: str,
        *,
        status: str,
        final: bool = False,
        stream_delta: bool = False,
    ) -> WorkerEvent:
        payload: dict[str, Any] = {
            "text": text,
            "status": status,
            "stream_key": f"{self.execution_id}:claude:text",
        }
        if stream_delta:
            payload["stream_delta"] = True
        return WorkerEvent(
            event_type="message",
            role="assistant",
            payload=payload,
            event_key=f"{self.execution_id}:assistant:final" if final else self._event_key("assistant"),
        )

    def _event_key(self, label: str) -> str:
        self._seq += 1
        return f"{self.execution_id}:claude-projector:{label}:{self._seq}"

    @staticmethod
    def _session_id(payload: dict[str, Any]) -> str | None:
        for key in ("session_id", "sessionId"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None
