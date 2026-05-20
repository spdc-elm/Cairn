from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    CreateExecutionRequest,
    CreateProjectRequest,
    PatchExecutionRequest,
    WorkerRuntimeHealth,
    WorkerRuntimeHealthUpsertRequest,
)
from cairn.server.routers.branches import CreateBranchRequest, create_branch
from cairn.server.routers.environments import healthcheck_environment
from cairn.server.routers.executions import create_project_execution, dispatcher_patch_execution
from cairn.server.routers.projects import create_project
from cairn.server.routers.workers import list_workers, upsert_worker_health
from cairn.server.services import dumps_json


class V32WorkerRuntimeHealthAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish", environment_id="docker-default"))
        self.project_id = self.project.project.id
        self._insert_pi_worker()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_worker_health_upsert_is_returned_by_inventory(self) -> None:
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="unhealthy",
                        checked_at="2026-05-19T00:00:00Z",
                        disabled_until="2026-05-19T00:00:05Z",
                        source="startup_healthcheck",
                        detail={"stderr_preview": "connection failed"},
                    )
                ]
            )
        )

        worker = next(item for item in list_workers() if item.name == "pi-main")

        self.assertEqual(worker.runtime_health[0].environment_id, "docker-default")
        self.assertEqual(worker.runtime_health[0].status, "unhealthy")
        self.assertEqual(worker.runtime_health[0].detail["stderr_preview"], "connection failed")

    def test_unhealthy_source_execution_disables_fork_resume(self) -> None:
        source = self._available_source_execution()
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="unhealthy",
                        checked_at="2026-05-19T00:00:00Z",
                        disabled_until="2999-01-01T00:00:00Z",
                        source="runtime_healthcheck",
                    )
                ]
            )
        )

        with self.assertRaises(HTTPException) as fork_exc:
            create_branch(self.project_id, CreateBranchRequest(mode="fork", source_execution_id=source.id))
        with self.assertRaises(HTTPException) as resume_exc:
            create_branch(self.project_id, CreateBranchRequest(mode="resume", source_execution_id=source.id))

        self.assertEqual(fork_exc.exception.detail, "worker_environment_unhealthy")
        self.assertEqual(resume_exc.exception.detail, "worker_environment_unhealthy")

    def test_removed_endpoint_marks_runtime_health_unknown(self) -> None:
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="ok",
                        checked_at="2026-05-19T00:00:00Z",
                        stale_after="2999-01-01T00:00:00Z",
                        source="startup_healthcheck",
                    )
                ]
            )
        )
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM environment_provider_endpoints WHERE environment_id = 'docker-default' AND endpoint_id = 'pi-default'"
            )

        worker = next(item for item in list_workers() if item.name == "pi-main")

        self.assertEqual(worker.runtime_health[0].status, "unknown")
        self.assertEqual(worker.runtime_health[0].detail["reason"], "worker_endpoint_unavailable")

    def test_environment_healthcheck_includes_runtime_worker_detail(self) -> None:
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="unhealthy",
                        checked_at="2026-05-19T00:00:00Z",
                        stale_after="2999-01-01T00:00:00Z",
                        disabled_until="2999-01-01T00:00:00Z",
                        source="startup_healthcheck",
                        detail={"returncode": 1, "duration_ms": 123, "stderr_preview": "connection failed"},
                    )
                ]
            )
        )

        result = healthcheck_environment("docker-default")
        worker_check = next(check for check in result["checks"] if check["name"] == "worker:pi-main")

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(worker_check["status"], "unhealthy")
        self.assertIn("returncode=1", worker_check["stdout"])
        self.assertEqual(worker_check["stderr"], "connection failed")

    def _insert_pi_worker(self) -> None:
        capability = {
            "can_resume_session": True,
            "can_fork_session": True,
            "question_modes": ["fork", "resume", "fresh_context"],
            "unavailable_reasons": {},
        }
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO environment_provider_endpoints (
                    environment_id, endpoint_id, type, base_url, provider_api, api_key, created_at, updated_at
                ) VALUES ('docker-default', 'pi-default', 'pi', 'http://example.invalid', 'openai', 'secret', '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, model_profile, endpoint, task_types_json, max_running, priority,
                    question_capability_json, capability_updated_at, capability_source, updated_at
                ) VALUES ('pi-main', 'pi', 'pi-main', 'pi-default', ?, 1, 0, ?, ?, 'static', ?)
                """,
                (dumps_json(["explore", "question"]), dumps_json(capability), "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
            )

    def _available_source_execution(self):
        execution = create_project_execution(self.project_id, CreateExecutionRequest(task_type="reason", phase="run"))
        dispatcher_patch_execution(
            execution.id,
            PatchExecutionRequest(
                status="succeeded",
                remote_session_out_kind="pi_session",
                remote_session_out_id="pi-session-1",
                remote_session_out_status="available",
            ),
        )
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE execution_runs
                SET worker_name = 'pi-main',
                    worker_type = 'pi',
                    environment_id = 'docker-default',
                    endpoint_id = 'pi-default',
                    model_profile_id = 'pi-main'
                WHERE id = ?
                """,
                (execution.id,),
            )
        return execution


if __name__ == "__main__":
    unittest.main()
