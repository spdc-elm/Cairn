from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import AppendExecutionEventsRequest, CreateExecutionRequest, CreateProjectRequest, ExecutionEventAppend, LeaseExecutionRequest, PatchExecutionRequest
from cairn.server.routers.branches import BranchMessageRequest, CreateBranchRequest, create_branch, post_branch_message
from cairn.server.routers.branches import get_branch_timeline
from cairn.server.routers.executions import create_project_execution, dispatcher_append_execution_events, dispatcher_lease_pending_execution, dispatcher_patch_execution
from cairn.server.routers.projects import create_project
from cairn.server.services import dumps_json


class V32ExecutionSessionLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id
        self.source = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="reason", phase="run"),
        )
        dispatcher_patch_execution(
            self.source.id,
            PatchExecutionRequest(
                status="succeeded",
                remote_session_out_kind="pi_session",
                remote_session_out_id="sess-1",
                remote_session_out_status="available",
            ),
        )
        with db.get_conn() as conn:
            capability = {
                "can_resume_session": True,
                "can_fork_session": True,
                "unavailable_reasons": {},
            }
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, task_types_json, max_running, priority,
                    question_capability_json, updated_at
                ) VALUES ('pi-main', 'pi', ?, 1, 0, ?, '2026-05-20T00:00:00Z')
                """,
                (dumps_json(["question"]), dumps_json(capability)),
            )
            conn.execute(
                """
                UPDATE execution_runs
                SET worker_name = 'pi-main',
                    worker_type = 'pi',
                    environment_id = 'docker-default',
                    endpoint_id = '',
                    model_profile_id = ''
                WHERE id = ?
                """,
                (self.source.id,),
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resume_lock_rejects_second_branch_until_terminal_release(self) -> None:
        first = create_branch(
            self.project_id,
            CreateBranchRequest(mode="resume", source_execution_id=self.source.id),
        )
        second = create_branch(
            self.project_id,
            CreateBranchRequest(mode="resume", source_execution_id=self.source.id),
        )

        first_message = post_branch_message(self.project_id, first.id, BranchMessageRequest(message="first?"))
        self.assertEqual(first_message["execution"].session_action, "resume_continue")

        with self.assertRaises(HTTPException) as conflict:
            post_branch_message(self.project_id, second.id, BranchMessageRequest(message="second?"))
        self.assertEqual(conflict.exception.status_code, 409)

        dispatcher_patch_execution(first_message["execution"].id, PatchExecutionRequest(status="succeeded"))
        second_message = post_branch_message(self.project_id, second.id, BranchMessageRequest(message="second?"))
        self.assertEqual(second_message["execution"].session_action, "resume_continue")

    def test_fork_first_turn_then_branch_continue_without_source_lock(self) -> None:
        branch = create_branch(
            self.project_id,
            CreateBranchRequest(mode="fork", source_execution_id=self.source.id),
        )
        first = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="first?"))
        self.assertEqual(first["execution"].session_action, "fork_initial")
        dispatcher_patch_execution(
            first["execution"].id,
            PatchExecutionRequest(
                status="succeeded",
                remote_session_out_kind="pi_session",
                remote_session_out_id="fork-sess",
                remote_session_out_status="available",
            ),
        )
        second = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="second?"))
        self.assertEqual(second["execution"].session_action, "branch_continue")
        self.assertEqual(second["execution"].remote_session_in_id, "fork-sess")

    def test_resume_and_fork_outputs_are_projected_in_branch_timeline(self) -> None:
        branch = create_branch(
            self.project_id,
            CreateBranchRequest(mode="fork", source_execution_id=self.source.id),
        )
        first = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="remember alpha"))
        first_leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=first["execution"].id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )
        dispatcher_append_execution_events(
            first["execution"].id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp",
                sink_token=first_leased.sink_token,
                events=[
                    ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "alpha stored"}),
                    ExecutionEventAppend(event_type="stdout", payload={"text": "first stdout\n"}),
                ]
            ),
        )
        dispatcher_patch_execution(
            first["execution"].id,
            PatchExecutionRequest(
                dispatcher_id="disp",
                sink_token=first_leased.sink_token,
                status="succeeded",
                remote_session_out_kind="pi_session",
                remote_session_out_id="fork-sess",
                remote_session_out_status="available",
            ),
        )
        second = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="what did I say?"))
        second_leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=second["execution"].id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )
        dispatcher_append_execution_events(
            second["execution"].id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp",
                sink_token=second_leased.sink_token,
                events=[
                    ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "you said alpha"}),
                    ExecutionEventAppend(event_type="stdout", payload={"text": "second stdout\n"}),
                ]
            ),
        )

        timeline = get_branch_timeline(self.project_id, branch.id)
        texts = [event.payload.get("text") for event in timeline.events]
        execution_ids = [event.execution_id for event in timeline.events]
        self.assertIn("remember alpha", texts)
        self.assertIn("alpha stored", texts)
        self.assertIn("what did I say?", texts)
        self.assertIn("you said alpha", texts)
        self.assertIn(first["execution"].id, execution_ids)
        self.assertIn(second["execution"].id, execution_ids)


if __name__ == "__main__":
    unittest.main()
