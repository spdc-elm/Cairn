from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import CreateExecutionRequest, CreateProjectRequest, PatchExecutionRequest, WorkerRuntimeHealth, WorkerRuntimeHealthUpsertRequest
from cairn.server.routers.branches import CreateBranchRequest, create_branch, list_branches
from cairn.server.routers.executions import create_project_execution, dispatcher_patch_execution
from cairn.server.routers.projects import create_project
from cairn.server.routers.workers import upsert_worker_health
from cairn.server.services import dumps_json


class V32BranchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fork_availability_comes_from_inventory_reason(self) -> None:
        source = self._source_execution(
            worker_name="codex-main",
            worker_type="codex",
            capability={
                "can_resume_session": True,
                "can_fork_session": False,
                "unavailable_reasons": {"fork": "codex_cli_no_headless_fork"},
            },
        )

        with self.assertRaises(HTTPException) as exc:
            create_branch(self.project_id, CreateBranchRequest(mode="fork", source_execution_id=source.id))

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.detail, "codex_cli_no_headless_fork")

    def test_unhealthy_source_identity_warns_without_disabling_fork_resume(self) -> None:
        source = self._source_execution(
            worker_name="pi-main",
            worker_type="pi",
            endpoint_id="pi-default",
            model_profile_id="pi-main",
            capability={
                "can_resume_session": True,
                "can_fork_session": True,
                "unavailable_reasons": {},
            },
        )
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
                        checked_at="2026-05-20T00:00:00Z",
                        disabled_until="2999-01-01T00:00:00Z",
                    )
                ]
            )
        )

        branch = create_branch(self.project_id, CreateBranchRequest(mode="resume", source_execution_id=source.id))

        self.assertIn("worker_environment_unhealthy", branch.warnings)

    def test_list_branches_filters_by_anchor_for_output_history(self) -> None:
        source = self._source_execution(
            worker_name="pi-main",
            worker_type="pi",
            capability={
                "can_resume_session": True,
                "can_fork_session": True,
                "unavailable_reasons": {},
            },
        )
        create_branch(
            self.project_id,
            CreateBranchRequest(
                anchor_kind="intent",
                anchor_id="i001",
                mode="fork",
                source_execution_id=source.id,
            ),
        )
        create_branch(
            self.project_id,
            CreateBranchRequest(
                anchor_kind="fact",
                anchor_id="f001",
                mode="fresh_context",
            ),
        )

        branches = list_branches(self.project_id, anchor_kind="intent", anchor_id="i001")

        self.assertEqual([branch.anchor_id for branch in branches], ["i001"])
        self.assertEqual([branch.mode for branch in branches], ["fork"])

    def _source_execution(
        self,
        *,
        worker_name: str,
        worker_type: str,
        capability: dict,
        endpoint_id: str = "",
        model_profile_id: str = "",
    ):
        execution = create_project_execution(self.project_id, CreateExecutionRequest(task_type="reason", phase="run"))
        dispatcher_patch_execution(
            execution.id,
            PatchExecutionRequest(
                status="succeeded",
                remote_session_out_kind=f"{worker_type}_session",
                remote_session_out_id="session-1",
                remote_session_out_status="available",
            ),
        )
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, task_types_json, max_running, priority,
                    question_capability_json, updated_at
                ) VALUES (?, ?, ?, 1, 0, ?, '2026-05-20T00:00:00Z')
                """,
                (worker_name, worker_type, dumps_json(["question"]), dumps_json(capability)),
            )
            conn.execute(
                """
                UPDATE execution_runs
                SET worker_name = ?,
                    worker_type = ?,
                    environment_id = 'docker-default',
                    endpoint_id = ?,
                    model_profile_id = ?
                WHERE id = ?
                """,
                (worker_name, worker_type, endpoint_id, model_profile_id, execution.id),
            )
        return execution


if __name__ == "__main__":
    unittest.main()
