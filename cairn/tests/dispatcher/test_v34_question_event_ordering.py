from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.questions import run_question_task


class FakeResponse:
    ok = True
    status_code = 200
    text = ""


class FakeClient:
    def __init__(self) -> None:
        self.patches: list[dict] = []
        self.finishes: list[dict] = []

    def patch_execution(self, execution_id: str, payload: dict):
        self.patches.append(payload)
        return FakeResponse()

    def append_execution_events(self, execution_id: str, *, dispatcher_id: str | None = None, events: list[dict]):
        raise AssertionError("question final must not bypass finish barrier")

    def finish_execution(self, execution_id: str, *, dispatcher_id: str, events: list[dict], patch: dict):
        self.finishes.append({"events": events, "patch": patch})
        return FakeResponse()


class FakeProcess:
    def __init__(self, run_logger=None, stdout_text: str = "answer\n") -> None:
        self.run_logger = run_logger
        self.stdout_text = stdout_text

    def start(self) -> None:
        if self.run_logger is not None:
            self.run_logger.write_stream("stdout", self.stdout_text)

    def communicate(self, timeout: float | None) -> ProcessResult:
        return ProcessResult(returncode=0, stdout=self.stdout_text, stderr="")

    def kill(self) -> None:
        return None

    def cancel(self, reason: str) -> None:
        return None


class FakeEnvironment:
    id = "mock-env"
    label = "Mock"
    backend = "ssh"

    def __init__(self, stdout_text: str = "answer\n") -> None:
        self.stdout_text = stdout_text

    def prepare_project(self, project_id: str) -> EnvironmentHandle:
        return EnvironmentHandle(project_id=project_id, target_name="remote", workspace="/tmp/work")

    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        return FakeProcess(run_logger=run_logger, stdout_text=self.stdout_text)


class V34QuestionEventOrderingTests(unittest.TestCase):
    def test_question_final_and_terminal_status_are_submitted_in_single_finish(self) -> None:
        with tempfile.TemporaryDirectory() as run_dir:
            import os

            old = os.environ.get("CAIRN_RUN_LOG_DIR")
            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            self.addCleanup(lambda: os.environ.pop("CAIRN_RUN_LOG_DIR", None) if old is None else os.environ.__setitem__("CAIRN_RUN_LOG_DIR", old))

            client = FakeClient()
            worker = WorkerConfig(name="mock-observer", type="mock", task_types=["explore"], max_running=1, priority=0, env={})
            project = SimpleNamespace(project=SimpleNamespace(id="proj_001"))
            job = {
                "id": "proj_001_ex001",
                "project_id": "proj_001",
                "branch_id": "proj_001_br001",
                "task_type": "question",
                "phase": "followup",
                "session_action": "branch_continue",
                "remote_session_in_id": "session-in",
                "input_snapshot": {"message": "question"},
            }

            outcome = run_question_task(
                SimpleNamespace(),
                client,
                FakeEnvironment(),
                project,
                worker,
                job,
                cancellation=SimpleNamespace(attach_process=lambda process: None),
            )

        events = client.finishes[-1]["events"]
        assistant_index = next(i for i, event in enumerate(events) if event["event_type"] == "message" and event.get("role") == "assistant")
        terminal_index = next(i for i, event in enumerate(events) if event["event_type"] == "status" and event["payload"].get("status") == "succeeded")
        self.assertEqual(outcome, "success")
        self.assertEqual(client.finishes[-1]["patch"]["status"], "succeeded")
        self.assertLess(assistant_index, terminal_index)

    def test_projected_assistant_message_is_not_duplicated_by_stdout_fallback(self) -> None:
        stdout = (
            '{"type":"session","version":3,"id":"sess-out"}\n'
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"answer"}]}}\n'
        )
        with tempfile.TemporaryDirectory() as run_dir:
            import os

            old = os.environ.get("CAIRN_RUN_LOG_DIR")
            os.environ["CAIRN_RUN_LOG_DIR"] = run_dir
            self.addCleanup(lambda: os.environ.pop("CAIRN_RUN_LOG_DIR", None) if old is None else os.environ.__setitem__("CAIRN_RUN_LOG_DIR", old))

            client = FakeClient()
            worker = WorkerConfig(
                name="pi-e2e",
                type="pi",
                task_types=["explore"],
                max_running=1,
                priority=0,
                env={
                    "PI_MODEL": "gpt-5.4",
                    "PI_BASE_URL": "http://example.invalid/v1",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_API_KEY": "sk-test",
                },
            )
            project = SimpleNamespace(project=SimpleNamespace(id="proj_001"))
            job = {
                "id": "proj_001_ex001",
                "project_id": "proj_001",
                "branch_id": "proj_001_br001",
                "task_type": "question",
                "phase": "followup",
                "session_action": "branch_continue",
                "remote_session_in_id": "sess-in",
                "input_snapshot": {"message": "question"},
            }

            outcome = run_question_task(
                SimpleNamespace(),
                client,
                FakeEnvironment(stdout_text=stdout),
                project,
                worker,
                job,
                cancellation=SimpleNamespace(attach_process=lambda process: None),
            )

        assistant_events = [
            event
            for event in client.finishes[-1]["events"]
            if event["event_type"] == "message" and event.get("role") == "assistant"
        ]
        self.assertEqual(outcome, "success")
        self.assertEqual(len(assistant_events), 1)
        self.assertEqual(assistant_events[0]["payload"]["text"], "answer")
        self.assertEqual(assistant_events[0]["payload"]["stream_key"], "proj_001_ex001:pi:text")


if __name__ == "__main__":
    unittest.main()
