from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.redaction import redact_text
from cairn.shared.worker_events import WorkerEvent, message_event, status_event, stream_event

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class FlushResult:
    ok: bool
    status_code: int | None = None
    text: str = ""
    attempts: int = 0
    batch_id: str | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(slots=True)
class ExecutionEventSink:
    client: CairnClient
    execution_id: str
    dispatcher_id: str = "dispatcher"
    secrets: list[str] = field(default_factory=list)
    batch_size: int = 25
    max_queue_events: int = 1000
    append_retry_delays: tuple[float, ...] = (0.25, 1.0, 3.0)
    close_timeout_seconds: float = 30.0
    event_projector: Any | None = None
    _lock: Any = field(init=False, repr=False)
    _seq: int = field(default=0, init=False, repr=False)
    _queue: list[WorkerEvent] = field(default_factory=list, init=False, repr=False)
    _failed_flushes: int = field(default=0, init=False, repr=False)
    _projector_failed: bool = field(default=False, init=False, repr=False)
    _projector_closed: bool = field(default=False, init=False, repr=False)
    _projected_assistant_messages: int = field(default=0, init=False, repr=False)
    _batch_seq: int = field(default=0, init=False, repr=False)
    _last_successful_flush_at: float | None = field(default=None, init=False, repr=False)
    _oldest_queued_at: float | None = field(default=None, init=False, repr=False)
    _fatal_error: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def failed_flushes(self) -> int:
        return self._failed_flushes

    @property
    def last_successful_flush_at(self) -> float | None:
        return self._last_successful_flush_at

    @property
    def oldest_queued_at(self) -> float | None:
        return self._oldest_queued_at

    @property
    def has_projected_assistant_message(self) -> bool:
        return self._projected_assistant_messages > 0

    def write_status(self, status: str, **payload) -> None:
        self.write_event(status_event(status, event_key=self._event_key("status"), **payload))

    def write_stream(self, stream: str, text: str) -> None:
        if stream not in {"stdout", "stderr"} or not text:
            return
        redacted = redact_text(text, self.secrets)
        self.write_event(stream_event(stream, redacted, event_key=self._event_key(stream)))
        self._write_projected_stream_events(stream, redacted)

    def write_event(self, event: WorkerEvent) -> None:
        if self._fatal_error is not None and event.event_type != "message":
            return
        should_flush = False
        with self._lock:
            if not self._queue:
                self._oldest_queued_at = time.monotonic()
            self._queue.append(event)
            should_flush = len(self._queue) >= self.batch_size
            over_limit = len(self._queue) > self.max_queue_events
        if over_limit:
            result = self.flush()
            if not result:
                self._mark_fatal("queue_full", result)
                return
        if should_flush:
            self.flush()

    def flush(self) -> FlushResult:
        with self._lock:
            if not self._queue:
                return FlushResult(ok=True, attempts=0)
            events = self._queue
            self._queue = []
            self._oldest_queued_at = None
            batch_id = self._next_batch_id()
        result = self._append_batch(events, batch_id=batch_id)
        if result.ok:
            self._last_successful_flush_at = time.monotonic()
            return result
        with self._lock:
            self._queue = events + self._queue
            self._oldest_queued_at = self._oldest_queued_at or time.monotonic()
            self._failed_flushes += 1
        LOG.warning(
            "append_timeout execution=%s batch_id=%s status=%s body=%s",
            self.execution_id,
            batch_id,
            result.status_code,
            result.text,
        )
        return result

    def _append_batch(self, events: list[WorkerEvent], *, batch_id: str) -> FlushResult:
        attempts = 0
        delays = (0.0, *self.append_retry_delays)
        last_response = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            attempts += 1
            response = self.client.append_execution_events(
                self.execution_id,
                dispatcher_id=self.dispatcher_id,
                events=[event.to_api_payload() for event in events],
            )
            last_response = response
            if response.ok:
                if attempts > 1:
                    LOG.info("idempotent_replay execution=%s batch_id=%s attempts=%s", self.execution_id, batch_id, attempts)
                return FlushResult(ok=True, status_code=response.status_code, text=response.text, attempts=attempts, batch_id=batch_id)
            if response.status_code == 409:
                LOG.warning("event_key_conflict execution=%s batch_id=%s body=%s", self.execution_id, batch_id, response.text)
                break
            if response.status_code != 0:
                break
        assert last_response is not None
        return FlushResult(
            ok=False,
            status_code=last_response.status_code,
            text=last_response.text,
            attempts=attempts,
            batch_id=batch_id,
        )

    def _finish_batch(self, events: list[WorkerEvent], patch: dict[str, Any], *, batch_id: str) -> FlushResult:
        finish = getattr(self.client, "finish_execution", None)
        if callable(finish):
            response = finish(
                self.execution_id,
                dispatcher_id=self.dispatcher_id,
                events=[event.to_api_payload() for event in events],
                patch=patch,
            )
            if response.ok:
                self._last_successful_flush_at = time.monotonic()
                return FlushResult(ok=True, status_code=response.status_code, text=response.text, attempts=1, batch_id=batch_id)
            LOG.warning(
                "finish_timeout execution=%s batch_id=%s status=%s body=%s",
                self.execution_id,
                batch_id,
                response.status_code,
                response.text,
            )
            return FlushResult(ok=False, status_code=response.status_code, text=response.text, attempts=1, batch_id=batch_id)

        response = self.client.append_execution_events(
            self.execution_id,
            dispatcher_id=self.dispatcher_id,
            events=[event.to_api_payload() for event in events],
        )
        if not response.ok:
            return FlushResult(ok=False, status_code=response.status_code, text=response.text, attempts=1, batch_id=batch_id)
        patch_response = self.client.patch_execution(self.execution_id, patch)
        return FlushResult(
            ok=patch_response.ok,
            status_code=patch_response.status_code,
            text=patch_response.text,
            attempts=1,
            batch_id=batch_id,
        )

    def close(
        self,
        *,
        terminal_status: str | None = None,
        returncode: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        patch_fields: dict[str, Any] | None = None,
    ) -> bool:
        self.flush_projector_events()
        if terminal_status is not None:
            if self._fatal_error is not None and terminal_status == "succeeded":
                terminal_status = "failed"
                error_code = error_code or "event_flush_failed"
                error_detail = error_detail or self._fatal_error
            patch = {
                "status": terminal_status,
                "returncode": returncode,
                "error_code": error_code,
                "error_detail": error_detail,
            }
            if patch_fields:
                patch.update(patch_fields)
            self._enqueue_now(status_event(terminal_status, event_key=self._event_key("status"), error_code=error_code, error_detail=error_detail))
            with self._lock:
                events = self._queue
                self._queue = []
                self._oldest_queued_at = None
                batch_id = self._next_batch_id()
            result = self._finish_batch(events, patch, batch_id=batch_id)
            if result.ok:
                return True
            with self._lock:
                self._queue = events + self._queue
                self._oldest_queued_at = self._oldest_queued_at or time.monotonic()
                self._failed_flushes += 1
            self._mark_terminal_barrier_failed(result)
            return False
        return bool(self.flush())

    def _event_key(self, prefix: str) -> str:
        self._seq += 1
        return f"{self.execution_id}:{prefix}:{self._seq}"

    def _next_batch_id(self) -> str:
        self._batch_seq += 1
        return f"{self.execution_id}:batch:{self._batch_seq}"

    def _enqueue_now(self, event: WorkerEvent) -> None:
        with self._lock:
            if not self._queue:
                self._oldest_queued_at = time.monotonic()
            self._queue.append(event)

    def _mark_fatal(self, code: str, result: FlushResult) -> None:
        self._fatal_error = code
        LOG.warning("%s execution=%s batch_id=%s queue_length=%s", code, self.execution_id, result.batch_id, len(self._queue))
        self._enqueue_now(
            message_event(
                "system",
                f"dispatcher diagnostic: {code}",
                event_key=self._event_key(f"diagnostic-{code}"),
            )
        )

    def _mark_terminal_barrier_failed(self, result: FlushResult) -> None:
        LOG.warning(
            "terminal_barrier_failed execution=%s batch_id=%s status=%s body=%s",
            self.execution_id,
            result.batch_id,
            result.status_code,
            result.text,
        )
        patch = {
            "dispatcher_id": self.dispatcher_id,
            "status": "failed",
            "error_code": "event_flush_failed",
            "error_detail": result.text or f"terminal barrier failed status={result.status_code}",
        }
        response = self.client.patch_execution(self.execution_id, patch)
        if not response.ok:
            LOG.warning(
                "terminal barrier fallback patch failed execution=%s status=%s body=%s",
                self.execution_id,
                response.status_code,
                response.text,
            )

    def _write_projected_stream_events(self, stream: str, text: str) -> None:
        if self.event_projector is None or self._projector_failed or self._projector_closed:
            return
        try:
            events = self.event_projector.feed(stream, text)
        except Exception as exc:
            self._projector_failed = True
            self.write_event(
                message_event(
                    "system",
                    f"dispatcher parser diagnostic: worker output projector failed: {exc}",
                    event_key=self._event_key("parser-diagnostic"),
                )
            )
            return
        self._write_projected_events(events or [])

    def flush_projector_events(self) -> None:
        if self.event_projector is None or self._projector_failed or self._projector_closed:
            return
        try:
            events = self.event_projector.close()
            self._projector_closed = True
        except Exception as exc:
            self._projector_failed = True
            self.write_event(
                message_event(
                    "system",
                    f"dispatcher parser diagnostic: worker output projector failed during close: {exc}",
                    event_key=self._event_key("parser-diagnostic"),
                )
            )
            return
        self._write_projected_events(events or [])

    def _write_projected_events(self, events: list[WorkerEvent]) -> None:
        for event in events:
            if event.event_type == "message" and event.role == "assistant":
                self._projected_assistant_messages += 1
            self.write_event(event)


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
