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

    def test_initial_project_without_auto_reason_does_not_create_bootstrap(self) -> None:
        loop = self._loop()
        created = []
        dispatched = []
        loop._create_bootstrap_intent = lambda *args: created.append(args) or None
        loop._dispatch_bootstrap = lambda *args: dispatched.append(args) or True
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001", auto_reason=False),
            intents=[],
        )

        result = loop._dispatch_initial_project(project, SimpleNamespace())

        self.assertFalse(result)
        self.assertEqual(created, [])
        self.assertEqual(dispatched, [])

    def test_manual_bootstrap_intent_dispatches_when_auto_reason_disabled(self) -> None:
        loop = self._loop()
        created = []
        dispatched = []
        loop._create_bootstrap_intent = lambda *args: created.append(args) or None
        loop._dispatch_bootstrap = lambda *args: dispatched.append(args) or True
        intent = SimpleNamespace(
            id="i001",
            from_=["origin"],
            to=None,
            worker=None,
            creator=BOOTSTRAP_INTENT_CREATOR,
            description=BOOTSTRAP_INTENT_DESCRIPTION,
            control_state="normal",
            created_at="2026-05-19T00:00:00Z",
        )
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001", auto_reason=False),
            intents=[intent],
        )

        result = loop._dispatch_initial_project(project, SimpleNamespace())

        self.assertTrue(result)
        self.assertEqual(created, [])
        self.assertEqual(dispatched[0][1], intent)

    def test_terminal_bootstrap_worker_projection_does_not_block_redispatch(self) -> None:
        loop = self._loop()
        dispatched = []
        loop._dispatch_bootstrap = lambda *args: dispatched.append(args) or True
        intent = SimpleNamespace(
            id="i001",
            from_=["origin"],
            to=None,
            worker="Human",
            active_execution_id=None,
            latest_execution_id="ex001",
            runtime_status="failed",
            active_worker_name=None,
            latest_worker_name="Human",
            worker_name="Human",
            creator=BOOTSTRAP_INTENT_CREATOR,
            description=BOOTSTRAP_INTENT_DESCRIPTION,
            control_state="normal",
            created_at="2026-05-19T00:00:00Z",
        )
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001", auto_reason=False),
            intents=[intent],
        )

        result = loop._dispatch_initial_project(project, SimpleNamespace())

        self.assertTrue(result)
        self.assertEqual(dispatched[0][1], intent)

    def test_active_bootstrap_execution_blocks_redispatch(self) -> None:
        loop = self._loop()
        dispatched = []
        loop._dispatch_bootstrap = lambda *args: dispatched.append(args) or True
        intent = SimpleNamespace(
            id="i001",
            from_=["origin"],
            to=None,
            worker="alpha",
            active_execution_id="ex001",
            latest_execution_id="ex001",
            runtime_status="leased",
            active_worker_name="alpha",
            latest_worker_name="alpha",
            worker_name="alpha",
            creator=BOOTSTRAP_INTENT_CREATOR,
            description=BOOTSTRAP_INTENT_DESCRIPTION,
            control_state="normal",
            created_at="2026-05-19T00:00:00Z",
        )
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001", auto_reason=False),
            intents=[intent],
        )

        result = loop._dispatch_initial_project(project, SimpleNamespace())

        self.assertFalse(result)
        self.assertEqual(dispatched, [])

    def test_auto_bootstrap_uses_allowed_auto_workers(self) -> None:
        loop = self._loop()
        captured = {}
        loop._select_worker = (
            lambda project_id, task_type, environment_id, **kwargs: captured.update(
                {
                    "project_id": project_id,
                    "task_type": task_type,
                    "environment_id": environment_id,
                    **kwargs,
                }
            )
            or SimpleNamespace(
                worker=None,
                blocked_busy=[],
                blocked_unhealthy=[],
                blocked_rejected=[],
            )
        )
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001", auto_reason=True, allowed_auto_workers=["beta"]),
        )
        intent = SimpleNamespace(id="i001", requested_worker=None)

        result = loop._dispatch_bootstrap(project, intent, SimpleNamespace(id="docker-default"))

        self.assertFalse(result)
        self.assertEqual(captured["task_type"], "bootstrap")
        self.assertEqual(captured["allowed_auto_workers"], ["beta"])
        self.assertIsNone(captured["requested_worker"])

    def test_manual_bootstrap_requested_worker_bypasses_auto_scope(self) -> None:
        loop = self._loop()
        captured = {}
        loop._select_worker = (
            lambda project_id, task_type, environment_id, **kwargs: captured.update(kwargs)
            or SimpleNamespace(
                worker=None,
                blocked_busy=[],
                blocked_unhealthy=[],
                blocked_rejected=[],
            )
        )
        project = SimpleNamespace(
            project=SimpleNamespace(id="proj_001", auto_reason=False, allowed_auto_workers=["beta"]),
        )
        intent = SimpleNamespace(id="i001", requested_worker="alpha")

        result = loop._dispatch_bootstrap(project, intent, SimpleNamespace(id="docker-default"))

        self.assertFalse(result)
        self.assertEqual(captured["requested_worker"], "alpha")
        self.assertIsNone(captured["allowed_auto_workers"])

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
