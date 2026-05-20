from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from cairn.dispatcher.redaction import redact_text
from cairn.shared.run_logs import run_log_root


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RunLogWriter:
    def __init__(
        self,
        *,
        project_id: str,
        task_type: str,
        phase: str,
        worker_name: str,
        intent_id: str | None,
        metadata: dict[str, Any],
        secrets: list[str] | None = None,
    ):
        self.run_id = f"run_{uuid.uuid4().hex}"
        self.project_id = project_id
        self.task_type = task_type
        self.phase = phase
        self.worker_name = worker_name
        self.intent_id = intent_id
        self._secrets = secrets or []
        self.path = run_log_root() / project_id / f"{self.run_id}.jsonl"
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write(
            "run_started",
            {
                "metadata": metadata,
            },
        )

    def write_stream(self, stream: str, text: str) -> None:
        if not text:
            return
        self.write(
            "stream",
            {
                "stream": stream,
                "text": redact_text(text, self._secrets),
            },
        )

    def finish(self, *, returncode: int, timed_out: bool, cancelled: bool, cancel_reason: str | None) -> None:
        self.write(
            "run_finished",
            {
                "returncode": returncode,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "cancel_reason": cancel_reason,
            },
        )
        with self._lock:
            self._closed = True

    def write(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            record = {
                "schema": "cairn.run_log.v1",
                "ts": utcnow(),
                "seq": self._seq,
                "run_id": self.run_id,
                "project_id": self.project_id,
                "intent_id": self.intent_id,
                "task_type": self.task_type,
                "phase": self.phase,
                "worker": self.worker_name,
                "event": event,
                **data,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
