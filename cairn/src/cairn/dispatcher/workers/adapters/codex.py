from __future__ import annotations

import json
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters._curl import (
    build_curl_healthcheck,
    build_verbose_curl_healthcheck,
    expand_env,
    render_curl_command,
)
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver


class CodexDriver(RegexSessionDriver):
    type_name = "codex"
    _required_executables = ("codex",)

    def required_executables(self) -> tuple[str, ...]:
        return self._required_executables

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return build_curl_healthcheck(
            self._healthcheck_url(worker),
            headers=self._healthcheck_headers(worker),
            payload=self._healthcheck_payload(worker),
            required_executables=self.required_executables(),
        )

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return build_verbose_curl_healthcheck(
            self._healthcheck_url(worker),
            headers=self._healthcheck_headers(worker),
            payload=self._healthcheck_payload(worker),
            required_executables=self.required_executables(),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return render_curl_command(
            self._healthcheck_url(worker),
            headers=[
                "-H",
                expand_env("Authorization: Bearer $OPENAI_API_KEY"),
                "-H",
                "content-type: application/json",
            ],
            payload=self._healthcheck_payload(worker),
            required_executables=self.required_executables(),
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        env = worker.env
        return DriverResult(
            argv=[
                "codex",
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                env["CODEX_MODEL"],
                "-c",
                'model_provider="cairn"',
                "-c",
                'model_providers.cairn.name="cairn"',
                "-c",
                'model_providers.cairn.wire_api="responses"',
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
                "-c",
                'model_providers.cairn.env_key="OPENAI_API_KEY"',
                "--",
                prompt,
            ]
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        env = worker.env
        return [
            "codex",
            "exec",
            "resume",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
            session,
            "--",
            prompt,
        ]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "thread.started":
                continue
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
        return super().extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        last_message: str | None = None
        for event in self._iter_events(stdout):
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                last_message = text
        return last_message.strip() if last_message and last_message.strip() else stdout

    @staticmethod
    def _healthcheck_url(worker: WorkerConfig) -> str:
        return f"{worker.env['CODEX_BASE_URL']}/responses"

    @staticmethod
    def _healthcheck_headers(worker: WorkerConfig) -> list[str]:
        return [
            "-H",
            f"Authorization: Bearer {worker.env['OPENAI_API_KEY']}",
            "-H",
            "content-type: application/json",
        ]

    @staticmethod
    def _healthcheck_payload(worker: WorkerConfig) -> str:
        stream = worker.env.get("CODEX_HEALTHCHECK_STREAM", "true").lower() in {"1", "true", "yes", "on"}
        return json.dumps(
            {
                "input": [{"content": "ping", "role": "user"}],
                "model": worker.env["CODEX_MODEL"],
                "stream": stream,
            },
            ensure_ascii=False,
        )

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
