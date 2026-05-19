from __future__ import annotations

from types import SimpleNamespace
import unittest

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.scheduler.loop import BOOTSTRAP_INTENT_CREATOR, BOOTSTRAP_INTENT_DESCRIPTION, DispatcherLoop
from cairn.dispatcher.tasks.common import best_effort_release_after_conclude_failure
from cairn.dispatcher.protocol.client import ApiResult
from cairn.server.models import ProviderEndpointPublic, WorkEnvironmentPublic


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
        {"name": "beta", "type": "mock", "task_types": ["explore", "reason"], "max_running": 1, "priority": 1},
    ],
}


class CommandBlackboardV2SelectionTests(unittest.TestCase):
    def _loop(self, config_data=None):
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = DispatchConfig.model_validate(config_data or BASE_CONFIG)
        loop.futures = {}
        loop.worker_unhealthy_until = {}
        loop.worker_rejected_until = {}
        loop._log_state = {}
        loop.environment_metadata = {
            "docker-default": WorkEnvironmentPublic(
                id="docker-default",
                label="Docker",
                backend="docker",
                provider_endpoints=[
                    ProviderEndpointPublic(id="unused", type="mock", base_url="http://mock"),
                ],
            )
        }
        return loop

    def test_requested_worker_only_selects_same_name(self) -> None:
        loop = self._loop()

        selection = loop._select_worker("proj_001", "explore", "docker-default", requested_worker="beta")

        self.assertEqual(selection.worker.name, "beta")
        self.assertIn("alpha", selection.blocked_requested_worker)

    def test_missing_requested_worker_is_blocked_without_fallback(self) -> None:
        loop = self._loop()

        selection = loop._select_worker("proj_001", "explore", "docker-default", requested_worker="missing")

        self.assertIsNone(selection.worker)
        self.assertEqual(selection.blocked_missing_worker, ["missing"])

    def test_allowed_auto_workers_limits_unrequested_selection(self) -> None:
        loop = self._loop()

        selection = loop._select_worker("proj_001", "explore", "docker-default", allowed_auto_workers=["beta"])

        self.assertEqual(selection.worker.name, "beta")
        self.assertIn("alpha", selection.blocked_auto_worker_scope)

    def test_requested_worker_ignores_allowed_auto_scope(self) -> None:
        loop = self._loop()

        selection = loop._select_worker(
            "proj_001",
            "explore",
            "docker-default",
            requested_worker="alpha",
            allowed_auto_workers=["beta"],
        )

        self.assertEqual(selection.worker.name, "alpha")

    def test_conclude_requested_cancels_running_task(self) -> None:
        loop = self._loop()
        cancellation = SimpleNamespace(cancelled=[], cancel=lambda reason: cancellation.cancelled.append(reason) or True)
        loop.futures = {
            object(): SimpleNamespace(project_id="proj_001", intent_id="i001", worker_name="alpha", cancellation=cancellation)
        }
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001"),
            intents=[SimpleNamespace(id="i001", control_state="conclude_requested")],
        )

        loop._cancel_control_requested_tasks(project)

        self.assertEqual(cancellation.cancelled, ["conclude_requested"])

    def test_conclude_requested_bootstrap_is_not_redispatched_after_release(self) -> None:
        loop = self._loop()
        dispatched = []
        loop._dispatch_bootstrap = lambda *args: dispatched.append(args) or True
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001"),
            intents=[
                SimpleNamespace(
                    id="i001",
                    from_=["origin"],
                    to=None,
                    worker=None,
                    creator=BOOTSTRAP_INTENT_CREATOR,
                    description=BOOTSTRAP_INTENT_DESCRIPTION,
                    control_state="conclude_requested",
                    created_at="2026-05-19T00:00:00Z",
                )
            ],
        )

        result = loop._dispatch_initial_project(project, SimpleNamespace())

        self.assertFalse(result)
        self.assertEqual(dispatched, [])

    def test_conclude_failure_release_preserves_control_state(self) -> None:
        calls = []

        class Client:
            def release(self, project_id, intent_id, worker):
                calls.append(("release", project_id, intent_id, worker))
                return ApiResult(200, data={})

        best_effort_release_after_conclude_failure(Client(), "proj_001", "i001", "alpha")

        self.assertEqual(
            calls,
            [
                ("release", "proj_001", "i001", "alpha"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
