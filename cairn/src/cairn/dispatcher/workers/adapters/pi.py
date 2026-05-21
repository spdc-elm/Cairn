from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, JsonLineStreamProjector, QuestionCapability, WorkerDriver
from cairn.shared.worker_events import WorkerEvent, session_event


class PiDriver(WorkerDriver):
    type_name = "pi"
    _required_executables = ("pi",)

    def required_executables(self) -> tuple[str, ...]:
        return self._required_executables

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return self._wrap_with_models(
            worker,
            [
                "--provider",
                "cairn",
                "--model",
                env["PI_MODEL"],
                "--mode",
                "json",
                "--session-dir",
                self._session_dir(worker),
                "--no-session",
                "--no-tools",
                "-p",
                "Reply with exactly pong.",
            ],
            enable_tools=False,
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
        ]
        if session:
            argv.extend(["--session", session])
        argv.extend(["-p", prompt])
        return DriverResult(argv=self._wrap_with_models(worker, argv), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
            "-p",
            prompt,
        ]
        return self._wrap_with_models(worker, argv)

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
    ) -> DriverResult:
        if mode != "fork":
            return super().build_question(worker, mode=mode, prompt=prompt, source_session=source_session)
        if not source_session:
            raise ValueError("fork question requires source_session")
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--fork",
            source_session,
            "-p",
            prompt,
        ]
        return DriverResult(argv=self._wrap_with_models(worker, argv), session=None)

    def remote_session_kind(self) -> str:
        return "pi_session"

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
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            break
        if assistant_message is None:
            return stdout
        content = assistant_message.get("content")
        if not isinstance(content, list):
            return stdout
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip() or stdout

    def stream_event_projector(self, execution_id: str) -> "PiStreamProjector":
        return PiStreamProjector(execution_id)

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True) -> list[str]:
        remote_credentials = not worker.env.get("PI_API_KEY")
        script = (
            'base_dir="${CAIRN_WORKSPACE:-/tmp}"\n'
            f'agent_dir="$base_dir/.cairn/pi/{worker.name}"\n'
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'if [ "${CAIRN_PI_REMOTE_CREDENTIALS:-0}" != "1" ]; then\n'
            "python3 - <<'PY' \"$agent_dir\"\n"
            "import json, os, sys\n"
            "model = {'id': os.environ['PI_MODEL'], 'name': os.environ['PI_MODEL']}\n"
            "context = os.environ.get('PI_MODEL_CONTEXT_WINDOW')\n"
            "if context:\n"
            "    model['contextWindow'] = int(context)\n"
            "payload = {'providers': {'cairn': {'baseUrl': os.environ['PI_BASE_URL'], 'api': os.environ['PI_PROVIDER_API'], 'apiKey': os.environ['PI_API_KEY'], 'models': [model]}}}\n"
            "open(os.path.join(sys.argv[1], 'models.json'), 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=True, separators=(',', ':')))\n"
            "PY\n"
            "chmod 600 \"$agent_dir/models.json\"\n"
            "fi\n"
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", "read,write,edit,bash,grep,find,ls"])
        wrapped_env = {"CAIRN_PI_REMOTE_CREDENTIALS": "1"} if remote_credentials else {}
        prefix = ["env", *[f"{key}={value}" for key, value in wrapped_env.items()]]
        return [*prefix, "/bin/sh", "-lc", script, "--", *argv, *pi_argv]

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

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
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _models_json(worker: WorkerConfig) -> str:
        env = worker.env
        model: dict[str, Any] = {
            "id": env["PI_MODEL"],
            "name": env["PI_MODEL"],
        }
        context_window = env.get("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            model["contextWindow"] = int(context_window)

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"cairn": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


class PiStreamProjector(JsonLineStreamProjector):
    def __init__(self, execution_id: str):
        super().__init__(execution_id)
        self._seq = 0
        self._saw_assistant_turn = False
        self._pending_agent_messages: list[list[dict[str, Any]]] = []

    def project_json_event(self, payload: dict[str, Any]) -> list[WorkerEvent]:
        event_type = payload.get("type")
        if event_type == "session":
            session_id = payload.get("id")
            if isinstance(session_id, str) and session_id:
                return [
                    session_event(
                        kind="pi_session",
                        session_id=session_id,
                        status="available",
                        capture_method="stdout_event",
                        event_key=f"{self.execution_id}:session:{session_id}",
                    )
                ]
        if event_type in {"message_update", "message_end", "turn_end"}:
            message = payload.get("message")
            text = PiDriver._message_text(message)
            if text and isinstance(message, dict) and message.get("role") == "assistant":
                if event_type == "turn_end":
                    self._saw_assistant_turn = True
                return [
                    WorkerEvent(
                        event_type="message",
                        role="assistant",
                        payload={
                            "text": text,
                            "status": "success" if event_type in {"message_end", "turn_end"} else "running",
                            "stream_key": f"{self.execution_id}:pi:text",
                        },
                        event_key=self._event_key("assistant"),
                    )
                ]
        if event_type == "agent_end":
            messages = payload.get("messages")
            if isinstance(messages, list):
                self._pending_agent_messages.append([item for item in messages if isinstance(item, dict)])
        if event_type in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            return [
                WorkerEvent(
                    event_type="tool",
                    payload={
                        "name": payload.get("toolName") or payload.get("name") or "tool",
                        "status": "error" if payload.get("isError") else ("success" if event_type.endswith("_end") else "running"),
                        "result": payload.get("result"),
                        "args": payload.get("args"),
                        "stream_key": f"{self.execution_id}:tool-call:{payload.get('toolCallId') or payload.get('name') or 'tool'}",
                    },
                    event_key=self._event_key("tool"),
                )
            ]
        return []

    def close(self) -> list[WorkerEvent]:
        events = super().close()
        if self._saw_assistant_turn:
            return events
        for messages in reversed(self._pending_agent_messages):
            assistant = next((message for message in reversed(messages) if message.get("role") == "assistant"), None)
            text = PiDriver._message_text(assistant)
            if text:
                events.append(
                    WorkerEvent(
                        event_type="message",
                        role="assistant",
                        payload={"text": text, "status": "success", "stream_key": f"{self.execution_id}:pi:text"},
                        event_key=f"{self.execution_id}:assistant:final",
                    )
                )
                break
        return events

    def _event_key(self, label: str) -> str:
        self._seq += 1
        return f"{self.execution_id}:pi-projector:{label}:{self._seq}"
