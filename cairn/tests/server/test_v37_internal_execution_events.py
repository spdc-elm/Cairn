from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    AppendExecutionEventsRequest,
    CreateExecutionRequest,
    CreateProjectRequest,
    ExecutionEventAppend,
    FinishExecutionPatch,
    FinishExecutionRequest,
    LeaseExecutionRequest,
    PatchExecutionRequest,
)
from cairn.server.routers.executions import (
    create_project_execution,
    dispatcher_append_execution_events,
    dispatcher_finish_execution,
    dispatcher_lease_pending_execution,
    dispatcher_patch_execution,
    get_project_execution_events,
)
from cairn.server.routers.branches import (
    create_branch,
    post_branch_message,
    CreateBranchRequest,
    BranchMessageRequest,
)
from cairn.server.routers.projects import create_project


class V37InternalExecutionEventsTests(unittest.TestCase):
    """Branch initial user event and manual conclude terminal event must use
    server-internal helpers that bypass dispatcher append guard."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="v37-internal", origin="start", goal="finish"))
        self.project_id = project.project.id
        self._setup_worker_and_source()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _setup_worker_and_source(self) -> None:
        """Create a successful source execution with available session for fork/resume."""
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO worker_inventory (name, type, endpoint, model_profile, priority, task_types_json, max_running, question_capability_json, updated_at)
                VALUES ('mock', 'mock', NULL, NULL, 0, '["explore","question"]', 1, '{"can_fork_session":true,"can_resume_session":true}', '2026-05-26T00:00:00Z')
                """
            )
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=execution.id,
                dispatcher_id="disp",
                worker_name="mock",
                worker_type="mock",
            )
        )
        self.source_execution = dispatcher_finish_execution(
            leased.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                sink_token=leased.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="message",
                        role="assistant",
                        payload={"text": "done"},
                        event_key=f"{leased.id}:assistant:final",
                        ts="2026-05-26T00:00:00Z",
                    ),
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "succeeded"},
                        event_key=f"{leased.id}:status:terminal",
                        ts="2026-05-26T00:00:01Z",
                    ),
                ],
                patch=FinishExecutionPatch(
                    status="succeeded",
                    returncode=0,
                    remote_session_out_kind="mock_session",
                    remote_session_out_id="session-source",
                    remote_session_out_status="available",
                ),
            ),
        )

    # --- Branch initial user event is visible on pending execution ---

    def test_branch_message_creates_pending_execution_with_user_event(self) -> None:
        """When posting a branch message, execution is created in pending status with user event already written."""
        branch_response = create_branch(
            self.project_id,
            CreateBranchRequest(
                mode="fork",
                source_execution_id=self.source_execution.id,
            ),
        )
        branch_id = branch_response.id

        result = post_branch_message(
            self.project_id,
            branch_id,
            BranchMessageRequest(message="Hello from fork"),
        )
        execution = result["execution"]

        # Execution must be pending (not leased/running)
        self.assertEqual(execution.status, "pending")

        # User event must already be visible
        page = get_project_execution_events(self.project_id, execution.id)
        self.assertEqual(len(page.events), 1)
        self.assertEqual(page.events[0].event_type, "message")
        self.assertEqual(page.events[0].role, "user")
        self.assertEqual(page.events[0].payload["text"], "Hello from fork")

    def test_branch_initial_user_event_not_blocked_by_pending_status_guard(self) -> None:
        """The pending execution status guard for dispatcher append must NOT block
        the server-internal user event write during branch message creation.

        This tests that the v3.7 pending-status guard on dispatcher_append doesn't
        break the branch message flow which writes to a pending execution via internal helper.
        """
        branch_response = create_branch(
            self.project_id,
            CreateBranchRequest(
                mode="fork",
                source_execution_id=self.source_execution.id,
            ),
        )
        branch_id = branch_response.id

        # This should NOT raise even though execution is pending
        result = post_branch_message(
            self.project_id,
            branch_id,
            BranchMessageRequest(message="Internal write must work"),
        )
        self.assertIsNotNone(result["execution"])
        self.assertEqual(result["execution"].status, "pending")

    # --- Manual conclude terminal event bypasses dispatcher owner guard ---

    def test_manual_conclude_terminates_active_execution_via_internal_barrier(self) -> None:
        """Manual conclude must write terminal status event and patch execution to cancelled,
        even though the execution is owned by a different dispatcher.

        This uses the _terminalize_active_intent_executions_after_manual_conclude path.
        """
        from cairn.server.services import submit_manual_intent_conclusion

        # Create an intent and active execution
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO intents (id, project_id, description, creator, created_at)
                VALUES ('i001', ?, 'test intent', 'user', '2026-05-26T00:00:00Z')
                """,
                (self.project_id,),
            )
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', ?, 'origin')",
                (self.project_id,),
            )

        intent_execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="run", intent_id="i001"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=intent_execution.id,
                dispatcher_id="disp-owner",
                worker_name="mock",
                worker_type="mock",
            )
        )
        # Patch to running
        dispatcher_patch_execution(
            leased.id,
            PatchExecutionRequest(dispatcher_id="disp-owner", sink_token=leased.sink_token, status="running"),
        )

        # Manual conclude should terminate this running execution
        conclusion_json = '{"title":"Manual result","description":"Manually concluded finding"}'
        with db.get_conn() as conn:
            result = submit_manual_intent_conclusion(
                conn,
                self.project_id,
                "i001",
                actor="user",
                raw_json=conclusion_json,
            )

        # Verify execution was cancelled with terminal event
        with db.get_conn() as conn:
            from cairn.server.services import get_execution_or_404
            ex = get_execution_or_404(conn, self.project_id, leased.id)
            self.assertEqual(ex.status, "cancelled")
            self.assertEqual(ex.error_code, "manual_concluded")

        # Verify terminal status event was written
        page = get_project_execution_events(self.project_id, leased.id)
        status_events = [e for e in page.events if e.event_type == "status"]
        self.assertTrue(len(status_events) >= 1)
        terminal = status_events[-1]
        self.assertEqual(terminal.payload["status"], "cancelled")
        self.assertEqual(terminal.payload["reason"], "manual_concluded")

    def test_manual_conclude_not_blocked_by_dispatcher_owner_guard(self) -> None:
        """Manual conclude must work even when v3.7 dispatcher owner guard is active.
        The terminalization path must use an internal helper, not dispatcher_append.
        """
        from cairn.server.services import submit_manual_intent_conclusion

        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO intents (id, project_id, description, creator, created_at)
                VALUES ('i002', ?, 'guarded intent', 'user', '2026-05-26T00:00:00Z')
                """,
                (self.project_id,),
            )
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i002', ?, 'origin')",
                (self.project_id,),
            )

        intent_execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="run", intent_id="i002"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=intent_execution.id,
                dispatcher_id="different-dispatcher",
                worker_name="mock",
                worker_type="mock",
            )
        )
        dispatcher_patch_execution(
            leased.id,
            PatchExecutionRequest(dispatcher_id="different-dispatcher", sink_token=leased.sink_token, status="running"),
        )

        # This MUST succeed - manual conclude is server-internal, not bound by dispatcher guard
        conclusion_json = '{"title":"Override result","description":"Concluding despite different owner"}'
        with db.get_conn() as conn:
            result = submit_manual_intent_conclusion(
                conn,
                self.project_id,
                "i002",
                actor="admin",
                raw_json=conclusion_json,
            )
        self.assertIsNotNone(result.fact)
        self.assertEqual(result.fact.title, "Override result")


if __name__ == "__main__":
    unittest.main()
