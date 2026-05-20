from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import CreateProjectRequest, RemoteSessionProvenance, RunProvenanceUpsert
from cairn.server.questions.models import (
    QuestionClaimRequest,
    QuestionCreateRequest,
    QuestionJobEventPayload,
    QuestionJobEventsRequest,
    QuestionJobHeartbeatRequest,
    QuestionJobTerminalRequest,
)
from cairn.server.routers.projects import create_project
from cairn.server.routers.questions import (
    close_question,
    create_question,
    dispatcher_append_question_events,
    dispatcher_claim_question_job,
    dispatcher_finish_question_job,
    dispatcher_start_question_job,
    get_question,
    post_question_message,
    reset_question_state_for_tests,
)
from cairn.server.routers.runs import update_run_provenance_session, upsert_run_provenance
from cairn.server.services import dumps_json
from cairn.server.questions.models import QuestionMessageRequest


class V3QuestionRuntimeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        reset_question_state_for_tests()
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id
        self._available_run("run_log_001")

    def tearDown(self) -> None:
        reset_question_state_for_tests()
        self.tmp.cleanup()

    def test_claim_start_events_finish_and_idempotent_event_append(self) -> None:
        thread = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="fresh_context", message="first?"),
        )
        claim = dispatcher_claim_question_job(QuestionClaimRequest(dispatcher_id="disp", worker_names=["codex-main"]))
        assert claim.job is not None
        self.assertEqual(claim.job.id, thread.active_job.id)

        running = dispatcher_start_question_job(claim.job.id, QuestionJobHeartbeatRequest(dispatcher_id="disp"))
        self.assertEqual(running.status, "running")

        payload = QuestionJobEventsRequest(
            dispatcher_id="disp",
            batch_id="batch_001",
            events=[QuestionJobEventPayload(event_key="batch_001:0", event={"kind": "message", "text": "hello"})],
        )
        self.assertEqual(len(dispatcher_append_question_events(claim.job.id, payload)), 1)
        self.assertEqual(len(dispatcher_append_question_events(claim.job.id, payload)), 1)

        done = dispatcher_finish_question_job(
            claim.job.id,
            QuestionJobTerminalRequest(dispatcher_id="disp", result_text="answer one"),
        )
        self.assertEqual(done.status, "succeeded")
        detail = get_question(self.project_id, thread.id)
        self.assertEqual(len(detail.events), 1)
        self.assertEqual(detail.messages[-1].text, "answer one")

    def test_fresh_context_second_turn_keeps_history_in_prompt_context(self) -> None:
        thread = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="fresh_context", message="first?"),
        )
        claim = dispatcher_claim_question_job(QuestionClaimRequest(dispatcher_id="disp", worker_names=["codex-main"]))
        assert claim.job is not None
        dispatcher_start_question_job(claim.job.id, QuestionJobHeartbeatRequest(dispatcher_id="disp"))
        dispatcher_finish_question_job(claim.job.id, QuestionJobTerminalRequest(dispatcher_id="disp", result_text="answer one"))

        detail = post_question_message(self.project_id, thread.id, QuestionMessageRequest(message="second?"))
        assert detail.active_job is not None
        prior = detail.active_job.prompt_context["prior_messages"]
        self.assertIn({"role": "user", "text": "first?", "seq": 1}, prior)
        self.assertIn({"role": "assistant", "text": "answer one", "seq": 1}, prior)

    def test_close_purges_runtime_and_get_returns_410(self) -> None:
        thread = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="fresh_context", message="first?"),
        )
        closed = close_question(self.project_id, thread.id)
        self.assertEqual(closed.status, "closed")
        with self.assertRaises(HTTPException) as gone:
            get_question(self.project_id, thread.id)
        self.assertEqual(gone.exception.status_code, 410)

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
