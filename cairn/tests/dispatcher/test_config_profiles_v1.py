from __future__ import annotations

import unittest
from pathlib import Path

from cairn.dispatcher.config import DispatchConfig, resolve_worker_env


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
}


class DispatchConfigProfileTests(unittest.TestCase):
    def test_dispatch_example_loads(self) -> None:
        config = DispatchConfig.load(Path(__file__).parents[3] / "dispatch.example.yaml")

        self.assertTrue(config.profiles)
        self.assertTrue(config.workers)

    def test_non_mock_worker_requires_declared_profile(self) -> None:
        with self.assertRaises(ValueError):
            DispatchConfig.model_validate(
                {
                    **BASE_CONFIG,
                    "workers": [
                        {
                            "name": "pi",
                            "type": "pi",
                            "task_types": ["bootstrap"],
                            "max_running": 1,
                            "priority": 0,
                        }
                    ],
                }
            )

    def test_profile_type_must_match_worker_type(self) -> None:
        with self.assertRaises(ValueError):
            DispatchConfig.model_validate(
                {
                    **BASE_CONFIG,
                    "profiles": [
                        {
                            "id": "codex-main",
                            "type": "codex",
                            "model": "gpt-test",
                            "base_url": "https://example.test/v1",
                            "api_key": "sk-test",
                        }
                    ],
                    "workers": [
                        {
                            "name": "pi",
                            "type": "pi",
                            "profile": "codex-main",
                            "task_types": ["bootstrap"],
                            "max_running": 1,
                            "priority": 0,
                        }
                    ],
                }
            )

    def test_pi_profile_resolves_worker_env(self) -> None:
        config = DispatchConfig.model_validate(
            {
                **BASE_CONFIG,
                "profiles": [
                    {
                        "id": "pi-main",
                        "type": "pi",
                        "model": "gpt-test",
                        "base_url": "https://example.test/v1",
                        "provider_api": "openai-completions",
                        "api_key": "sk-test",
                        "context_window": 12345,
                    }
                ],
                "workers": [
                    {
                        "name": "pi",
                        "type": "pi",
                        "profile": "pi-main",
                        "task_types": ["bootstrap"],
                        "max_running": 1,
                        "priority": 0,
                        "env": {"PI_MODEL": "worker-value"},
                    }
                ],
            }
        )

        worker = config.workers[0]
        profile = config.profile_config(worker)
        assert profile is not None
        env = resolve_worker_env(worker, profile)

        self.assertEqual(env["PI_MODEL"], "gpt-test")
        self.assertEqual(env["PI_BASE_URL"], "https://example.test/v1")
        self.assertEqual(env["PI_PROVIDER_API"], "openai-completions")
        self.assertEqual(env["PI_API_KEY"], "sk-test")
        self.assertEqual(env["PI_MODEL_CONTEXT_WINDOW"], "12345")

    def test_mock_worker_can_omit_profile(self) -> None:
        config = DispatchConfig.model_validate(
            {
                **BASE_CONFIG,
                "workers": [
                    {
                        "name": "mock",
                        "type": "mock",
                        "task_types": ["bootstrap"],
                        "max_running": 1,
                        "priority": 0,
                    }
                ],
            }
        )

        self.assertIsNone(config.workers[0].profile)


if __name__ == "__main__":
    unittest.main()
