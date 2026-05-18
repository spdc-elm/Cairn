from __future__ import annotations

import unittest
from unittest.mock import patch

from cairn.dispatcher.config import SshEnvironmentConfig
from cairn.dispatcher.runtime.environments.ssh import SshEnvironment


class SshHealthcheckCommandTests(unittest.TestCase):
    def test_harness_check_fails_fast(self) -> None:
        environment = SshEnvironment(
            SshEnvironmentConfig(
                id="ssh",
                label="SSH",
                backend="ssh",
                ssh_command="ssh host",
                workspace_root="/tmp/cairn-workspaces",
                runner_path="/tmp/cairn-runner",
            )
        )
        commands: list[list[str]] = []

        def fake_check(name: str, argv: list[str], *, timeout: int):
            commands.append(argv)
            return {"name": name, "status": "ok", "duration_ms": 0, "command": "", "stdout": "", "stderr": ""}

        with patch.object(environment, "_check", side_effect=fake_check), patch.object(environment, "_ensure_runner"):
            environment.run_healthcheck()

        harness = commands[2]
        self.assertEqual(harness[:2], ["sh", "-lc"])
        self.assertIn("set -e;", harness[2])
        self.assertIn("command -v pi", harness[2])
        self.assertIn("missing executable: pi", harness[2])


if __name__ == "__main__":
    unittest.main()
