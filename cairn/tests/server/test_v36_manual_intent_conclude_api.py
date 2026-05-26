from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    CreateExecutionRequest,
    CreateIntentRequest,
    CreateProjectRequest,
    ExecutionConclusionReportRequest,
    LeaseExecutionRequest,
    ManualConcludeRequest,
    PatchExecutionRequest,
)
from cairn.server.routers.executions import (
    create_project_execution,
    dispatcher_lease_pending_execution,
    dispatcher_patch_execution,
    dispatcher_submit_execution_conclusion_report,
    get_project_execution_events,
)
from cairn.server.routers.intents import create_intent, manual_conclude, manual_conclude_prompt
from cairn.server.routers.projects import create_project, get_project_graph


class V36ManualIntentConcludeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id
        self.intent = create_intent(
            self.project_id,
            CreateIntentRequest(**{"from": ["origin"], "description": "explore the thing", "creator": "human"}),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prompt_returns_available_source_session_for_failed_latest_execution(self) -> None:
        execution = self._execution(status="failed", session_available=True)

        result = manual_conclude_prompt(self.project_id, self.intent.id)

        self.assertEqual(result.source_execution_id, execution.id)
        self.assertTrue(result.source_session_available)
        self.assertEqual(result.remote_session_id, "sess-1")
        self.assertIn("Return only one raw JSON object", result.prompt)
        self.assertIn(self.intent.id, result.prompt)

    def test_prompt_prefers_available_session_over_active_execution_without_session(self) -> None:
        active = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="explore", phase="run"),
        )
        dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=active.id,
                dispatcher_id="disp",
                worker_name="pi-active",
                worker_type="pi",
                environment_id="docker-default",
                endpoint_id="pi-default",
                model_profile_id="pi-main",
            )
        )
        failed = self._execution(status="failed", session_available=True)

        result = manual_conclude_prompt(self.project_id, self.intent.id)

        self.assertEqual(result.source_execution_id, failed.id)
        self.assertTrue(result.source_session_available)
        self.assertEqual(result.remote_session_id, "sess-1")

    def test_prompt_without_execution_is_still_available_without_session_hint(self) -> None:
        result = manual_conclude_prompt(self.project_id, self.intent.id)

        self.assertIsNone(result.source_execution_id)
        self.assertFalse(result.source_session_available)
        self.assertIn("No available remote session", result.prompt)

    def test_manual_import_concludes_active_intent_claimed_by_other_worker(self) -> None:
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="explore", phase="run"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=execution.id,
                dispatcher_id="disp",
                worker_name="pi-main",
                worker_type="pi",
                environment_id="docker-default",
                endpoint_id="pi-default",
                model_profile_id="pi-main",
            )
        )

        result = manual_conclude(
            self.project_id,
            self.intent.id,
            ManualConcludeRequest(
                actor="human",
                source_execution_id=leased.id,
                raw_json='{"accepted": true, "data": {"title": "Manual Fact", "description": "confirmed result"}}',
            ),
        )

        self.assertEqual(result.fact.title, "Manual Fact")
        self.assertEqual(result.intent.to, result.fact.id)
        with db.get_conn() as conn:
            fact = conn.execute(
                "SELECT * FROM facts WHERE project_id = ? AND id = ?",
                (self.project_id, result.fact.id),
            ).fetchone()
            execution_row = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (leased.id,)).fetchone()
            links = conn.execute(
                "SELECT * FROM evidence_links WHERE project_id = ? AND fact_id = ?",
                (self.project_id, result.fact.id),
            ).fetchall()
        assert fact is not None
        assert execution_row is not None
        self.assertEqual(fact["produced_by_intent_id"], self.intent.id)
        self.assertEqual(fact["produced_by_execution_id"], leased.id)
        self.assertEqual(execution_row["status"], "cancelled")
        self.assertEqual(execution_row["error_code"], "manual_concluded")
        self.assertEqual([link["relation"] for link in links], ["derived_from"])
        events = get_project_execution_events(self.project_id, leased.id).events
        self.assertEqual(events[-1].event_type, "status")
        self.assertEqual(events[-1].event_key, f"{leased.id}:status:manual-concluded")
        self.assertEqual(events[-1].payload["status"], "cancelled")

    def test_manual_import_accepts_bare_json_and_rejects_invalid_payloads(self) -> None:
        result = manual_conclude(
            self.project_id,
            self.intent.id,
            ManualConcludeRequest(actor="human", raw_json='{"title": "Bare", "description": "bare description"}'),
        )
        self.assertEqual(result.fact.title, "Bare")

        other = create_intent(
            self.project_id,
            CreateIntentRequest(**{"from": ["origin"], "description": "other", "creator": "human"}),
        )
        for raw_json in (
            '{"accepted": false, "reason": "not_enough"}',
            '{"accepted": true, "data": {"title": "Missing description"}}',
            '{"description": "   "}',
        ):
            with self.assertRaises(HTTPException) as exc:
                manual_conclude(self.project_id, other.id, ManualConcludeRequest(actor="human", raw_json=raw_json))
            self.assertEqual(exc.exception.status_code, 400)

    def test_manual_import_rejects_already_concluded_and_late_dispatcher_report(self) -> None:
        execution = self._execution(status="failed", session_available=True)
        result = manual_conclude(
            self.project_id,
            self.intent.id,
            ManualConcludeRequest(
                actor="human",
                source_execution_id=execution.id,
                raw_json='{"description": "first result"}',
            ),
        )

        with self.assertRaises(HTTPException) as duplicate:
            manual_conclude(
                self.project_id,
                self.intent.id,
                ManualConcludeRequest(actor="human", raw_json='{"description": "second result"}'),
            )
        self.assertEqual(duplicate.exception.status_code, 409)

        with self.assertRaises(HTTPException) as late:
            dispatcher_submit_execution_conclusion_report(
                execution.id,
                ExecutionConclusionReportRequest(description="late result", title="Late"),
            )
        self.assertEqual(late.exception.status_code, 409)
        graph = get_project_graph(self.project_id)
        facts = [fact for fact in graph["facts"] if fact["id"] == result.fact.id]
        self.assertEqual(len(facts), 1)

    def test_manual_import_validates_source_execution_scope(self) -> None:
        wrong_intent = create_intent(
            self.project_id,
            CreateIntentRequest(**{"from": ["origin"], "description": "wrong", "creator": "human"}),
        )
        wrong_execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=wrong_intent.id, task_type="explore", phase="run"),
        )

        with self.assertRaises(HTTPException) as wrong:
            manual_conclude(
                self.project_id,
                self.intent.id,
                ManualConcludeRequest(actor="human", source_execution_id=wrong_execution.id, raw_json='{"description": "x"}'),
            )
        self.assertEqual(wrong.exception.status_code, 400)

        with self.assertRaises(HTTPException) as missing:
            manual_conclude(
                self.project_id,
                self.intent.id,
                ManualConcludeRequest(actor="human", source_execution_id="missing", raw_json='{"description": "x"}'),
            )
        self.assertEqual(missing.exception.status_code, 404)

    def _execution(self, *, status: str, session_available: bool):
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="explore", phase="run"),
        )
        patch = PatchExecutionRequest(
            status=status,
            remote_session_out_kind="pi_session",
            remote_session_out_id="sess-1" if session_available else None,
            remote_session_out_status="available" if session_available else "unavailable",
        )
        return dispatcher_patch_execution(execution.id, patch)


if __name__ == "__main__":
    unittest.main()
