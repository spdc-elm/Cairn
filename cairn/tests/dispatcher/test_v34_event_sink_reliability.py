from __future__ import annotations

import unittest

from cairn.dispatcher.runtime.event_sink import ExecutionEventSink


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

    def test_queue_full_enters_failed_terminal_barrier(self) -> None:
        client = FinishFailClient()
        sink = ExecutionEventSink(client, "ex001", batch_size=99, max_queue_events=1, append_retry_delays=(0.0,))

        sink.write_stream("stdout", "one")
        sink.write_stream("stdout", "two")
        ok = sink.close(terminal_status="succeeded", returncode=0)

        self.assertFalse(ok)
        self.assertEqual(client.patches[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
