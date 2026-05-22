from __future__ import annotations

import unittest
from pathlib import Path

from cairn.dispatcher.config import DispatchConfig, resolve_worker_env
from cairn.server.models import ProviderEndpointSecret


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


class DispatchConfigModelProfileTests(unittest.TestCase):
    def test_dispatch_example_loads(self) -> None:
        config = DispatchConfig.load(Path(__file__).parents[3] / "dispatch.example.yaml")

        self.assertTrue(config.model_profiles)
        self.assertTrue(config.workers)

    def test_v1_profiles_schema_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DispatchConfig.model_validate(
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
                        }
                    ],
                }
            )

    def test_non_mock_worker_requires_model_profile_and_endpoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_profile"):
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

        with self.assertRaisesRegex(ValueError, "endpoint"):
            DispatchConfig.model_validate(
                {
                    **BASE_CONFIG,
                    "model_profiles": [
                        {"id": "pi-main", "type": "pi", "model": "gpt-test"},
                    ],
                    "workers": [
                        {
                            "name": "pi",
                            "type": "pi",
                            "model_profile": "pi-main",
                            "task_types": ["bootstrap"],
                            "max_running": 1,
                            "priority": 0,
                        }
                    ],
                }
            )

    def test_model_profile_type_must_match_worker_type(self) -> None:
        with self.assertRaises(ValueError):
            DispatchConfig.model_validate(
                {
                    **BASE_CONFIG,
                    "model_profiles": [
                        {"id": "codex-main", "type": "codex", "model": "gpt-test"},
                    ],
                    "workers": [
                        {
                            "name": "pi",
                            "type": "pi",
                            "model_profile": "codex-main",
                            "endpoint": "pi-default",
                            "task_types": ["bootstrap"],
                            "max_running": 1,
                            "priority": 0,
                        }
                    ],
                }
            )

    def test_pi_model_profile_and_endpoint_resolve_worker_env(self) -> None:
        config = DispatchConfig.model_validate(
            {
                **BASE_CONFIG,
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
                        "task_types": ["bootstrap"],
                        "max_running": 1,
                        "priority": 0,
                        "env": {"PI_MODEL": "worker-value"},
                    }
                ],
            }
        )

        worker = config.workers[0]
        profile = config.model_profile_config(worker)
        assert profile is not None
        endpoint = ProviderEndpointSecret(
            id="pi-default",
            type="pi",
            base_url="https://example.test/v1",
            provider_api="openai-completions",
            has_api_key=True,
            api_key="sk-test",
        )
        env = resolve_worker_env(worker, profile, endpoint)

        self.assertEqual(env["PI_MODEL"], "gpt-test")
        self.assertEqual(env["PI_BASE_URL"], "https://example.test/v1")
        self.assertEqual(env["PI_PROVIDER_API"], "openai-completions")
        self.assertEqual(env["PI_API_KEY"], "sk-test")
        self.assertEqual(env["PI_MODEL_CONTEXT_WINDOW"], "12345")
        self.assertEqual(env["PI_REASONING"], "medium")

    def test_pi_model_profile_can_override_reasoning_level(self) -> None:
        config = DispatchConfig.model_validate(
            {
                **BASE_CONFIG,
                "model_profiles": [
                    {
                        "id": "pi-main",
                        "type": "pi",
                        "model": "gpt-test",
                        "reasoning": "off",
                    }
                ],
                "workers": [
                    {
                        "name": "pi",
                        "type": "pi",
                        "model_profile": "pi-main",
                        "endpoint": "pi-default",
                        "task_types": ["bootstrap"],
                        "max_running": 1,
                        "priority": 0,
                    }
                ],
            }
        )

        worker = config.workers[0]
        profile = config.model_profile_config(worker)
        assert profile is not None
        endpoint = ProviderEndpointSecret(
            id="pi-default",
            type="pi",
            base_url="https://example.test/v1",
            provider_api="openai-completions",
            has_api_key=True,
            api_key="sk-test",
        )
        env = resolve_worker_env(worker, profile, endpoint)

        self.assertEqual(env["PI_REASONING"], "off")

    def test_reasoning_is_only_valid_for_pi_profiles(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning is only supported for pi"):
            DispatchConfig.model_validate(
                {
                    **BASE_CONFIG,
                    "model_profiles": [
                        {
                            "id": "codex-main",
                            "type": "codex",
                            "model": "gpt-test",
                            "reasoning": "medium",
                        }
                    ],
                    "workers": [
                        {
                            "name": "codex",
                            "type": "codex",
                            "model_profile": "codex-main",
                            "endpoint": "codex-default",
                            "task_types": ["bootstrap"],
                            "max_running": 1,
                            "priority": 0,
                        }
                    ],
                }
            )

    def test_pi_reasoning_worker_env_rejects_invalid_level(self) -> None:
        with self.assertRaisesRegex(ValueError, "PI_REASONING"):
            DispatchConfig.model_validate(
                {
                    **BASE_CONFIG,
                    "model_profiles": [
                        {"id": "pi-main", "type": "pi", "model": "gpt-test"},
                    ],
                    "workers": [
                        {
                            "name": "pi",
                            "type": "pi",
                            "model_profile": "pi-main",
                            "endpoint": "pi-default",
                            "task_types": ["bootstrap"],
                            "max_running": 1,
                            "priority": 0,
                            "env": {"PI_REASONING": "very"},
                        }
                    ],
                }
            )

    def test_mock_worker_can_omit_model_profile_and_endpoint(self) -> None:
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

        self.assertIsNone(config.workers[0].model_profile)
        self.assertIsNone(config.workers[0].endpoint)


if __name__ == "__main__":
    unittest.main()
