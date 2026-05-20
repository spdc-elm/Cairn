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
    return WorkerConfig(
        name=f"{worker_type}-main",
        type=worker_type,
        task_types=["explore"],
        max_running=1,
        priority=0,
        env=env,
    )


class V3QuestionCapabilityTests(unittest.TestCase):
    def test_real_drivers_declare_question_modes_from_verified_cli_surface(self) -> None:
        expected = {
            "codex": (False, ("resume", "fresh_context")),
            "pi": (True, ("fork", "resume", "fresh_context")),
            "claudecode": (True, ("fork", "resume", "fresh_context")),
        }
        for worker_type, (can_fork, modes) in expected.items():
            with self.subTest(worker_type=worker_type):
                driver = get_driver(worker_type)
                capability = driver.question_capability(_worker(worker_type))

                self.assertTrue(capability.can_resume_session)
                self.assertEqual(capability.can_fork_session, can_fork)
                self.assertTrue(capability.resume_mutates_source)
                self.assertEqual(capability.question_modes, modes)

    def test_codex_question_resume_uses_resume_exec_and_fresh_context_starts_new_exec(self) -> None:
        driver = get_driver("codex")
        worker = _worker("codex")

        resume = driver.build_question(worker, mode="resume", prompt="why?", source_session="thread-1")
        fresh = driver.build_question(worker, mode="fresh_context", prompt="why?")

        self.assertIn("resume", resume.argv)
        self.assertIn("thread-1", resume.argv)
        self.assertNotIn("resume", fresh.argv)
        self.assertIsNone(fresh.session)

    def test_pi_question_resume_uses_session_flag_and_fresh_context_omits_it(self) -> None:
        driver = get_driver("pi")
        worker = _worker("pi")

        resume = driver.build_question(worker, mode="resume", prompt="why?", source_session="pi-session-1")
        fresh = driver.build_question(worker, mode="fresh_context", prompt="why?")

        self.assertIn("--session", resume.argv)
        self.assertIn("pi-session-1", resume.argv)
        self.assertNotIn("--session", fresh.argv)
        self.assertIsNone(fresh.session)

    def test_pi_question_fork_uses_fork_flag_and_discovers_new_session_from_stream(self) -> None:
        driver = get_driver("pi")
        worker = _worker("pi")

        fork = driver.build_question(worker, mode="fork", prompt="why?", source_session="pi-session-1")

        self.assertIn("--fork", fork.argv)
        self.assertIn("pi-session-1", fork.argv)
        self.assertIsNone(fork.session)
        self.assertEqual(driver.extract_session_provenance(fork.session, '{"type":"session","id":"pi-session-2"}\n', "").id, "pi-session-2")

    def test_claudecode_question_resume_uses_resume_and_fresh_context_seeds_session(self) -> None:
        driver = get_driver("claudecode")
        worker = _worker("claudecode")

        resume = driver.build_question(worker, mode="resume", prompt="why?", source_session="claude-session-1")
        fresh = driver.build_question(worker, mode="fresh_context", prompt="why?")

        self.assertIn("-r", resume.argv)
        self.assertIn("claude-session-1", resume.argv)
        self.assertIn("--session-id", fresh.argv)
        self.assertIsNotNone(fresh.session)

    def test_claudecode_question_fork_uses_fork_session_flag_and_discovers_new_session(self) -> None:
        driver = get_driver("claudecode")
        worker = _worker("claudecode")

        fork = driver.build_question(worker, mode="fork", prompt="why?", source_session="claude-session-1")

        self.assertIn("--resume", fork.argv)
        self.assertIn("claude-session-1", fork.argv)
        self.assertIn("--fork-session", fork.argv)
        self.assertIsNone(fork.session)
        self.assertEqual(
            driver.extract_session_provenance(fork.session, '{"type":"system","session_id":"claude-session-2"}\n', "").id,
            "claude-session-2",
        )

    def test_unsupported_question_mode_and_missing_resume_session_fail_clearly(self) -> None:
        driver = get_driver("codex")
        worker = _worker("codex")

        with self.assertRaisesRegex(ValueError, "not supported"):
            driver.build_question(worker, mode="fork", prompt="why?")
        with self.assertRaisesRegex(ValueError, "requires source_session"):
            driver.build_question(worker, mode="resume", prompt="why?")


if __name__ == "__main__":
    unittest.main()
