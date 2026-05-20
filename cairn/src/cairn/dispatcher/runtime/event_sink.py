from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.redaction import redact_text
from cairn.shared.worker_events import WorkerEvent, status_event, stream_event

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionEventSink:
    client: CairnClient
    execution_id: str
    dispatcher_id: str = "dispatcher"
    secrets: list[str] = field(default_factory=list)
    batch_size: int = 1
    _lock: Any = field(init=False, repr=False)
    _seq: int = field(default=0, init=False, repr=False)
    _queue: list[WorkerEvent] = field(default_factory=list, init=False, repr=False)
    _failed_flushes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def failed_flushes(self) -> int:
        return self._failed_flushes

    def write_status(self, status: str, **payload) -> None:
        self.write_event(status_event(status, event_key=self._event_key("status"), **payload))

    def write_stream(self, stream: str, text: str) -> None:
        if stream not in {"stdout", "stderr"} or not text:
            return
        redacted = redact_text(text, self.secrets)
        self.write_event(stream_event(stream, redacted, event_key=self._event_key(stream)))

    def write_event(self, event: WorkerEvent) -> None:
        with self._lock:
            self._queue.append(event)
            should_flush = len(self._queue) >= self.batch_size
        if should_flush:
            self.flush()

    def flush(self) -> bool:
        with self._lock:
            if not self._queue:
                return True
            events = self._queue
            self._queue = []
        response = self.client.append_execution_events(
            self.execution_id,
            dispatcher_id=self.dispatcher_id,
            events=[event.to_api_payload() for event in events],
        )
        if response.ok:
            return True
        with self._lock:
            self._queue = events + self._queue
            self._failed_flushes += 1
        LOG.warning(
            "execution event flush failed execution=%s status=%s body=%s",
            self.execution_id,
            response.status_code,
            response.text,
        )
        return False

    def close(
        self,
        *,
        terminal_status: str | None = None,
        returncode: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        flushed = True
        if terminal_status is not None:
            self.write_status(terminal_status)
            flushed = self.flush()
            patch = {
                "status": terminal_status,
                "returncode": returncode,
                "error_code": error_code,
                "error_detail": error_detail,
            }
            response = self.client.patch_execution(self.execution_id, patch)
            if not response.ok:
                LOG.warning(
                    "execution terminal patch failed execution=%s status=%s body=%s",
                    self.execution_id,
                    response.status_code,
                    response.text,
                )
        return flushed and self.flush()

    def _event_key(self, prefix: str) -> str:
        self._seq += 1
        return f"{self.execution_id}:{prefix}:{self._seq}"


class CompositeRunLogger:
    def __init__(self, *loggers):
        self._loggers = [logger for logger in loggers if logger is not None]

    def write_stream(self, stream: str, text: str) -> None:
        for logger in self._loggers:
            write_stream = getattr(logger, "write_stream", None)
            if callable(write_stream):
                write_stream(stream, text)

    def finish(self, *, returncode: int, timed_out: bool, cancelled: bool, cancel_reason: str | None) -> None:
        for logger in self._loggers:
            finish = getattr(logger, "finish", None)
            if callable(finish):
                finish(
                    returncode=returncode,
                    timed_out=timed_out,
                    cancelled=cancelled,
                    cancel_reason=cancel_reason,
                )
