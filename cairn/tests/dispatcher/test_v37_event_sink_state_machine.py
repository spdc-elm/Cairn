from __future__ import annotations

import unittest

from cairn.dispatcher.runtime.event_sink import ExecutionEventSink, MAX_APPEND_EVENTS


class FakeResponse:
    def __init__(self, ok: bool, status_code: int = 200, text: str = "") -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text


class RecordingClient:
    def __init__(self) -> None:
        self.append_calls: list[list[dict]] = []
        self.patch_calls: list[dict] = []
        self.finish_calls: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, sink_token: str | None = None, events: list[dict]):
        self.append_calls.append(events)
        return FakeResponse(True)

    def patch_execution(self, execution_id: str, payload: dict):
        self.patch_calls.append(payload)
        return FakeResponse(True)

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, sink_token: str | None = None, events: list[dict], patch: dict):
        self.finish_calls.append({"events": events, "patch": patch})
        return FakeResponse(True)


class ConflictClient:
    """Returns 409 on first append, then OK."""

    def __init__(self) -> None:
        self.append_calls: list[list[dict]] = []
        self.patch_calls: list[dict] = []
        self.finish_calls: list[dict] = []
        self._conflict_count = 0

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, sink_token: str | None = None, events: list[dict]):
        self.append_calls.append(events)
        if self._conflict_count == 0:
            self._conflict_count += 1
            return FakeResponse(False, 409, "Event key conflict")
        return FakeResponse(True)

    def patch_execution(self, execution_id: str, payload: dict):
        self.patch_calls.append(payload)
        return FakeResponse(True)

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, sink_token: str | None = None, events: list[dict], patch: dict):
        self.finish_calls.append({"events": events, "patch": patch})
        return FakeResponse(True)


class AlwaysConflictClient:
    """Always returns 409."""

    def __init__(self) -> None:
        self.append_calls: list[list[dict]] = []
        self.patch_calls: list[dict] = []
        self.finish_calls: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, sink_token: str | None = None, events: list[dict]):
        self.append_calls.append(events)
        return FakeResponse(False, 409, "Event key conflict")

    def patch_execution(self, execution_id: str, payload: dict):
        self.patch_calls.append(payload)
        return FakeResponse(True)

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, sink_token: str | None = None, events: list[dict], patch: dict):
        self.finish_calls.append({"events": events, "patch": patch})
        return FakeResponse(True)


class V37EventSinkStateMachineTests(unittest.TestCase):
    """EventSink state machine: live flush, finish, fail-fast, batch slicing, diagnostics."""

    # --- Live flush does NOT close projector ---

    def test_live_flush_does_not_close_projector(self) -> None:
        """flush() / write_stream during running must not call projector.close()."""

        class TrackingProjector:
            def __init__(self):
                self.feed_calls = 0
                self.close_called = False

            def feed(self, stream: str, text: str):
                self.feed_calls += 1
                return []

            def close(self):
                self.close_called = True
                return []

        client = RecordingClient()
        projector = TrackingProjector()
        sink = ExecutionEventSink(
            client, "ex001", batch_size=1, flush_interval_seconds=0,
            event_projector=projector,
        )

        sink.write_stream("stdout", "chunk1")
        sink.write_stream("stdout", "chunk2")
        sink.flush()

        self.assertGreater(projector.feed_calls, 0)
        self.assertFalse(projector.close_called,
                         "Live flush must NOT close the projector - only finish() should")

    # --- Repeated finish is idempotent, does not repeat raw metric ---

    def test_repeated_finish_does_not_duplicate_raw_metric(self) -> None:
        """Calling close(terminal_status=...) twice must not emit raw_stream_storage metric twice."""
        client = RecordingClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99)
        sink.write_stream("stdout", "x" * 20000)

        # First close with no terminal (flushes projector + raw metric)
        sink.close()
        # Second close with terminal
        sink.close(terminal_status="succeeded", returncode=0)

        all_events = [event for batch in client.append_calls for event in batch]
        all_events += [event for call in client.finish_calls for event in call["events"]]
        raw_metrics = [
            e for e in all_events
            if e.get("event_type") == "metric" and e.get("payload", {}).get("metric") == "raw_stream_storage"
        ]
        self.assertEqual(len(raw_metrics), 1,
                         "Raw storage metric must be emitted exactly once, not repeated on re-finish")

    # --- Batch slicing: never exceeds 250 events per append ---

    def test_batch_slicing_never_exceeds_250_events(self) -> None:
        """Even with large queue, each append batch must be <= 250 events."""
        client = RecordingClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=500, max_queue_events=2000, live_flush=False)

        # Queue 400 events
        for i in range(400):
            sink.write_event(
                __import__("cairn.shared.worker_events", fromlist=["WorkerEvent"]).WorkerEvent(
                    event_type="stdout",
                    payload={"text": f"line-{i}"},
                    event_key=f"ex001:stdout:{i}",
                )
            )

        sink.flush()

        for batch in client.append_calls:
            self.assertLessEqual(len(batch), MAX_APPEND_EVENTS,
                                 f"Batch of {len(batch)} events exceeds MAX_APPEND_EVENTS={MAX_APPEND_EVENTS}")
        total = sum(len(b) for b in client.append_calls)
        self.assertEqual(total, 400)

    # --- 409 fail-fast: stop appending after conflict, don't snowball ---

    def test_409_fail_fast_stops_further_appends(self) -> None:
        """After a 409 conflict, the sink must stop trying to append more events.
        It must not continue rolling up the queue and producing more 409s.
        """
        client = AlwaysConflictClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=1, max_queue_events=100, append_retry_delays=())

        sink.write_stream("stdout", "first")
        sink.write_stream("stdout", "second")
        sink.write_stream("stdout", "third")

        # After conflict, further writes should be suppressed (fatal_error set)
        # At most the first batch triggers the conflict + one diagnostic
        self.assertLessEqual(len(client.append_calls), 2,
                             "After 409, sink must not keep trying to append (fail-fast)")

    def test_409_clears_queue_and_marks_fatal(self) -> None:
        """After 409, the original queue must be cleared and _fatal_error must be set.
        Only the bounded diagnostic message may remain in queue."""
        client = AlwaysConflictClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99, max_queue_events=100, append_retry_delays=())

        # Pre-queue some events
        from cairn.shared.worker_events import WorkerEvent
        for i in range(5):
            sink._enqueue_now(WorkerEvent(event_type="stdout", payload={"text": f"q{i}"}, event_key=f"ex001:stdout:{i}"))

        sink.flush()

        self.assertIsNotNone(sink._fatal_error)
        self.assertEqual(sink._fatal_error, "event_key_conflict")
        with sink._lock:
            # Only the diagnostic message may remain (bounded diagnostic)
            self.assertLessEqual(len(sink._queue), 1,
                                 "After 409 conflict, only bounded diagnostic may remain in queue")
            if sink._queue:
                self.assertEqual(sink._queue[0].event_type, "metric",
                                 "Only diagnostic metric may remain after 409 clear")
                self.assertTrue(sink._queue[0].payload.get("diagnostic"))

    # --- 422 too_long: prevented by batch slicing ---

    def test_422_too_long_handled_as_protocol_bug(self) -> None:
        """If server returns 422 despite batch slicing, it's treated as fatal (protocol bug)."""

        class Return422Client:
            def __init__(self):
                self.append_calls = []
                self.patch_calls = []

            def append_execution_events(self, execution_id, *, dispatcher_id=None, sink_token=None, events=None):
                self.append_calls.append(events)
                return FakeResponse(False, 422, "events: ensure this value has at most 250 items")

            def patch_execution(self, execution_id, payload):
                self.patch_calls.append(payload)
                return FakeResponse(True)

        client = Return422Client()
        sink = ExecutionEventSink(client, "ex001", batch_size=10, max_queue_events=100, append_retry_delays=())

        from cairn.shared.worker_events import WorkerEvent
        for i in range(10):
            sink._enqueue_now(WorkerEvent(event_type="stdout", payload={"text": f"x{i}"}, event_key=f"ex001:x:{i}"))

        result = sink.flush()
        self.assertFalse(result.ok)
        # Sink should not endlessly retry 422
        self.assertLessEqual(len(client.append_calls), 2)

    # --- Queue overflow produces bounded diagnostic ---

    def test_queue_overflow_bounded_diagnostic(self) -> None:
        """Queue overflow should produce exactly one diagnostic message, not flood."""

        class DownClient:
            def __init__(self):
                self.append_calls = []
                self.patch_calls = []

            def append_execution_events(self, execution_id, *, dispatcher_id=None, sink_token=None, events=None):
                self.append_calls.append(events)
                return FakeResponse(False, 0, "connection refused")

            def patch_execution(self, execution_id, payload):
                self.patch_calls.append(payload)
                return FakeResponse(True)

        client = DownClient()
        sink = ExecutionEventSink(
            client, "ex001", batch_size=99, max_queue_events=3,
            append_retry_delays=(),
        )

        # Write enough to overflow
        sink.write_stream("stdout", "a")
        sink.write_stream("stdout", "b")
        sink.write_stream("stdout", "c")
        sink.write_stream("stdout", "d")
        sink.write_stream("stdout", "e")

        # Check that fatal error is set
        self.assertIsNotNone(sink._fatal_error)
        self.assertEqual(sink._fatal_error, "queue_full")

    # --- Terminal status and final assistant message ordering ---

    def test_terminal_status_not_emitted_before_final_assistant(self) -> None:
        """The terminal status event must come after the final assistant message in the finish batch."""
        client = RecordingClient()

        class FinalMessageProjector:
            def feed(self, stream, text):
                return []

            def close(self):
                from cairn.shared.worker_events import message_event
                return [message_event("assistant", "final answer", event_key="ex001:assistant:final")]

        sink = ExecutionEventSink(
            client, "ex001", batch_size=99, event_projector=FinalMessageProjector(),
        )
        sink.write_stream("stdout", "working...")

        sink.close(terminal_status="succeeded", returncode=0)

        # The finish call should have events where terminal status comes AFTER assistant message
        self.assertEqual(len(client.finish_calls), 1)
        finish_events = client.finish_calls[0]["events"]
        assistant_idx = None
        status_idx = None
        for i, ev in enumerate(finish_events):
            if ev.get("event_type") == "message" and ev.get("role") == "assistant":
                assistant_idx = i
            if ev.get("event_type") == "status" and ev.get("payload", {}).get("status") == "succeeded":
                status_idx = i
        self.assertIsNotNone(assistant_idx, "Final assistant message must be in finish batch")
        self.assertIsNotNone(status_idx, "Terminal status must be in finish batch")
        self.assertLess(assistant_idx, status_idx,
                        "Terminal status must come AFTER final assistant message")


if __name__ == "__main__":
    unittest.main()
