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
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver


ANTHROPIC_VERSION = "2023-06-01"


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"
    _required_executables = ("claude",)

    def required_executables(self) -> tuple[str, ...]:
        return self._required_executables

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return build_curl_healthcheck(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                f"Authorization: Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=self._healthcheck_payload(worker),
            required_executables=self.required_executables(),
        )

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        return build_verbose_curl_healthcheck(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                f"Authorization: Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=self._healthcheck_payload(worker),
            required_executables=self.required_executables(),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        env = worker.env
        return render_curl_command(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                expand_env("Authorization: Bearer $ANTHROPIC_AUTH_TOKEN"),
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=self._healthcheck_payload(worker),
            required_executables=self.required_executables(),
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "-p",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "-p",
            "--",
            prompt,
        ]

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

    @staticmethod
    def _healthcheck_payload(worker: WorkerConfig) -> str:
        return (
            '{"model":"'
            + worker.env["ANTHROPIC_MODEL"]
            + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
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
