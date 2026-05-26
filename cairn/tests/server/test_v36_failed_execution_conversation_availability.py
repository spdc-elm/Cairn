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
from cairn.server.routers.branches import BranchMessageRequest, CreateBranchRequest, create_branch, post_branch_message
from cairn.server.routers.executions import create_project_execution, dispatcher_patch_execution
from cairn.server.routers.projects import create_project
from cairn.server.routers.workers import upsert_worker_health
from cairn.server.services import dumps_json


class V36FailedExecutionConversationAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish", environment_id="docker-default"))
        self.project_id = self.project.project.id
        self._insert_pi_worker()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_failed_execution_with_available_session_can_fork_and_resume(self) -> None:
        source = self._source_execution(status="failed", session_available=True)

        fork = create_branch(self.project_id, CreateBranchRequest(mode="fork", source_execution_id=source.id))
        resume = create_branch(self.project_id, CreateBranchRequest(mode="resume", source_execution_id=source.id))

        fork_message = post_branch_message(self.project_id, fork.id, BranchMessageRequest(message="fork?"))
        resume_message = post_branch_message(self.project_id, resume.id, BranchMessageRequest(message="resume?"))
        self.assertEqual(fork_message["execution"].session_action, "fork_initial")
        self.assertEqual(fork_message["execution"].remote_session_in_id, "pi-session-1")
        self.assertEqual(resume_message["execution"].session_action, "resume_continue")
        self.assertEqual(resume_message["execution"].remote_session_in_id, "pi-session-1")

    def test_unhealthy_runtime_health_warns_but_does_not_block_fork_resume(self) -> None:
        source = self._source_execution(status="failed", session_available=True)
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
                        checked_at="2026-05-23T00:00:00Z",
                        disabled_until="2999-01-01T00:00:00Z",
                        source="runtime_healthcheck",
                    )
                ]
            )
        )

        fork = create_branch(self.project_id, CreateBranchRequest(mode="fork", source_execution_id=source.id))
        resume = create_branch(self.project_id, CreateBranchRequest(mode="resume", source_execution_id=source.id))

        self.assertIn("worker_environment_unhealthy", fork.warnings)
        self.assertIn("worker_environment_unhealthy", resume.warnings)

    def test_no_available_session_still_blocks_fork_resume(self) -> None:
        source = self._source_execution(status="failed", session_available=False)

        with self.assertRaises(HTTPException) as fork_exc:
            create_branch(self.project_id, CreateBranchRequest(mode="fork", source_execution_id=source.id))
        with self.assertRaises(HTTPException) as resume_exc:
            create_branch(self.project_id, CreateBranchRequest(mode="resume", source_execution_id=source.id))

        self.assertEqual(fork_exc.exception.status_code, 409)
        self.assertEqual(resume_exc.exception.detail, "Source execution has no available remote session")

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
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, model_profile, endpoint, task_types_json, max_running, priority,
                    question_capability_json, capability_updated_at, capability_source, updated_at
                ) VALUES ('pi-main', 'pi', 'pi-main', 'pi-default', ?, 1, 0, ?, ?, 'static', ?)
                """,
                (dumps_json(["explore", "question"]), dumps_json(capability), "2026-05-23T00:00:00Z", "2026-05-23T00:00:00Z"),
            )

    def _source_execution(self, *, status: str, session_available: bool):
        execution = create_project_execution(self.project_id, CreateExecutionRequest(task_type="reason", phase="run"))
        updated = dispatcher_patch_execution(
            execution.id,
            PatchExecutionRequest(
                status=status,
                remote_session_out_kind="pi_session",
                remote_session_out_id="pi-session-1" if session_available else None,
                remote_session_out_status="available" if session_available else "unavailable",
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
                (updated.id,),
            )
        return updated


if __name__ == "__main__":
    unittest.main()
