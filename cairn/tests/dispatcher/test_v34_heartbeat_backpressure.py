from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.runtime.heartbeat import HeartbeatFailure
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import run_worker_process


class FakeResponse:
    ok = True
    status_code = 200
    text = ""


class FakeClient:
    def __init__(self) -> None:
        self.finishes: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, events: list[dict]):
        return FakeResponse()

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, events: list[dict], patch: dict):
        self.finishes.append({"events": events, "patch": patch})
        return FakeResponse()

    def patch_execution(self, execution_id: str, payload: dict):
        return FakeResponse()


class FakeProcess:
    def __init__(self, run_logger=None) -> None:
        self.run_logger = run_logger

    def start(self) -> None:
        return None

    def communicate(self, timeout: float | None) -> ProcessResult:
        return ProcessResult(returncode=1, stdout="", stderr="killed", cancelled=True, cancel_reason="heartbeat")

    def kill(self) -> None:
        return None

    def cancel(self, reason: str) -> None:
        return None


class FakeEnvironment:
    id = "ssh-main"
    label = "SSH Main"
    backend = "ssh"

    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        return FakeProcess(run_logger=run_logger)


class V34HeartbeatBackpressureTests(unittest.TestCase):
    def test_heartbeat_failure_is_visible_in_terminal_events(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            import os

            old = os.environ.get("CAIRN_RUN_LOG_DIR")
            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            self.addCleanup(lambda: os.environ.pop("CAIRN_RUN_LOG_DIR", None) if old is None else os.environ.__setitem__("CAIRN_RUN_LOG_DIR", old))

            client = FakeClient()
            sink = ExecutionEventSink(client, "ex001", batch_size=99)
            lease = SimpleNamespace(failure=HeartbeatFailure(0, "read timed out"), attach_process=lambda process: None)
            worker = WorkerConfig(name="pi-main", type="pi", task_types=["explore"], max_running=1, priority=0, env={})

            run_worker_process(
                FakeEnvironment(),
                EnvironmentHandle(project_id="proj_001", target_name="remote", workspace="/tmp/work"),
                worker,
                ["pi"],
                phase="question_resume",
                timeout_seconds=5,
                project_id="proj_001",
                task_type="question",
                lease=lease,
                event_sink=sink,
            )

        texts = [event["payload"].get("text", "") for event in client.finishes[-1]["events"] if event["event_type"] == "message"]
        self.assertTrue(any("heartbeat_cancelled" in text for text in texts))


if __name__ == "__main__":
    unittest.main()
