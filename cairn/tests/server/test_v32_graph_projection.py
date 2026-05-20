from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cairn.server import db
from cairn.server.models import ConcludeRequest, CreateExecutionRequest, CreateIntentRequest, CreateProjectRequest, LeaseExecutionRequest
from cairn.server.routers.executions import create_project_execution, dispatcher_lease_pending_execution
from cairn.server.routers.intents import conclude, create_intent
from cairn.server.routers.projects import create_project, get_project_graph


class V32GraphProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_intent_runtime_state_is_derived_from_execution_runs(self) -> None:
        intent = create_intent(
            self.project_id,
            CreateIntentRequest(**{"from": ["origin"], "description": "explore", "creator": "human"}),
        )
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=intent.id, task_type="explore", phase="run"),
        )
        dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=execution.id,
                dispatcher_id="disp",
                worker_name="pi-main",
                worker_type="pi",
            )
        )

        graph = get_project_graph(self.project_id)
        projected = next(item for item in graph["intents"] if item["id"] == intent.id)

        self.assertEqual(projected["active_execution_id"], execution.id)
        self.assertEqual(projected["latest_execution_id"], execution.id)
        self.assertEqual(projected["runtime_status"], "leased")
        self.assertEqual(projected["worker_name"], "pi-main")

    def test_fact_projection_falls_back_to_intent_execution(self) -> None:
        intent = create_intent(
            self.project_id,
            CreateIntentRequest(**{"from": ["origin"], "description": "explore", "creator": "human"}),
        )
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(intent_id=intent.id, task_type="explore", phase="run"),
        )
        dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=execution.id,
                dispatcher_id="disp",
                worker_name="pi-main",
                worker_type="pi",
            )
        )
        result = conclude(
            self.project_id,
            intent.id,
            ConcludeRequest(worker="pi-main", title="Found", description="found"),
        )

        with db.get_conn() as conn:
            produced = conn.execute(
                "SELECT produced_by_execution_id FROM facts WHERE project_id = ? AND id = ?",
                (self.project_id, result.fact.id),
            ).fetchone()
        assert produced is not None
        self.assertEqual(produced["produced_by_execution_id"], execution.id)

        graph = get_project_graph(self.project_id)
        fact = next(item for item in graph["facts"] if item["id"] == result.fact.id)
        self.assertEqual(fact["producing_execution_id"], execution.id)


if __name__ == "__main__":
    unittest.main()
