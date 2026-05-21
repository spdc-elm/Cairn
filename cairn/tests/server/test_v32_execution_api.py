from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    AppendExecutionEventsRequest,
    CreateExecutionRequest,
    CreateIntentRequest,
    CreateProjectRequest,
    ExecutionEventAppend,
    ExecutionConclusionReportRequest,
    LeaseExecutionRequest,
    PatchExecutionRequest,
    UploadExecutionArtifactRequest,
)
from cairn.server.routers.executions import (
    create_project_execution,
    dispatcher_append_execution_events,
    dispatcher_lease_intent_execution,
    dispatcher_lease_pending_execution,
    dispatcher_patch_execution,
    dispatcher_submit_execution_conclusion_report,
    dispatcher_upload_execution_artifact,
    get_project_execution_events,
)
from cairn.server.routers.intents import create_intent
from cairn.server.routers.projects import create_project


class V32ExecutionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id
        self.intent = create_intent(
            self.project_id,
            CreateIntentRequest(**{"from": ["origin"], "description": "explore", "creator": "human"}),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_lease_patch_and_append_events(self) -> None:
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="explore", phase="run"),
        )
        self.assertEqual(execution.status, "pending")
        self.assertIsNone(execution.worker_name)

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
        self.assertEqual(leased.status, "leased")
        self.assertEqual(leased.worker_name, "pi-main")

        running = dispatcher_patch_execution(
            leased.id,
            PatchExecutionRequest(status="running", lease_seconds=30),
        )
        self.assertEqual(running.status, "running")
        self.assertIsNotNone(running.started_at)

        events = dispatcher_append_execution_events(
            leased.id,
            AppendExecutionEventsRequest(
                events=[
                    ExecutionEventAppend(event_type="stdout", payload={"text": "hi"}, event_key="chunk-1"),
                    ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "answer"}, event_key="msg-1"),
                ]
            ),
        )
        self.assertEqual([event.seq for event in events], [1, 2])
        self.assertEqual([event.project_seq for event in events], [1, 2])
        self.assertEqual(events[-1].cursor, "evt_2")

        page = get_project_execution_events(self.project_id, leased.id, after_cursor="evt_1")
        self.assertEqual(len(page.events), 1)
        self.assertEqual(page.events[0].payload["text"], "answer")

    def test_duplicate_event_key_is_idempotent_and_does_not_consume_cursor(self) -> None:
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="explore", phase="run"),
        )
        body = AppendExecutionEventsRequest(
            events=[ExecutionEventAppend(event_type="stdout", payload={"text": "once"}, event_key="same")]
        )
        first = dispatcher_append_execution_events(execution.id, body)[0]
        second = dispatcher_append_execution_events(execution.id, body)[0]
        third = dispatcher_append_execution_events(
            execution.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="stderr", payload={"text": "next"}, event_key="next")]
            ),
        )[0]

        self.assertEqual(first.id, second.id)
        self.assertEqual(third.project_seq, first.project_seq + 1)

    def test_intent_lease_execution_creates_and_leases_atomically(self) -> None:
        leased = dispatcher_lease_intent_execution(
            self.intent.id,
            LeaseExecutionRequest(
                project_id=self.project_id,
                dispatcher_id="disp",
                worker_name="codex-main",
                worker_type="codex",
                task_type="explore",
                phase="run",
            ),
        )

        self.assertEqual(leased.intent_id, self.intent.id)
        self.assertEqual(leased.status, "leased")
        with self.assertRaises(HTTPException) as conflict:
            dispatcher_lease_intent_execution(
                self.intent.id,
                LeaseExecutionRequest(
                    project_id=self.project_id,
                    dispatcher_id="disp",
                    worker_name="codex-main",
                    task_type="explore",
                    phase="run",
                ),
            )
        self.assertEqual(conflict.exception.status_code, 409)

    def test_conclusion_report_writes_primary_fact_provenance_in_one_transaction(self) -> None:
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="conclude", phase="run"),
        )
        artifact = dispatcher_upload_execution_artifact(
            execution.id,
            UploadExecutionArtifactRequest(type="report", path="/tmp/report.md", summary="report"),
        )

        result = dispatcher_submit_execution_conclusion_report(
            execution.id,
            ExecutionConclusionReportRequest(description="The useful result", title="Result", artifact_ids=[artifact.id]),
        )

        self.assertEqual(result.fact.title, "Result")
        self.assertEqual(result.intent.to, result.fact.id)
        with db.get_conn() as conn:
            fact = conn.execute(
                "SELECT * FROM facts WHERE project_id = ? AND id = ?",
                (self.project_id, result.fact.id),
            ).fetchone()
            intent = conn.execute(
                "SELECT * FROM intents WHERE project_id = ? AND id = ?",
                (self.project_id, self.intent.id),
            ).fetchone()
            links = conn.execute(
                "SELECT * FROM evidence_links WHERE project_id = ? AND fact_id = ?",
                (self.project_id, result.fact.id),
            ).fetchall()

        assert fact is not None
        assert intent is not None
        self.assertEqual(fact["produced_by_execution_id"], execution.id)
        self.assertEqual(fact["produced_by_intent_id"], self.intent.id)
        self.assertEqual(intent["concluded_fact_id"], result.fact.id)
        self.assertEqual(len(links), 2)
        self.assertEqual({link["relation"] for link in links}, {"derived_from", "supports"})

    def test_explore_conclusion_report_writes_execution_scoped_primary_fact(self) -> None:
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=self.intent.id, task_type="explore", phase="run"),
        )

        result = dispatcher_submit_execution_conclusion_report(
            execution.id,
            ExecutionConclusionReportRequest(description="Explore result", title="Explore Fact"),
        )

        self.assertEqual(result.intent.to, result.fact.id)
        with db.get_conn() as conn:
            fact = conn.execute(
                "SELECT produced_by_execution_id, produced_by_intent_id FROM facts WHERE project_id = ? AND id = ?",
                (self.project_id, result.fact.id),
            ).fetchone()
            links = conn.execute(
                "SELECT relation FROM evidence_links WHERE project_id = ? AND fact_id = ?",
                (self.project_id, result.fact.id),
            ).fetchall()
        assert fact is not None
        self.assertEqual(fact["produced_by_execution_id"], execution.id)
        self.assertEqual(fact["produced_by_intent_id"], self.intent.id)
        self.assertEqual([link["relation"] for link in links], ["derived_from"])


if __name__ == "__main__":
    unittest.main()
