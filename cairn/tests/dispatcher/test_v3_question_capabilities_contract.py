from __future__ import annotations

import unittest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.registry import get_driver


def _worker(worker_type: str) -> WorkerConfig:
    env = {
        "codex": {
            "CODEX_MODEL": "gpt-test",
            "CODEX_BASE_URL": "https://api.example.test/v1",
            "OPENAI_API_KEY": "sk-secret",
        },
        "pi": {
            "PI_MODEL": "pi-test",
            "PI_BASE_URL": "https://pi.example.test/v1",
            "PI_API_KEY": "sk-secret",
            "PI_PROVIDER_API": "openai",
        },
        "claudecode": {
            "ANTHROPIC_MODEL": "claude-test",
            "ANTHROPIC_BASE_URL": "https://anthropic.example.test",
            "ANTHROPIC_AUTH_TOKEN": "sk-secret",
        },
        "mock": {},
    }[worker_type]
    return WorkerConfig(name=f"{worker_type}-main", type=worker_type, task_types=["explore"], max_running=1, priority=0, env=env)


class V3QuestionCapabilitiesContractTests(unittest.TestCase):
    def test_mock_driver_supports_protocol_fork(self) -> None:
        driver = get_driver("mock")
        worker = _worker("mock")

        capability = driver.question_capability(worker)
        result = driver.build_question(worker, mode="fork", prompt="why?", source_session="source-session")

        self.assertTrue(capability.can_fork_session)
        self.assertIn("fork", capability.question_modes)
        self.assertEqual(result.session, "mock-fork-source-session")

    def test_verified_real_driver_fork_capabilities_match_cli_surface(self) -> None:
        for worker_type in ("pi", "claudecode"):
            with self.subTest(worker_type=worker_type):
                capability = get_driver(worker_type).question_capability(_worker(worker_type))
                self.assertTrue(capability.can_fork_session)
                self.assertIn("fork", capability.question_modes)
                self.assertEqual(capability.unavailable_reasons, {})

    def test_codex_explains_fork_unavailability_until_headless_cli_exists(self) -> None:
        expected = {
            "codex": "codex_cli_no_headless_fork",
        }
        for worker_type, reason in expected.items():
            with self.subTest(worker_type=worker_type):
                capability = get_driver(worker_type).question_capability(_worker(worker_type))
                self.assertFalse(capability.can_fork_session)
                self.assertEqual((capability.unavailable_reasons or {}).get("fork"), reason)


if __name__ == "__main__":
    unittest.main()
