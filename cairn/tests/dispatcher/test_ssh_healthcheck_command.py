from __future__ import annotations

import unittest
from unittest.mock import patch

from cairn.dispatcher.config import SshEnvironmentConfig
from cairn.dispatcher.runtime.environments.ssh import SshEnvironment


class SshHealthcheckCommandTests(unittest.TestCase):
    def test_no_worker_type_skips_worker_cli_check(self) -> None:
        environment = self._environment()
        commands: list[list[str]] = []

        def fake_check(name: str, argv: list[str], *, timeout: int):
            commands.append(argv)
            return {"name": name, "status": "ok", "duration_ms": 0, "command": "", "stdout": "", "stderr": ""}

        with patch.object(environment, "_check", side_effect=fake_check), patch.object(environment, "_ensure_runner"):
            result = environment.run_healthcheck()

        self.assertEqual(commands[2], ["/tmp/cairn-runner", "--version"])
        self.assertEqual(commands[3][:2], ["sh", "-lc"])
        worker_cli = next(check for check in result["checks"] if check["name"] == "worker-cli")
        self.assertEqual(worker_cli["status"], "skipped")
        self.assertIn("No provider endpoints", worker_cli["stderr"])

    def test_worker_cli_check_uses_driver_required_executables(self) -> None:
        environment = self._environment()
        commands: list[list[str]] = []

        def fake_check(name: str, argv: list[str], *, timeout: int):
            commands.append(argv)
            return {"name": name, "status": "ok", "duration_ms": 0, "command": "", "stdout": "", "stderr": ""}

        with patch.object(environment, "_check", side_effect=fake_check), patch.object(environment, "_ensure_runner"):
            environment.run_healthcheck(["pi", "codex", "claudecode"])

        worker_cli = commands[3]
        self.assertEqual(worker_cli[:2], ["sh", "-lc"])
        self.assertIn("set -e", worker_cli[2])
        self.assertIn("command -v pi", worker_cli[2])
        self.assertIn("command -v codex", worker_cli[2])
        self.assertIn("command -v claude", worker_cli[2])
        self.assertIn("missing executable: pi", worker_cli[2])
        self.assertIn("missing executable: codex", worker_cli[2])
        self.assertIn("missing executable: claude", worker_cli[2])

    @staticmethod
    def _environment() -> SshEnvironment:
        return SshEnvironment(
            SshEnvironmentConfig(
                id="ssh",
                label="SSH",
                backend="ssh",
                ssh_command="ssh host",
                workspace_root="/tmp/cairn-workspaces",
                runner_path="/tmp/cairn-runner",
            )
        )


if __name__ == "__main__":
    unittest.main()
