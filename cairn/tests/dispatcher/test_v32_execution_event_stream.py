from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import WorkerProcessRun, record_remote_session
from cairn.dispatcher.tasks.common import run_worker_process
from cairn.dispatcher.tasks.explore import _mark_execution_before_stream_finished, _mark_execution_postprocess_failed
from cairn.dispatcher.tasks.questions import run_question_task
from cairn.dispatcher.workers.registry import get_driver


class FakeProcess:
    def __init__(self, run_logger=None) -> None:
        self.run_logger = run_logger

    def start(self) -> None:
        if self.run_logger is not None:
            self.run_logger.write_stream("stdout", "hello\n")
            self.run_logger.write_stream("stderr", "warn\n")

    def communicate(self, timeout: float | None) -> ProcessResult:
        return ProcessResult(returncode=0, stdout="hello\n", stderr="warn\n")

    def kill(self) -> None:
        return None

    def cancel(self, reason: str) -> None:
        return None


class FailingProcess(FakeProcess):
    def start(self) -> None:
        if self.run_logger is not None:
            self.run_logger.write_stream("stderr", "driver exploded\n")

    def communicate(self, timeout: float | None) -> ProcessResult:
        return ProcessResult(returncode=2, stdout="", stderr="driver exploded\n")


class ClaudeJsonProcess(FakeProcess):
    def start(self) -> None:
        if self.run_logger is not None:
            self.run_logger.write_stream("stdout", '{"type":"system","subtype":"init","session_id":"claude-session-1"}\n')
            self.run_logger.write_stream(
                "stdout",
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"partial"}]}}\n',
            )
            self.run_logger.write_stream(
                "stdout",
                '{"type":"result","result":"final answer","session_id":"claude-session-1"}\n',
            )
            self.run_logger.write_stream("stderr", "debug line\n")

    def communicate(self, timeout: float | None) -> ProcessResult:
        return ProcessResult(
            returncode=0,
            stdout=(
                '{"type":"system","subtype":"init","session_id":"claude-session-1"}\n'
                '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"partial"}]}}\n'
                '{"type":"result","result":"final answer","session_id":"claude-session-1"}\n'
            ),
            stderr="debug line\n",
        )


class ExplodingProjector:
    def feed(self, stream: str, text: str):
        raise RuntimeError("parser exploded")

    def close(self):
        return []


class FakeEnvironment:
    id = "ssh-main"
    label = "SSH Main"
    backend = "ssh"

    def prepare_project(self, project_id: str) -> EnvironmentHandle:
        return EnvironmentHandle(project_id=project_id, target_name="remote", workspace="/tmp/work")

    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        return FakeProcess(run_logger=run_logger)


class ClaudeJsonEnvironment(FakeEnvironment):
    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        return ClaudeJsonProcess(run_logger=run_logger)


class FailingEnvironment(FakeEnvironment):
    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        return FailingProcess(run_logger=run_logger)


class FakeClient:
    def __init__(self) -> None:
        self.batches: list[list[dict]] = []
        self.patches: list[dict] = []
        self.heartbeats: list[dict] = []

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, sink_token: str | None = None, events: list[dict]):
        self.batches.append(events)
        return type("Response", (), {"ok": True, "status_code": 200, "text": ""})()

    def patch_execution(self, execution_id: str, payload: dict):
        self.patches.append(payload)
        return type("Response", (), {"ok": True, "status_code": 200, "text": ""})()

    def heartbeat_execution(self, execution_id: str, *, dispatcher_id: str, sink_token: str | None = None, lease_seconds: int | None = None):
        self.heartbeats.append(
            {
                "execution_id": execution_id,
                "dispatcher_id": dispatcher_id,
                "sink_token": sink_token,
                "lease_seconds": lease_seconds,
            }
        )
        return type("Response", (), {"ok": True, "status_code": 200, "text": ""})()


def _dispatch_config(interval: int = 30):
    return SimpleNamespace(runtime=SimpleNamespace(interval=interval))


class V32ExecutionEventStreamTests(unittest.TestCase):
    def test_run_worker_process_dual_writes_jsonl_and_execution_events(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            self.addCleanupEnv("CAIRN_RUN_LOG_DIR")
            import os

            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            worker = WorkerConfig(
                name="codex-main",
                type="codex",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={"SECRET_TOKEN": "redact-me"},
            )
            handle = EnvironmentHandle(project_id="proj_001", target_name="remote", workspace="/tmp/work")
            client = FakeClient()
            sink = ExecutionEventSink(client, "proj_001_ex001", batch_size=10, secrets=["redact-me"])

            run = run_worker_process(
                FakeEnvironment(),
                handle,
                worker,
                ["true"],
                phase="explore_execute",
                timeout_seconds=5,
                project_id="proj_001",
                task_type="explore",
                intent_id="i001",
                event_sink=sink,
            )

        self.assertEqual(run.returncode, 0)
        self.assertTrue(run.run_log_id and run.run_log_id.startswith("run_"))
        flattened = [event for batch in client.batches for event in batch]
        self.assertEqual([event["event_type"] for event in flattened], ["status", "stdout", "stderr", "status"])
        self.assertEqual(flattened[0]["payload"]["status"], "running")
        self.assertEqual(flattened[1]["payload"]["text"], "hello\n")
        self.assertEqual(flattened[-1]["payload"]["status"], "succeeded")
        self.assertEqual(client.patches[-1]["status"], "succeeded")
        self.assertFalse(run.event_flush_failed)

    def test_run_worker_process_patches_failed_execution_with_driver_error_detail(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            self.addCleanupEnv("CAIRN_RUN_LOG_DIR")
            import os

            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            worker = WorkerConfig(
                name="pi-main",
                type="pi",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={},
            )
            handle = EnvironmentHandle(project_id="proj_001", target_name="remote", workspace="/tmp/work")
            client = FakeClient()
            sink = ExecutionEventSink(client, "proj_001_ex001", batch_size=10)

            run = run_worker_process(
                FailingEnvironment(),
                handle,
                worker,
                ["pi"],
                phase="explore_execute",
                timeout_seconds=5,
                project_id="proj_001",
                task_type="explore",
                intent_id="i001",
                event_sink=sink,
            )

        self.assertEqual(run.returncode, 2)
        self.assertEqual(client.patches[-1]["status"], "failed")
        self.assertEqual(client.patches[-1]["error_code"], "worker_process_failed")
        self.assertIn("driver exploded", client.patches[-1]["error_detail"])

    def test_claudecode_stdout_stderr_reach_raw_and_messages_are_projected(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            self.addCleanupEnv("CAIRN_RUN_LOG_DIR")
            import os

            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            worker = WorkerConfig(
                name="claude-main",
                type="claudecode",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={"ANTHROPIC_MODEL": "claude-test"},
            )
            handle = EnvironmentHandle(project_id="proj_001", target_name="remote", workspace="/tmp/work")
            client = FakeClient()
            driver = get_driver("claudecode")
            sink = ExecutionEventSink(
                client,
                "proj_001_ex003",
                batch_size=10,
                event_projector=driver.stream_event_projector("proj_001_ex003"),
            )

            run = run_worker_process(
                ClaudeJsonEnvironment(),
                handle,
                worker,
                ["claude"],
                phase="question_resume",
                timeout_seconds=5,
                project_id="proj_001",
                task_type="question",
                event_sink=sink,
            )

        flattened = [event for batch in client.batches for event in batch]
        self.assertEqual(run.returncode, 0)
        self.assertTrue(any(event["event_type"] == "stdout" and "final answer" in event["payload"]["text"] for event in flattened))
        self.assertTrue(any(event["event_type"] == "stderr" and "debug line" in event["payload"]["text"] for event in flattened))
        assistant_messages = [event for event in flattened if event["event_type"] == "message" and event.get("role") == "assistant"]
        self.assertTrue(any(event["payload"]["text"] == "final answer" for event in assistant_messages))
        self.assertTrue(any(event["event_type"] == "session" and event["payload"]["id"] == "claude-session-1" for event in flattened))

    def test_projector_failure_keeps_raw_and_emits_system_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            self.addCleanupEnv("CAIRN_RUN_LOG_DIR")
            import os

            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            worker = WorkerConfig(
                name="claude-main",
                type="claudecode",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={"ANTHROPIC_MODEL": "claude-test"},
            )
            handle = EnvironmentHandle(project_id="proj_001", target_name="remote", workspace="/tmp/work")
            client = FakeClient()
            sink = ExecutionEventSink(client, "proj_001_ex004", batch_size=10, event_projector=ExplodingProjector())

            run_worker_process(
                ClaudeJsonEnvironment(),
                handle,
                worker,
                ["claude"],
                phase="question_resume",
                timeout_seconds=5,
                project_id="proj_001",
                task_type="question",
                event_sink=sink,
            )

        flattened = [event for batch in client.batches for event in batch]
        self.assertTrue(any(event["event_type"] == "stdout" and "final answer" in event["payload"]["text"] for event in flattened))
        diagnostics = [event for event in flattened if event["event_type"] == "message" and event.get("role") == "system"]
        self.assertTrue(any("parser exploded" in event["payload"]["text"] for event in diagnostics))

    def test_question_execution_task_writes_execution_events(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            self.addCleanupEnv("CAIRN_RUN_LOG_DIR")
            import os

            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            worker = WorkerConfig(
                name="mock-observer",
                type="mock",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={},
            )
            client = FakeClient()
            project = SimpleNamespace(project=SimpleNamespace(id="proj_001"))
            job = {
                "id": "proj_001_ex002",
                "project_id": "proj_001",
                "branch_id": "proj_001_br001",
                "task_type": "question",
                "phase": "followup",
                "session_action": "branch_continue",
                "remote_session_in_id": "fork-session-1",
                "input_snapshot": {"message": "what did I say?"},
            }

            outcome = run_question_task(
                _dispatch_config(),
                client,
                FakeEnvironment(),
                project,
                worker,
                job,
                cancellation=SimpleNamespace(attach_process=lambda process: None),
            )

        flattened = [event for batch in client.batches for event in batch]
        self.assertEqual(outcome, "success")
        self.assertIn({"dispatcher_id": "dispatcher", "status": "running"}, client.patches)
        self.assertTrue(any(patch.get("remote_session_out_id") == "fork-session-1" for patch in client.patches))
        self.assertIn("assistant", [event.get("role") for event in flattened])
        self.assertIn("stdout", [event["event_type"] for event in flattened])
        assistant_index = next(i for i, event in enumerate(flattened) if event.get("role") == "assistant")
        terminal_index = next(i for i, event in enumerate(flattened) if event["event_type"] == "status" and event["payload"].get("status") == "succeeded")
        self.assertLess(assistant_index, terminal_index)
        self.assertEqual(client.patches[-1]["status"], "succeeded")

    def test_claudecode_question_final_message_precedes_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            self.addCleanupEnv("CAIRN_RUN_LOG_DIR")
            import os

            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            worker = WorkerConfig(
                name="claude-main",
                type="claudecode",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={"ANTHROPIC_MODEL": "claude-test"},
            )
            client = FakeClient()
            project = SimpleNamespace(project=SimpleNamespace(id="proj_001"))
            job = {
                "id": "proj_001_ex005",
                "project_id": "proj_001",
                "branch_id": "proj_001_br001",
                "task_type": "question",
                "phase": "followup",
                "session_action": "branch_continue",
                "remote_session_in_id": "claude-session-0",
                "input_snapshot": {"message": "what did I say?"},
            }

            outcome = run_question_task(
                _dispatch_config(),
                client,
                ClaudeJsonEnvironment(),
                project,
                worker,
                job,
                cancellation=SimpleNamespace(attach_process=lambda process: None),
            )

        flattened = [event for batch in client.batches for event in batch]
        assistant_indexes = [
            i for i, event in enumerate(flattened)
            if event["event_type"] == "message" and event.get("role") == "assistant"
        ]
        terminal_index = next(
            i for i, event in enumerate(flattened)
            if event["event_type"] == "status" and event["payload"].get("status") == "succeeded"
        )
        self.assertEqual(outcome, "success")
        self.assertTrue(assistant_indexes)
        self.assertLess(max(assistant_indexes), terminal_index)

    def test_postprocess_failure_marks_execution_failed_and_adds_diagnostic(self) -> None:
        client = FakeClient()

        _mark_execution_postprocess_failed(
            client,
            "proj_001_ex006",
            "missing_report",
            "worker returned JSON but report is missing",
        )

        flattened = [event for batch in client.batches for event in batch]
        self.assertEqual(client.patches[-1]["status"], "failed")
        self.assertEqual(client.patches[-1]["error_code"], "missing_report")
        self.assertIn("report is missing", client.patches[-1]["error_detail"])
        self.assertTrue(any(event["event_type"] == "message" and event.get("role") == "system" for event in flattened))
        self.assertEqual(flattened[-1]["event_type"], "status")
        self.assertEqual(flattened[-1]["payload"]["status"], "failed")
        self.assertEqual(flattened[-1]["payload"]["error_code"], "missing_report")

    def test_pre_stream_failure_marks_execution_failed_and_adds_diagnostic(self) -> None:
        client = FakeClient()

        _mark_execution_before_stream_finished(
            client,
            "proj_001_ex007",
            "failed",
            "runtime_healthcheck_timeout",
            "dispatcher worker healthcheck failed before stdout/stderr streaming started",
        )

        flattened = [event for batch in client.batches for event in batch]
        self.assertEqual(client.patches[-1]["status"], "failed")
        self.assertEqual(client.patches[-1]["error_code"], "runtime_healthcheck_timeout")
        self.assertIn("healthcheck failed", client.patches[-1]["error_detail"])
        self.assertEqual(flattened[-2]["event_type"], "message")
        self.assertEqual(flattened[-2]["role"], "system")
        self.assertIn("before stdout/stderr streaming started", flattened[-2]["payload"]["text"])
        self.assertEqual(flattened[-1]["event_type"], "status")
        self.assertEqual(flattened[-1]["payload"]["status"], "failed")
        self.assertEqual(flattened[-1]["payload"]["error_code"], "runtime_healthcheck_timeout")

    def test_record_remote_session_updates_execution_run_session(self) -> None:
        client = FakeClient()
        driver = SimpleNamespace(
            extract_session_provenance=lambda prepared, stdout, stderr: SimpleNamespace(
                id="pi-session-1",
                kind="pi_session",
                status="available",
                capture_method="stdout_event",
            )
        )
        run = WorkerProcessRun(
            result=ProcessResult(returncode=0, stdout='{"type":"session","id":"pi-session-1"}\n', stderr=""),
            run_log_id=None,
            run_log_path=None,
        )

        session_id = record_remote_session(
            client,
            "proj_001",
            run,
            driver,
            prepared_session=None,
            execution_id="proj_001_ex001",
        )

        self.assertEqual(session_id, "pi-session-1")
        self.assertEqual(
            client.patches[-1],
            {
                "remote_session_out_kind": "pi_session",
                "remote_session_out_id": "pi-session-1",
                "remote_session_out_status": "available",
            },
        )

    def addCleanupEnv(self, key: str) -> None:
        import os

        old = os.environ.get(key)

        def restore() -> None:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

        self.addCleanup(restore)


if __name__ == "__main__":
    unittest.main()
