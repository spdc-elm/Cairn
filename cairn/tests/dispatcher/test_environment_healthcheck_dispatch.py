from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop


BASE_CONFIG = {
    "server": "http://127.0.0.1:8000",
    "runtime": {
        "interval": 3,
        "max_workers": 2,
        "max_running_projects": 1,
        "max_project_workers": 2,
        "healthcheck_timeout": 2,
        "prompt_group": "mock",
    },
    "tasks": {
        "bootstrap": {"timeout": 9, "conclude_timeout": 5},
        "reason": {"timeout": 5, "max_intents": 3},
        "explore": {"timeout": 9, "conclude_timeout": 5},
    },
    "container": {
        "image": "ghcr.io/oritera/cairn-worker-container:latest",
        "network_mode": "host",
        "completed_action": "stop",
    },
    "workers": [
        {"name": "alpha", "type": "mock", "task_types": ["explore", "reason"], "max_running": 1, "priority": 0},
    ],
}


class FakeProcess:
    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result or ProcessResult(returncode=0, stdout="mock ok\n", stderr="")

    def start(self) -> None:
        return None

    def communicate(self, timeout=None) -> ProcessResult:
        return self.result

    def kill(self) -> None:
        return None

    def cancel(self, reason: str) -> None:
        return None


class FakeEnvironment:
    id = "docker-default"
    label = "Docker"
    backend = "docker"

    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result

    def prepare_startup(self) -> EnvironmentHandle:
        return EnvironmentHandle(project_id="startup", target_name="target", workspace="/tmp/work")

    def cleanup_startup(self, handle: EnvironmentHandle) -> None:
        return None

    def build_process(self, handle, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
        return FakeProcess(self.result)


class FakeClient:
    def __init__(self) -> None:
        self.patches: list[dict] = []
        self.event_batches: list[list[dict]] = []
        self.health: list[dict] = []

    def claim_healthcheck_executions(self, dispatcher_id, worker_names, environment_ids, *, limit=1, lease_seconds=60):
        return ApiResult(
            200,
            data=[
                {
                    "id": "__system_healthchecks___ex001",
                    "project_id": "__system_healthchecks__",
                    "worker_name": "alpha",
                    "worker_type": "mock",
                    "environment_id": "docker-default",
                    "endpoint_id": None,
                    "model_profile_id": None,
                }
            ],
        )

    def patch_execution(self, execution_id: str, payload: dict):
        self.patches.append(payload)
        return ApiResult(200, data={})

    def append_execution_events(self, execution_id: str, *, dispatcher_id=None, sink_token=None, events: list[dict]):
        self.event_batches.append(events)
        return ApiResult(200, data={})

    def upsert_worker_health(self, health: list[dict]):
        self.health.extend(health)
        return ApiResult(200, data=health)


class EnvironmentHealthcheckDispatchTests(unittest.TestCase):
    def test_dispatcher_claims_runs_and_publishes_manual_healthcheck(self) -> None:
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = DispatchConfig.model_validate(BASE_CONFIG)
        loop.futures = {}
        loop.environments = {"docker-default": FakeEnvironment()}
        loop.client = FakeClient()
        loop.executor = ThreadPoolExecutor(max_workers=1)
        loop.worker_unhealthy_until = {}
        loop.worker_rejected_until = {}
        loop.reason_checkpoints = {}
        loop._log_state = {}
        self.addCleanup(loop.executor.shutdown, True)

        self.assertTrue(loop._try_dispatch_healthcheck_execution())
        for future in list(loop.futures):
            future.result(timeout=2)
        loop._reap_futures()

        self.assertEqual(loop.futures, {})
        self.assertEqual(loop.client.health[0]["source"], "manual_healthcheck")
        self.assertEqual(loop.client.health[0]["environment_id"], "docker-default")
        self.assertEqual(loop.client.health[0]["worker_name"], "alpha")
        self.assertEqual(loop.client.health[0]["status"], "ok")
        flattened = [event for batch in loop.client.event_batches for event in batch]
        self.assertIn("stdout", [event["event_type"] for event in flattened])
        self.assertIn("metric", [event["event_type"] for event in flattened])
        self.assertIn("succeeded", [patch.get("status") for patch in loop.client.patches])

    def test_failed_healthcheck_without_output_writes_error_detail_event(self) -> None:
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = DispatchConfig.model_validate(BASE_CONFIG)
        loop.futures = {}
        loop.environments = {"docker-default": FakeEnvironment(ProcessResult(returncode=255, stdout="", stderr=""))}
        loop.client = FakeClient()
        loop.executor = ThreadPoolExecutor(max_workers=1)
        loop.worker_unhealthy_until = {}
        loop.worker_rejected_until = {}
        loop.reason_checkpoints = {}
        loop._log_state = {}
        self.addCleanup(loop.executor.shutdown, True)

        self.assertTrue(loop._try_dispatch_healthcheck_execution())
        for future in list(loop.futures):
            future.result(timeout=2)

        flattened = [event for batch in loop.client.event_batches for event in batch]
        stderr_events = [event for event in flattened if event["event_type"] == "stderr"]
        self.assertEqual(stderr_events[0]["payload"]["text"], "healthcheck exited with returncode 255 without stdout/stderr\n")
        self.assertIn("healthcheck_failed", [patch.get("error_code") for patch in loop.client.patches])

    def test_healthcheck_task_is_not_cancelled_by_hidden_system_project(self) -> None:
        loop = DispatcherLoop.__new__(DispatcherLoop)
        cancelled: list[str] = []
        cancellation = SimpleNamespace(cancel=lambda reason: cancelled.append(reason) or True)
        loop.futures = {
            object(): RunningTask(
                "__system_healthchecks__",
                "healthcheck",
                "alpha",
                cancellation,
                environment_id="docker-default",
                worker_type="mock",
                execution_id="__system_healthchecks___ex001",
            )
        }

        loop._cancel_inactive_tasks([])

        self.assertEqual(cancelled, [])


if __name__ == "__main__":
    unittest.main()
    def __init__(self, result: ProcessResult | None = None) -> None:
        self.result = result
