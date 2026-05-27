from __future__ import annotations

import unittest

from cairn.dispatcher.runtime.event_sink import ExecutionEventSink


class FakeClient:
    def __init__(self) -> None:
        self.fail = True
        self.calls: list[list[dict]] = []
        self.order: list[str] = []
        self.patches: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, sink_token: str | None = None, events: list[dict]):
        self.order.append("append")
        self.calls.append(events)
        ok = not self.fail
        return type("Response", (), {"ok": ok, "status_code": 200 if ok else 503, "text": "down" if not ok else ""})()

    def patch_execution(self, execution_id: str, payload: dict):
        self.order.append("patch")
        self.patches.append(payload)
        return type("Response", (), {"ok": True, "status_code": 200, "text": ""})()


class V32EventSinkRetryTests(unittest.TestCase):
    def test_failed_flush_keeps_queue_for_retry(self) -> None:
        client = FakeClient()
        sink = ExecutionEventSink(client, "proj_001_ex001", batch_size=99)
        sink.write_stream("stdout", "one")

        self.assertFalse(sink.flush())
        self.assertEqual(sink.failed_flushes, 1)

        client.fail = False
        self.assertTrue(sink.flush())

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1][0]["payload"]["text"], "one")

    def test_terminal_events_are_flushed_before_terminal_status_patch(self) -> None:
        client = FakeClient()
        client.fail = False
        sink = ExecutionEventSink(client, "proj_001_ex001", batch_size=99)

        self.assertTrue(sink.close(terminal_status="succeeded", returncode=0))

        self.assertEqual(client.order, ["append", "patch"])
        self.assertEqual(client.calls[0][0]["event_type"], "status")
        self.assertEqual(client.calls[0][0]["payload"]["status"], "succeeded")
        self.assertEqual(client.patches[0]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
