from __future__ import annotations

import json
import unittest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.registry import get_driver


class WorkerDriverCapabilityTests(unittest.TestCase):
    def test_required_executables_are_declared_by_real_drivers(self) -> None:
        self.assertEqual(get_driver("pi").required_executables(), ("pi",))
        self.assertEqual(get_driver("codex").required_executables(), ("codex",))
        self.assertEqual(get_driver("claudecode").required_executables(), ("claude",))
        self.assertEqual(get_driver("mock").required_executables(), ())

    def test_codex_healthcheck_preflights_cli_and_redacts_described_key(self) -> None:
        worker = WorkerConfig(
            name="codex",
            type="codex",
            task_types=["reason"],
            max_running=1,
            priority=0,
            env={
                "CODEX_MODEL": "gpt-test",
                "CODEX_BASE_URL": "https://api.example.test/v1",
                "OPENAI_API_KEY": "sk-secret",
            },
        )
        driver = get_driver("codex")

        argv = driver.build_healthcheck(worker)
        description = driver.describe_startup_healthcheck(worker)

        self.assertEqual(argv[:2], ["/bin/sh", "-lc"])
        self.assertIn("command -v codex", argv[2])
        self.assertIn("missing executable: codex", argv[2])
        self.assertIn("curl", argv[2])
        self.assertIn("$OPENAI_API_KEY", description)
        self.assertNotIn("sk-secret", description)

    def test_claudecode_healthcheck_preflights_cli_and_redacts_described_key(self) -> None:
        worker = WorkerConfig(
            name="claude",
            type="claudecode",
            task_types=["reason"],
            max_running=1,
            priority=0,
            env={
                "ANTHROPIC_MODEL": "claude-test",
                "ANTHROPIC_BASE_URL": "https://anthropic.example.test",
                "ANTHROPIC_AUTH_TOKEN": "sk-secret",
            },
        )
        driver = get_driver("claudecode")

        argv = driver.build_healthcheck(worker)
        description = driver.describe_startup_healthcheck(worker)

        self.assertEqual(argv[0], "claude")
        self.assertIn("--bare", argv)
        self.assertIn("--tools", argv)
        self.assertIn("--model", argv)
        self.assertIn("claude-test", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)
        self.assertIn("claude-test", description)
        self.assertNotIn("sk-secret", description)

    def test_codex_driver_uses_jsonl_and_extracts_final_agent_message(self) -> None:
        worker = WorkerConfig(
            name="codex",
            type="codex",
            task_types=["reason"],
            max_running=1,
            priority=0,
            env={
                "CODEX_MODEL": "gpt-test",
                "CODEX_BASE_URL": "https://api.example.test/v1",
                "OPENAI_API_KEY": "sk-secret",
            },
        )
        driver = get_driver("codex")

        execute = driver.build_execute(worker, "prompt", None)
        conclude = driver.build_conclude(worker, "prompt", "thread-123")
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok": true}'}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
            ]
        )

        self.assertIn("--json", execute.argv)
        self.assertIn("--json", conclude)
        self.assertEqual(driver.extract_session(None, stdout, ""), "thread-123")
        self.assertEqual(driver.extract_response_text(stdout, ""), '{"ok": true}')

    def test_claudecode_driver_uses_stream_json_and_extracts_result(self) -> None:
        worker = WorkerConfig(
            name="claude",
            type="claudecode",
            task_types=["reason"],
            max_running=1,
            priority=0,
            env={
                "ANTHROPIC_MODEL": "claude-test",
                "ANTHROPIC_BASE_URL": "https://anthropic.example.test",
                "ANTHROPIC_AUTH_TOKEN": "sk-secret",
            },
        )
        driver = get_driver("claudecode")

        execute = driver.build_execute(worker, "prompt", "session-123")
        conclude = driver.build_conclude(worker, "prompt", "session-123")
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "assistant fallback"}]},
                    }
                ),
                json.dumps({"type": "result", "result": '{"ok": true}', "session_id": "session-123"}),
            ]
        )

        self.assertIn("--output-format", execute.argv)
        self.assertIn("stream-json", execute.argv)
        self.assertIn("--model", execute.argv)
        self.assertIn("claude-test", execute.argv)
        self.assertIn("--verbose", execute.argv)
        self.assertIn("--include-partial-messages", execute.argv)
        self.assertIn("--output-format", conclude)
        self.assertIn("stream-json", conclude)
        self.assertIn("--model", conclude)
        self.assertIn("claude-test", conclude)
        self.assertIn("--verbose", conclude)
        self.assertIn("--include-partial-messages", conclude)
        self.assertEqual(driver.extract_response_text(stdout, ""), '{"ok": true}')


if __name__ == "__main__":
    unittest.main()
