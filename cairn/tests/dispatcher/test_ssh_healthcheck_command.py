from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from cairn.dispatcher.config import SshEnvironmentConfig
from cairn.dispatcher.runtime.environments.ssh import RUNNER_PATH, RUNNER_SCRIPT, SshEnvironment


class SshHealthcheckCommandTests(unittest.TestCase):
    def test_constructor_does_not_connect_when_runner_path_omitted(self) -> None:
        config = SshEnvironmentConfig(
            id="ssh",
            label="SSH",
            backend="ssh",
            ssh_command="ssh host",
            workspace_root="/tmp/cairn-workspaces",
        )

        with patch.object(SshEnvironment, "_remote_run", side_effect=AssertionError("constructor must not ssh")):
            environment = SshEnvironment(config)

        self.assertEqual(environment.runner_path, RUNNER_PATH)

    def test_stopped_cleanup_is_needed_when_workspace_exists(self) -> None:
        environment = self._environment()

        with patch.object(environment, "_workspace_exists", return_value=True):
            self.assertTrue(environment.needs_stopped_cleanup("proj_001"))

        with patch.object(environment, "_workspace_exists", return_value=False):
            self.assertFalse(environment.needs_stopped_cleanup("proj_001"))

    def test_cleanup_stopped_cancels_workspace_runs(self) -> None:
        environment = self._environment()
        commands: list[list[str]] = []

        def fake_remote_run(argv: list[str], **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(environment, "_ensure_runner") as ensure_runner, patch.object(
            environment, "_remote_run", side_effect=fake_remote_run
        ):
            self.assertTrue(environment.cleanup_stopped("proj_001"))

        ensure_runner.assert_called_once()
        self.assertEqual(commands, [["/tmp/cairn-runner", "cancel-workspace", "/tmp/cairn-workspaces/proj_001"]])

    def test_runner_cancel_workspace_kills_recorded_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            workspace.mkdir()
            runner = root / "cairn-runner"
            runner.write_text(RUNNER_SCRIPT, encoding="utf-8")
            state_path = workspace / ".cairn" / "runs" / "run_001" / "state.json"
            request = {
                "cwd": str(workspace),
                "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                "env": {},
                "timeout_seconds": None,
                "kill_after_seconds": 1,
                "state_path": str(state_path),
            }
            proc = subprocess.Popen(
                [sys.executable, str(runner), "execute"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(request))
            proc.stdin.close()
            deadline = time.monotonic() + 5
            while not state_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(state_path.exists())

            subprocess.run(
                [sys.executable, str(runner), "cancel-workspace", str(workspace)],
                check=True,
                text=True,
                capture_output=True,
            )

            proc.communicate(timeout=5)
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(Path(str(state_path) + ".cancel").exists())

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
