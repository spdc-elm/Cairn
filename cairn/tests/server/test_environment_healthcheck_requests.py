from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cairn.server import db
from cairn.server.models import WorkerInventoryItem, WorkerInventoryUpsertRequest
from cairn.server.routers.environments import create_environment_healthcheck_request, list_environments
from cairn.server.routers.projects import list_projects
from cairn.server.routers.workers import upsert_workers


class EnvironmentHealthcheckRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_request_creates_pending_executions_for_only_environment_eligible_workers(self) -> None:
        upsert_workers(
            WorkerInventoryUpsertRequest(
                workers=[
                    WorkerInventoryItem(
                        name="alpha",
                        type="mock",
                        task_types=["explore"],
                        max_running=1,
                        priority=0,
                        allowed_environments=["docker-default"],
                    ),
                    WorkerInventoryItem(
                        name="beta",
                        type="mock",
                        task_types=["explore"],
                        max_running=1,
                        priority=1,
                        allowed_environments=["pentestvm"],
                    ),
                ]
            )
        )

        result = create_environment_healthcheck_request("docker-default")

        self.assertEqual(result["status"], "queued")
        self.assertEqual([execution["worker_name"] for execution in result["executions"]], ["alpha"])
        with db.get_conn() as conn:
            rows = conn.execute(
                """
                SELECT task_type, phase, status, environment_id, worker_name, metadata_json
                FROM execution_runs
                ORDER BY created_at
                """
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "healthcheck")
        self.assertEqual(rows[0]["phase"], "healthcheck")
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["environment_id"], "docker-default")
        self.assertEqual(rows[0]["worker_name"], "alpha")
        self.assertIn('"environment_id": "docker-default"', rows[0]["metadata_json"])

    def test_system_healthcheck_project_is_hidden_from_project_list(self) -> None:
        upsert_workers(
            WorkerInventoryUpsertRequest(
                workers=[
                    WorkerInventoryItem(name="alpha", type="mock", task_types=["explore"], max_running=1, priority=0)
                ]
            )
        )

        create_environment_healthcheck_request("docker-default")

        self.assertEqual([project.id for project in list_projects()], [])

    def test_environment_list_does_not_replay_previous_queued_healthcheck(self) -> None:
        upsert_workers(
            WorkerInventoryUpsertRequest(
                workers=[
                    WorkerInventoryItem(name="alpha", type="mock", task_types=["explore"], max_running=1, priority=0)
                ]
            )
        )
        create_environment_healthcheck_request("docker-default")

        environment = next(item for item in list_environments() if item.id == "docker-default")

        self.assertEqual(environment.last_health_status, "untested")
        self.assertIsNone(environment.last_healthcheck)


if __name__ == "__main__":
    unittest.main()
