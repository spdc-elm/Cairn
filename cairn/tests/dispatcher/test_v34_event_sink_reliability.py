from __future__ import annotations

import unittest

from cairn.dispatcher.runtime.event_sink import ExecutionEventSink, RAW_PREVIEW_TOTAL_BYTES


class FakeResponse:
    def __init__(self, ok: bool, status_code: int = 200, text: str = "") -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text


class TimeoutThenOkClient:
    def __init__(self) -> None:
        self.append_calls: list[list[dict]] = []
        self.patch_calls: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, events: list[dict]):
        self.append_calls.append(events)
        if len(self.append_calls) == 1:
            return FakeResponse(False, 0, "read timed out")
        return FakeResponse(True)

    def patch_execution(self, execution_id: str, payload: dict):
        self.patch_calls.append(payload)
        return FakeResponse(True)


class FinishFailClient:
    def __init__(self) -> None:
        self.finishes: list[dict] = []
        self.patches: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, events: list[dict]):
        return FakeResponse(False, 0, "down")

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, events: list[dict], patch: dict):
        self.finishes.append({"events": events, "patch": patch})
        return FakeResponse(False, 0, "finish timed out")

    def patch_execution(self, execution_id: str, payload: dict):
        self.patches.append(payload)
        return FakeResponse(True)


class RecordingClient:
    def __init__(self) -> None:
        self.append_calls: list[list[dict]] = []
        self.patch_calls: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, events: list[dict]):
        self.append_calls.append(events)
        return FakeResponse(True)

    def patch_execution(self, execution_id: str, payload: dict):
        self.patch_calls.append(payload)
        return FakeResponse(True)


class V34EventSinkReliabilityTests(unittest.TestCase):
    def test_append_timeout_retries_same_batch_without_terminal_patch(self) -> None:
        client = TimeoutThenOkClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=1, append_retry_delays=(0.0,))

        result = sink.flush()
        self.assertTrue(result)
        sink.write_stream("stdout", "hello")

        self.assertEqual(len(client.append_calls), 2)
        self.assertEqual(client.append_calls[0][0]["event_key"], client.append_calls[1][0]["event_key"])
        self.assertEqual(client.append_calls[0][0]["ts"], client.append_calls[1][0]["ts"])
        self.assertEqual(client.patch_calls, [])

    def test_finish_failure_never_marks_succeeded_and_exposes_failed_patch(self) -> None:
        client = FinishFailClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99, append_retry_delays=(0.0,))
        sink.write_stream("stdout", "tail")

        ok = sink.close(terminal_status="succeeded", returncode=0)

        self.assertFalse(ok)
        self.assertEqual(client.finishes[0]["patch"]["status"], "succeeded")
        self.assertEqual(client.patches[-1]["status"], "failed")
        self.assertEqual(client.patches[-1]["error_code"], "event_flush_failed")

    def test_running_status_patches_execution_and_flushes_immediately(self) -> None:
        client = RecordingClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99)

        sink.write_status("running")

        self.assertEqual(client.patch_calls[0]["status"], "running")
        self.assertEqual(len(client.append_calls), 1)
        self.assertEqual(client.append_calls[0][0]["payload"]["status"], "running")

    def test_time_based_flush_prevents_long_running_stream_lag(self) -> None:
        client = RecordingClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99, flush_interval_seconds=0)

        sink.write_stream("stdout", "first")
        sink.flush()
        sink.write_stream("stdout", "second")

        self.assertEqual(len(client.append_calls), 2)
        self.assertEqual(client.append_calls[1][0]["payload"]["text"], "second")

    def test_live_flush_can_be_disabled_for_finish_barrier_tasks(self) -> None:
        client = RecordingClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=1, flush_interval_seconds=0, live_flush=False)

        sink.write_status("running")
        sink.write_stream("stdout", "held")

        self.assertEqual(client.append_calls, [])
        self.assertEqual(client.patch_calls, [])
        self.assertTrue(sink.close(terminal_status="succeeded", returncode=0))
        self.assertEqual(len(client.append_calls), 1)
        self.assertEqual(client.patch_calls[-1]["status"], "succeeded")

    def test_queue_full_enters_failed_terminal_barrier(self) -> None:
        client = FinishFailClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99, max_queue_events=1, append_retry_delays=(0.0,))

        sink.write_stream("stdout", "one")
        sink.write_stream("stdout", "two")
        ok = sink.close(terminal_status="succeeded", returncode=0)

        self.assertFalse(ok)
        self.assertEqual(client.patches[-1]["status"], "failed")

    def test_non_terminal_close_does_not_reemit_raw_storage_metric_at_finish(self) -> None:
        client = RecordingClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99)
        sink.write_stream("stdout", "x" * (RAW_PREVIEW_TOTAL_BYTES + 1))

        self.assertTrue(sink.close())
        self.assertTrue(sink.close(terminal_status="succeeded", returncode=0))

        events = [event for batch in client.append_calls for event in batch]
        raw_storage_events = [
            event
            for event in events
            if event["event_type"] == "metric" and event["payload"].get("metric") == "raw_stream_storage"
        ]
        terminal_events = [
            event
            for event in events
            if event["event_type"] == "status" and event["payload"].get("status") == "succeeded"
        ]
        self.assertEqual(len(raw_storage_events), 1)
        self.assertEqual(raw_storage_events[0]["event_key"], "ex001:raw-storage:stdout:1")
        self.assertEqual(len(terminal_events), 1)


if __name__ == "__main__":
    unittest.main()
