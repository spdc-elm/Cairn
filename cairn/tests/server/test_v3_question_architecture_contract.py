from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import CreateProjectRequest, RemoteSessionProvenance, RunProvenanceUpsert
from cairn.server.questions.models import QuestionCreateRequest
from cairn.server.routers.projects import create_project
from cairn.server.routers.questions import create_question, reset_question_state_for_tests
from cairn.server.routers.runs import update_run_provenance_session, upsert_run_provenance
from cairn.server.services import dumps_json

ROOT = Path(__file__).resolve().parents[2]
SERVER_SRC = ROOT / "src" / "cairn" / "server"


class V3QuestionArchitectureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        reset_question_state_for_tests()
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id

    def tearDown(self) -> None:
        reset_question_state_for_tests()
        self.tmp.cleanup()

    def test_server_question_path_has_no_execution_plane_imports(self) -> None:
        forbidden = ("get_driver", "run_worker_process", "SshEnvironment", "dispatcher.runtime.environments")
        offenders: list[str] = []
        for path in SERVER_SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path.relative_to(ROOT)}:{token}")
        self.assertEqual(offenders, [])

    def test_post_question_with_message_returns_pending_job_without_execution(self) -> None:
        self._available_run("run_log_001", can_fork=True)
        thread = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="auto", message="why?"),
        )

        self.assertEqual(thread.mode, "fork")
        self.assertIsNotNone(thread.active_job)
        self.assertEqual(thread.active_job.status, "pending")
        self.assertEqual(len(thread.messages), 1)

    def test_server_uses_inventory_capability_not_driver_default(self) -> None:
        self._available_run("run_log_001", can_fork=False)
        with self.assertRaises(HTTPException) as exc:
            create_question(
                self.project_id,
                QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="fork"),
            )
        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail, "codex_cli_no_headless_fork")

    def _available_run(self, run_log_id: str, *, can_fork: bool) -> None:
        modes = ["resume", "fresh_context"]
        reasons = {"fork": "codex_cli_no_headless_fork"}
        if can_fork:
            modes.insert(0, "fork")
            reasons = {}
        capability = {
            "can_resume_session": True,
            "can_fork_session": can_fork,
            "can_use_tools": True,
            "can_stream_events": True,
            "resume_mutates_source": True,
            "fork_creates_remote_log": can_fork,
            "question_modes": modes,
            "unavailable_reasons": reasons,
        }
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, task_types_json, max_running, priority,
                    question_capability_json, capability_updated_at, capability_source, updated_at
                ) VALUES ('codex-main', 'codex', ?, 1, 0, ?, ?, 'config', ?)
                """,
                (dumps_json(["explore"]), dumps_json(capability), "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
            )
        upsert_run_provenance(
            self.project_id,
            RunProvenanceUpsert(
                run_log_id=run_log_id,
                task_type="explore",
                phase="explore_execute",
                worker_name="codex-main",
                worker_type="codex",
                started_at="2026-05-19T00:00:00Z",
            ),
        )
        update_run_provenance_session(
            self.project_id,
            run_log_id,
            RemoteSessionProvenance(id="thread-123", kind="codex_thread", status="available", capture_method="stdout_event"),
        )


if __name__ == "__main__":
    unittest.main()
