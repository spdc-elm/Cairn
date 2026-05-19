from __future__ import annotations

import unittest

from cairn.dispatcher.config import DispatchConfig, DockerEnvironmentConfig, SshEnvironmentConfig


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
    "workers": [
        {
            "name": "mock",
            "type": "mock",
            "task_types": ["reason", "explore", "bootstrap"],
            "max_running": 1,
            "priority": 0,
            "env": {},
        }
    ],
}


class DispatchConfigEnvironmentTests(unittest.TestCase):
    def test_old_container_config_generates_docker_default(self) -> None:
        config = DispatchConfig.model_validate(
            {
                **BASE_CONFIG,
                "container": {
                    "image": "ghcr.io/oritera/cairn-worker-container:latest",
                    "network_mode": "host",
                    "completed_action": "stop",
                },
            }
        )

        self.assertEqual(config.default_environment_id, "docker-default")
        self.assertIsInstance(config.environments[0], DockerEnvironmentConfig)

    def test_ssh_config_loads_and_parses_command(self) -> None:
        data = {
            **BASE_CONFIG,
            "environments": [
                {
                    "id": "pentestvm-ssh",
                    "label": "pentestVM over SSH",
                    "backend": "ssh",
                    "ssh_command": "ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm",
                    "workspace_root": "/home/kali/cairn-workspaces",
                    "credentials": {"mode": "inject"},
                }
            ],
            "workers": [
                {
                    **BASE_CONFIG["workers"][0],
                    "allowed_environments": ["pentestvm-ssh"],
                }
            ],
        }
        config = DispatchConfig.model_validate(data)

        environment = config.environments[0]
        self.assertIsInstance(environment, SshEnvironmentConfig)
        assert isinstance(environment, SshEnvironmentConfig)
        self.assertEqual(environment.ssh_argv(), ["ssh", "-F", "/tmp/cairn_pentestvm_ssh_config", "cairn-pentestvm"])

    def test_ssh_config_ignores_legacy_harness_field(self) -> None:
        config = DispatchConfig.model_validate(
            {
                **BASE_CONFIG,
                "environments": [
                    {
                        "id": "ssh",
                        "label": "SSH",
                        "backend": "ssh",
                        "ssh_command": "ssh host",
                        "workspace_root": "/tmp/cairn",
                        "harness": "pi",
                    }
                ],
            }
        )

        environment = config.environments[0]
        self.assertIsInstance(environment, SshEnvironmentConfig)
        self.assertNotIn("harness", environment.model_dump())

    def test_rejects_duplicate_environment_id(self) -> None:
        data = {
            **BASE_CONFIG,
            "environments": [
                {"id": "x", "label": "one", "backend": "ssh", "ssh_command": "ssh host", "workspace_root": "/tmp/cairn"},
                {"id": "x", "label": "two", "backend": "ssh", "ssh_command": "ssh host", "workspace_root": "/tmp/cairn2"},
            ],
        }

        with self.assertRaises(ValueError):
            DispatchConfig.model_validate(data)

    def test_rejects_ctf_workspace(self) -> None:
        data = {
            **BASE_CONFIG,
            "environments": [
                {"id": "bad", "label": "bad", "backend": "ssh", "ssh_command": "ssh host", "workspace_root": "/home/kali/ctf"},
            ],
        }

        with self.assertRaises(ValueError):
            DispatchConfig.model_validate(data)

    def test_worker_allowed_environment_can_reference_server_side_environment(self) -> None:
        data = {
            **BASE_CONFIG,
            "environments": [
                {"id": "ok", "label": "ok", "backend": "ssh", "ssh_command": "ssh host", "workspace_root": "/tmp/cairn"},
            ],
            "workers": [
                {
                    **BASE_CONFIG["workers"][0],
                    "allowed_environments": ["missing"],
                }
            ],
        }

        config = DispatchConfig.model_validate(data)
        self.assertEqual(config.workers[0].allowed_environments, ["missing"])


if __name__ == "__main__":
    unittest.main()
