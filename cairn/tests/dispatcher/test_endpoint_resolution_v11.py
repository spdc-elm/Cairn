from __future__ import annotations

from types import SimpleNamespace
import unittest

from cairn.dispatcher.config import DispatchConfig, resolve_worker_env
from cairn.dispatcher.runtime.startup_healthcheck import run_startup_healthchecks
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import ProviderEndpointPublic, ProviderEndpointSecret, WorkEnvironmentPublic


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
    "model_profiles": [
        {
            "id": "pi-main",
            "type": "pi",
            "model": "gpt-test",
            "context_window": 12345,
        }
    ],
    "workers": [
        {
            "name": "pi",
            "type": "pi",
            "model_profile": "pi-main",
            "endpoint": "pi-default",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": 0,
        }
    ],
}


class EndpointResolutionTests(unittest.TestCase):
    def test_same_worker_resolves_different_environment_endpoint_values(self) -> None:
        config = DispatchConfig.model_validate(BASE_CONFIG)
        worker = config.workers[0]
        profile = config.model_profile_config(worker)
        assert profile is not None
        endpoint_a = ProviderEndpointSecret(
            id="pi-default",
            type="pi",
            base_url="http://10.0.0.2:3000/v1",
            provider_api="openai-completions",
            has_api_key=True,
            api_key="sk-a",
        )
        endpoint_b = ProviderEndpointSecret(
            id="pi-default",
            type="pi",
            base_url="http://host.docker.internal:3000",
            provider_api="openai-completions",
            has_api_key=True,
            api_key="sk-b",
        )

        env_a = resolve_worker_env(worker, profile, endpoint_a)
        env_b = resolve_worker_env(worker, profile, endpoint_b)

        self.assertEqual(env_a["PI_BASE_URL"], "http://10.0.0.2:3000/v1")
        self.assertEqual(env_a["PI_API_KEY"], "sk-a")
        self.assertEqual(env_b["PI_BASE_URL"], "http://host.docker.internal:3000/v1")
        self.assertEqual(env_b["PI_API_KEY"], "sk-b")

    def test_startup_healthcheck_reports_missing_endpoint_without_running_worker(self) -> None:
        config = DispatchConfig.model_validate(BASE_CONFIG)
        environment = SimpleNamespace(id="pentestvm", backend="ssh")

        results = run_startup_healthchecks(
            config,
            {"pentestvm": environment},
            environment_metadata={
                "pentestvm": WorkEnvironmentPublic(
                    id="pentestvm",
                    label="pentestVM",
                    backend="ssh",
                    provider_endpoints=[],
                )
            },
            endpoint_loader=lambda _environment_id, _endpoint_id: (_ for _ in ()).throw(AssertionError("should not load")),
        )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("missing endpoint", results[0].stderr_preview)

    def test_worker_selection_skips_worker_when_endpoint_missing(self) -> None:
        config = DispatchConfig.model_validate(BASE_CONFIG)
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = config
        loop.futures = {}
        loop.worker_unhealthy_until = {}
        loop.worker_rejected_until = {}
        loop.environment_metadata = {
            "pentestvm": WorkEnvironmentPublic(
                id="pentestvm",
                label="pentestVM",
                backend="ssh",
                provider_endpoints=[],
            )
        }

        selection = loop._select_worker("proj_001", "bootstrap", "pentestvm")

        self.assertIsNone(selection.worker)
        self.assertEqual(selection.blocked_endpoint, ["pi"])

    def test_worker_selection_accepts_matching_endpoint_metadata(self) -> None:
        config = DispatchConfig.model_validate(BASE_CONFIG)
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = config
        loop.futures = {}
        loop.worker_unhealthy_until = {}
        loop.worker_rejected_until = {}
        loop.environment_metadata = {
            "pentestvm": WorkEnvironmentPublic(
                id="pentestvm",
                label="pentestVM",
                backend="ssh",
                provider_endpoints=[
                    ProviderEndpointPublic(
                        id="pi-default",
                        type="pi",
                        base_url="https://pi.example.test/v1",
                        provider_api="openai-completions",
                        has_api_key=True,
                    )
                ],
            )
        }

        selection = loop._select_worker("proj_001", "bootstrap", "pentestvm")

        self.assertEqual(selection.worker, config.workers[0])

    def test_normal_startup_healthcheck_does_not_raise_when_all_workers_unavailable(self) -> None:
        config = DispatchConfig.model_validate(BASE_CONFIG)
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = config
        loop.environments = {"pentestvm": SimpleNamespace(id="pentestvm", backend="ssh")}
        loop.environment_metadata = {
            "pentestvm": WorkEnvironmentPublic(
                id="pentestvm",
                label="pentestVM",
                backend="ssh",
                provider_endpoints=[],
            )
        }
        loop.client = SimpleNamespace(
            get_environment_endpoint=lambda _environment_id, _endpoint_id, include_secret=False: (_ for _ in ()).throw(
                AssertionError("should not load")
            )
        )

        loop._run_startup_healthchecks(show_commands=False, fail_on_all=False)

    def test_startup_healthcheck_only_raises_when_all_workers_unavailable(self) -> None:
        config = DispatchConfig.model_validate(BASE_CONFIG)
        loop = DispatcherLoop.__new__(DispatcherLoop)
        loop.config = config
        loop.environments = {"pentestvm": SimpleNamespace(id="pentestvm", backend="ssh")}
        loop.environment_metadata = {
            "pentestvm": WorkEnvironmentPublic(
                id="pentestvm",
                label="pentestVM",
                backend="ssh",
                provider_endpoints=[],
            )
        }
        loop.client = SimpleNamespace(
            get_environment_endpoint=lambda _environment_id, _endpoint_id, include_secret=False: (_ for _ in ()).throw(
                AssertionError("should not load")
            )
        )

        with self.assertRaises(RuntimeError):
            loop._run_startup_healthchecks(show_commands=False, fail_on_all=True)


if __name__ == "__main__":
    unittest.main()
