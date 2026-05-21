from __future__ import annotations

import unittest

from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import (
    WorkerProcessRun,
    finish_deferred_worker_process,
    write_conclude_result,
)


class FakeResponse:
    def __init__(self, ok: bool, status_code: int = 200, text: str = "", data: dict | None = None) -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self.data = data or {}


class RecordingClient:
    def __init__(self, *, submit_ok: bool = True) -> None:
        self.submit_ok = submit_ok
        self.calls: list[str] = []
        self.append_batches: list[list[dict]] = []
        self.finish_calls: list[dict] = []
        self.conclude_calls: list[dict] = []
        self.release_calls: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, events: list[dict]):
        self.calls.append("append")
        self.append_batches.append(events)
        return FakeResponse(True)

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, events: list[dict], patch: dict):
        self.calls.append("finish")
        self.finish_calls.append({"events": events, "patch": patch})
        return FakeResponse(True)

    def submit_execution_conclusion_report(self, execution_id: str, payload: dict):
        self.calls.append("submit_report")
        if not self.submit_ok:
            return FakeResponse(False, 500, "down")
        return FakeResponse(True, data={"fact": {"id": "f001"}})

    def conclude(self, project_id: str, intent_id: str, worker_name: str, description: str, *, title=None, metadata=None):
        self.conclude_calls.append(
            {"project_id": project_id, "intent_id": intent_id, "worker_name": worker_name, "description": description}
        )
        return FakeResponse(True, data={"fact": {"id": "legacy"}})

    def release(self, project_id: str, intent_id: str, worker_name: str):
        self.release_calls.append({"project_id": project_id, "intent_id": intent_id, "worker_name": worker_name})
        return FakeResponse(True)


class V34DeferredTerminalConclusionTests(unittest.TestCase):
    def test_execution_report_is_written_before_terminal_success(self) -> None:
        client = RecordingClient()
        sink = ExecutionEventSink(client, "proj_001_ex001", batch_size=99)
        sink.write_stream("stdout", '{"accepted":true}\n')
        self.assertTrue(sink.close())
        run = WorkerProcessRun(
            ProcessResult(returncode=0, stdout='{"accepted":true}\n', stderr=""),
            event_sink=sink,
        )

        outcome = write_conclude_result(
            client,
            "proj_001",
            "i001",
            "pi",
            "Useful result",
            title="Useful",
            source="explore_execute",
            phase_ms=123,
            execution_id="proj_001_ex001",
        )
        finish_deferred_worker_process(client, "proj_001_ex001", run, "succeeded", returncode=0)

        self.assertEqual(outcome, "success")
        self.assertEqual(client.conclude_calls, [])
        self.assertEqual(client.calls, ["append", "submit_report", "finish"])
        self.assertEqual(client.finish_calls[0]["patch"]["status"], "succeeded")
        self.assertEqual(client.finish_calls[0]["patch"]["returncode"], 0)

    def test_conclusion_report_failure_does_not_finish_succeeded(self) -> None:
        client = RecordingClient(submit_ok=False)
        sink = ExecutionEventSink(client, "proj_001_ex002", batch_size=99)
        sink.write_stream("stdout", '{"accepted":true}\n')
        self.assertTrue(sink.close())
        run = WorkerProcessRun(
            ProcessResult(returncode=0, stdout='{"accepted":true}\n', stderr=""),
            event_sink=sink,
        )

        outcome = write_conclude_result(
            client,
            "proj_001",
            "i001",
            "pi",
            "Useful result",
            title="Useful",
            source="explore_execute",
            phase_ms=123,
            execution_id="proj_001_ex002",
        )
        finish_deferred_worker_process(
            client,
            "proj_001_ex002",
            run,
            "failed",
            error_code="conclude_write_failed",
            error_detail="dispatcher parsed a valid worker result but failed to write the fact",
        )

        self.assertEqual(outcome, "failed")
        self.assertTrue(client.release_calls)
        self.assertEqual(client.finish_calls[0]["patch"]["status"], "failed")
        self.assertNotEqual(client.finish_calls[0]["patch"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
