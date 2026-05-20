from __future__ import annotations

import unittest

from cairn.dispatcher.prompting import load_prompt
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.tasks.reports import (
    build_report_path,
    metadata_for_report,
    report_instruction,
    validate_report_written,
    write_failure_report,
)


class FakeEnvironment:
    id = "fake"
    backend = "ssh"

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def write_text_file(self, handle, path, content) -> None:
        if not self.is_path_in_workspace(handle, path):
            raise RuntimeError("outside workspace")
        self.files[path] = content

    def exists(self, handle, path) -> bool:
        return path in self.files

    def is_path_in_workspace(self, handle, path) -> bool:
        return path == handle.workspace or path.startswith(handle.workspace.rstrip("/") + "/")


class CommandBlackboardV2ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handle = EnvironmentHandle(project_id="proj_001", target_name="ssh", workspace="/tmp/cairn/proj_001")

    def test_report_path_and_instruction(self) -> None:
        path = build_report_path(self.handle, "i001", "run_abc")

        self.assertEqual(path, "/tmp/cairn/proj_001/.cairn/reports/execution-i001-run_abc.md")
        self.assertIn(path, report_instruction(path))

    def test_validate_report_written_requires_file_inside_workspace(self) -> None:
        env = FakeEnvironment()
        path = build_report_path(self.handle, "i001", "run_abc")

        self.assertFalse(validate_report_written(env, self.handle, path))
        env.write_text_file(self.handle, path, "report")
        self.assertTrue(validate_report_written(env, self.handle, path))
        self.assertFalse(validate_report_written(env, self.handle, "/tmp/cairn/proj_0012/report.md"))

    def test_failure_report_and_metadata(self) -> None:
        env = FakeEnvironment()
        path = build_report_path(self.handle, "i001", "run_abc")

        write_failure_report(env, self.handle, path, intent_id="i001", worker="alpha", run_id="run_abc", reason="timeout")

        self.assertIn("timeout", env.files[path])
        self.assertEqual(
            metadata_for_report(path, "run_abc", "alpha", "i001", producing_run_log_id="run_log_123"),
            {
                "report_path": path,
                "report_run_id": "run_abc",
                "worker": "alpha",
                "intent_id": "i001",
                "provenance": {
                    "producing_intent_id": "i001",
                    "producing_run_log_id": "run_log_123",
                    "report_run_id": "run_abc",
                    "report_path": path,
                    "worker_name": "alpha",
                },
            },
        )

    def test_reason_prompt_teaches_report_metadata(self) -> None:
        prompt = load_prompt("default", "reason.md")

        self.assertIn("metadata.report_path", prompt)
        self.assertIn("read the relevant report files", prompt)

    def test_conclude_prompt_allows_report_write(self) -> None:
        prompt = load_prompt("default", "explore_conclude.md")

        self.assertIn("create parent directories if needed", prompt)
        self.assertIn("writing the report file are the only allowed actions", prompt)
        self.assertNotIn("make any more tool calls", prompt)


if __name__ == "__main__":
    unittest.main()
