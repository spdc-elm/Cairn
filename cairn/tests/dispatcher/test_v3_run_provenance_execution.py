from __future__ import annotations

import tempfile
import unittest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import run_worker_process


class FakeProcess:
    def __init__(self, run_logger=None) -> None:
        self.run_logger = run_logger
        self.started = False

    def start(self) -> None:
        self.started = True
        if self.run_logger is not None:
            self.run_logger.write_stream("stdout", "hello\n")

    def communicate(self, timeout: float | None) -> ProcessResult:
        return ProcessResult(returncode=0, stdout="hello\n", stderr="")

    def kill(self) -> None:
        return None

    def cancel(self, reason: str) -> None:
        return None


class FakeEnvironment:
    id = "ssh-main"
    label = "SSH Main"
    backend = "ssh"

    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        self.last_process = FakeProcess(run_logger=run_logger)
        return self.last_process


class FakeRecorder:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.finished: list[dict] = []

    def start_run(self, **payload) -> None:
        self.started.append(payload)

    def finish_run(self, *, project_id: str, run_log_id: str, result: ProcessResult) -> None:
        self.finished.append({"project_id": project_id, "run_log_id": run_log_id, "returncode": result.returncode})


class V3RunProvenanceExecutionTests(unittest.TestCase):
    def test_run_worker_process_returns_run_log_id_and_records_lifecycle(self) -> None:
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
                env={},
            )
            handle = EnvironmentHandle(project_id="proj_001", target_name="remote", workspace="/tmp/work")
            recorder = FakeRecorder()

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
                provenance_recorder=recorder,
                extra_metadata={"report_path": "/tmp/work/.cairn/reports/execution.md", "report_run_id": "run_report_001"},
            )

        self.assertEqual(run.result.stdout, "hello\n")
        self.assertTrue(run.run_log_id.startswith("run_"))
        self.assertTrue(run.run_log_path.endswith(f"{run.run_log_id}.jsonl"))
        self.assertEqual(recorder.started[0]["run_log_id"], run.run_log_id)
        self.assertEqual(recorder.started[0]["worker_type"], "codex")
        self.assertEqual(recorder.started[0]["report_run_id"], "run_report_001")
        self.assertEqual(recorder.finished[0]["run_log_id"], run.run_log_id)

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
