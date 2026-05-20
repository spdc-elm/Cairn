from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import CreateProjectRequest, RemoteSessionProvenance, RunProvenanceUpsert
from cairn.server.routers.projects import create_project
from cairn.server.routers.questions import (
    close_question,
    create_question,
    dispatcher_claim_question_job,
    dispatcher_finish_question_job,
    dispatcher_start_question_job,
    get_question,
    promote_question,
    reset_question_state_for_tests,
)
from cairn.server.routers.runs import update_run_provenance_session, upsert_run_provenance
from cairn.server.questions.models import (
    QuestionClaimRequest,
    QuestionCreateRequest,
    QuestionJobHeartbeatRequest,
    QuestionJobTerminalRequest,
    QuestionPromotionRequest,
)
from cairn.server.services import dumps_json


class V3QuestionsApiTests(unittest.TestCase):
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

    def test_resume_requires_confirmation_and_locks_source_session(self) -> None:
        self._available_run("run_log_001")

        with self.assertRaises(HTTPException) as unconfirmed:
            create_question(
                self.project_id,
                QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="resume"),
            )
        self.assertEqual(unconfirmed.exception.status_code, 409)
        self.assertEqual(unconfirmed.exception.detail, "resume_confirmation_required")

        first = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="resume", confirm_resume=True),
        )
        self.assertEqual(first.mode, "resume")
        self.assertEqual(first.session_effect, "continued")

        with self.assertRaises(HTTPException) as locked:
            create_question(
                self.project_id,
                QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="resume", confirm_resume=True),
            )
        self.assertEqual(locked.exception.status_code, 409)

        closed = close_question(self.project_id, first.id)
        self.assertEqual(closed.status, "closed")
        with self.assertRaises(HTTPException) as gone:
            get_question(self.project_id, first.id)
        self.assertEqual(gone.exception.status_code, 410)

    def test_legacy_fact_falls_back_to_fresh_context_without_session_recovery(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO facts (id, project_id, title, description, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (
                    "f001",
                    self.project_id,
                    "legacy",
                    "old fact",
                    dumps_json({"run_id": "run_report_abc", "report_path": "/tmp/report.md"}),
                ),
            )

        thread = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="fact", anchor_id="f001", mode="auto"),
        )

        self.assertEqual(thread.mode, "fresh_context")
        self.assertEqual(thread.anchor_resolution.status, "missing")
        self.assertEqual(thread.anchor_resolution.reason, "legacy_report_run_id_only")
        self.assertEqual(thread.source_session.status, "missing")

    def test_running_intent_question_is_out_of_scope(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO intents (
                    id, project_id, description, creator, worker, last_heartbeat_at, created_at
                ) VALUES ('i001', ?, 'do work', 'human', 'codex-main', '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')
                """,
                (self.project_id,),
            )

        with self.assertRaises(HTTPException) as exc:
            create_question(self.project_id, QuestionCreateRequest(anchor_type="intent", anchor_id="i001"))
        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail, "running_intent_question_out_of_scope")

    def test_question_message_uses_executor_and_promotion_fact_keeps_source_metadata(self) -> None:
        self._available_run("run_log_001")

        thread = create_question(
            self.project_id,
            QuestionCreateRequest(
                anchor_type="run",
                anchor_id="run_log_001",
                mode="fresh_context",
                message="what happened?",
            ),
        )
        self.assertEqual(len(thread.messages), 1)
        self.assertIsNotNone(thread.active_job)
        self.assertEqual(thread.active_job.status, "pending")

        claim = dispatcher_claim_question_job(
            QuestionClaimRequest(dispatcher_id="disp_test", worker_names=["codex-main"])
        )
        assert claim.job is not None
        dispatcher_start_question_job(claim.job.id, QuestionJobHeartbeatRequest(dispatcher_id="disp_test"))
        dispatcher_finish_question_job(
            claim.job.id,
            QuestionJobTerminalRequest(dispatcher_id="disp_test", result_text="answer: what happened?"),
        )
        thread = get_question(self.project_id, thread.id)
        self.assertEqual(len(thread.messages), 2)
        self.assertEqual(thread.messages[-1].text, "answer: what happened?")

        promoted = promote_question(
            self.project_id,
            thread.id,
            QuestionPromotionRequest(kind="fact", title="Answer", content="verified answer", answer_summary="short"),
        )

        self.assertEqual(promoted.kind, "fact")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM facts WHERE project_id = ? AND id = ?",
                (self.project_id, promoted.object_id),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn('"source_run_log_id": "run_log_001"', row["metadata_json"])
        self.assertIn('"mode": "fresh_context"', row["metadata_json"])
        self.assertIn('"question_thread_id"', row["metadata_json"])

    def _available_run(self, run_log_id: str) -> None:
        capability = {
            "can_resume_session": True,
            "can_fork_session": False,
            "can_use_tools": True,
            "can_stream_events": True,
            "resume_mutates_source": True,
            "fork_creates_remote_log": False,
            "question_modes": ["resume", "fresh_context"],
            "unavailable_reasons": {"fork": "codex_cli_no_headless_fork"},
        }
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, model_profile, model, model_context_window, endpoint,
                    task_types_json, max_running, priority, allowed_environments_json,
                    question_capability_json, capability_updated_at, capability_source, updated_at
                ) VALUES (?, ?, NULL, NULL, NULL, NULL, ?, 1, 0, NULL, ?, ?, 'config', ?)
                """,
                ("codex-main", "codex", dumps_json(["explore"]), dumps_json(capability), "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
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
            RemoteSessionProvenance(
                id="thread-123",
                kind="codex_thread",
                status="available",
                capture_method="stdout_event",
            ),
        )


if __name__ == "__main__":
    unittest.main()
