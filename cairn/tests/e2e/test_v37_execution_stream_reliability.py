from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

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
    get_branch_timeline,
    post_branch_message,
    CreateBranchRequest,
    BranchMessageRequest,
)
from cairn.server.routers.projects import create_project
from fastapi import HTTPException


class V37ExecutionStreamReliabilityE2E(unittest.TestCase):
    """End-to-end test covering the v3.7 execution event stream reliability improvements."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="v37-e2e", origin="start", goal="finish"))
        self.project_id = project.project.id
        self._setup_worker()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _setup_worker(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO worker_inventory (name, type, endpoint, model_profile, priority, task_types_json, max_running, question_capability_json, updated_at)
                VALUES ('mock', 'mock', NULL, NULL, 0, '["explore","question"]', 1, '{"can_fork_session":true,"can_resume_session":true}', '2026-05-26T00:00:00Z')
                """
            )

    def _create_successful_execution(self, *, session_id: str = "session-1") -> object:
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
        dispatcher_patch_execution(
            leased.id,
            PatchExecutionRequest(dispatcher_id="disp", sink_token=leased.sink_token, status="running"),
        )
        for i in range(5):
            dispatcher_append_execution_events(
                leased.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp",
                    sink_token=leased.sink_token,
                    events=[
                        ExecutionEventAppend(
                            event_type="stdout",
                            payload={"text": f"output line {i}\n"},
                            event_key=f"{leased.id}:stdout:{i}",
                            ts=f"2026-05-26T00:00:{i:02d}Z",
                        )
                    ],
                ),
            )
        return dispatcher_finish_execution(
            leased.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                sink_token=leased.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="message",
                        role="assistant",
                        payload={"text": "Execution complete"},
                        event_key=f"{leased.id}:assistant:final",
                        ts="2026-05-26T00:00:10Z",
                    ),
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "succeeded"},
                        event_key=f"{leased.id}:status:terminal",
                        ts="2026-05-26T00:00:11Z",
                    ),
                ],
                patch=FinishExecutionPatch(
                    status="succeeded",
                    returncode=0,
                    remote_session_out_kind="mock_session",
                    remote_session_out_id=session_id,
                    remote_session_out_status="available",
                ),
            ),
        )

    # === Flow 1: Full execution lifecycle with append guards ===

    def test_full_execution_lifecycle_with_guards(self) -> None:
        """Complete execution: create -> lease -> running -> events -> finish -> terminal guard."""
        source = self._create_successful_execution()
        self.assertEqual(source.status, "succeeded")
        self.assertIsNotNone(source.sink_token)

        # Terminal guard: new events rejected
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                source.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp",
                    sink_token=source.sink_token,
                    events=[ExecutionEventAppend(event_type="stdout", payload={"text": "late"}, event_key="late:1", ts="t1")],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 409)

        # Terminal idempotent replay: same key+canonical works
        result = dispatcher_append_execution_events(
            source.id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp",
                sink_token=source.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "succeeded"},
                        event_key=f"{source.id}:status:terminal",
                        ts="2026-05-26T00:00:11Z",
                    )
                ],
            ),
        )
        self.assertEqual(len(result), 1)

    # === Flow 2: Fork turn1 -> fork turn2 -> source not polluted ===

    def test_fork_multi_turn_isolation(self) -> None:
        """Fork creates independent branch executions, source output unchanged."""
        source = self._create_successful_execution()

        # Create fork branch
        branch = create_branch(
            self.project_id,
            CreateBranchRequest(mode="fork", source_execution_id=source.id),
        )

        # Fork turn 1
        result1 = post_branch_message(
            self.project_id, branch.id, BranchMessageRequest(message="Fork question 1")
        )
        ex1 = result1["execution"]
        self.assertEqual(ex1.status, "pending")
        self.assertNotEqual(ex1.id, source.id)

        # Claim and finish fork turn 1
        leased1 = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=ex1.id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )
        dispatcher_finish_execution(
            leased1.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                sink_token=leased1.sink_token,
                events=[
                    ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "Fork reply 1"}, event_key=f"{leased1.id}:assistant:1", ts="t1"),
                    ExecutionEventAppend(event_type="status", payload={"status": "succeeded"}, event_key=f"{leased1.id}:status:terminal", ts="t2"),
                ],
                patch=FinishExecutionPatch(
                    status="succeeded", returncode=0,
                    remote_session_out_kind="mock_session", remote_session_out_id="fork-session-1", remote_session_out_status="available",
                ),
            ),
        )

        # Fork turn 2 should use branch_continue
        result2 = post_branch_message(
            self.project_id, branch.id, BranchMessageRequest(message="Fork question 2")
        )
        ex2 = result2["execution"]
        self.assertNotEqual(ex2.id, ex1.id)
        self.assertEqual(ex2.session_action, "branch_continue")

        # Source events not polluted by fork
        source_events = get_project_execution_events(self.project_id, source.id)
        source_texts = [e.payload.get("text", "") for e in source_events.events if e.event_type == "message"]
        self.assertNotIn("Fork reply 1", source_texts)

    # === Flow 3: Resume creates new execution per turn ===

    def test_resume_multi_turn_creates_new_executions(self) -> None:
        """Each resume message creates a new execution, never reuses source."""
        source = self._create_successful_execution()

        branch = create_branch(
            self.project_id,
            CreateBranchRequest(mode="resume", source_execution_id=source.id),
        )

        # Resume turn 1
        result1 = post_branch_message(
            self.project_id, branch.id, BranchMessageRequest(message="Resume Q1")
        )
        ex1 = result1["execution"]
        self.assertNotEqual(ex1.id, source.id)
        self.assertEqual(ex1.session_action, "resume_continue")

    # === Flow 4: 409 conflict produces bounded diagnostic ===

    def test_409_conflict_does_not_snowball(self) -> None:
        """A single 409 conflict should not produce cascading failures."""
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=execution.id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )

        # Write an event
        dispatcher_append_execution_events(
            leased.id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp",
                sink_token=leased.sink_token,
                events=[ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "A"}, event_key="key-1", ts="t1")],
            ),
        )

        # Conflicting event with same key but different payload
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                leased.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp",
                    sink_token=leased.sink_token,
                    events=[ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "B"}, event_key="key-1", ts="t1")],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 409)

        # But a different key should still work (execution is not dead)
        result = dispatcher_append_execution_events(
            leased.id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp",
                sink_token=leased.sink_token,
                events=[ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "C"}, event_key="key-2", ts="t2")],
            ),
        )
        self.assertEqual(len(result), 1)

    # === Flow 5: Batch never exceeds 250 events at API level ===

    def test_large_batch_slicing_at_api_level(self) -> None:
        """API rejects > 250 events per batch (Pydantic validation)."""
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=execution.id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )

        events_251 = [
            ExecutionEventAppend(event_type="stdout", payload={"text": f"x{i}"}, event_key=f"x{i}", ts=f"t{i}")
            for i in range(251)
        ]
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            AppendExecutionEventsRequest(dispatcher_id="disp", events=events_251)

    # === Flow 6: Pending execution append rejected, claim first ===

    def test_pending_execution_append_rejected(self) -> None:
        """Dispatcher must lease/claim before appending."""
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                execution.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp",
                    events=[ExecutionEventAppend(event_type="status", payload={"status": "running"}, event_key="s1", ts="t1")],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    # === Flow 7: Wrong dispatcher rejected ===

    def test_owner_guard_enforcement(self) -> None:
        """Different dispatcher cannot write to another's execution."""
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=execution.id, dispatcher_id="owner-disp", worker_name="mock", worker_type="mock")
        )
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                leased.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="intruder-disp",
                    events=[ExecutionEventAppend(event_type="status", payload={"status": "running"}, event_key="s1", ts="t1")],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 403)

    # === Flow 8: Branch timeline completeness after fork/resume ===

    def test_branch_timeline_complete_after_fork(self) -> None:
        """Branch timeline shows all events from all branch executions."""
        source = self._create_successful_execution()
        branch = create_branch(
            self.project_id,
            CreateBranchRequest(mode="fork", source_execution_id=source.id),
        )

        result = post_branch_message(
            self.project_id, branch.id, BranchMessageRequest(message="Hello fork")
        )
        ex = result["execution"]
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=ex.id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )
        dispatcher_finish_execution(
            leased.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                sink_token=leased.sink_token,
                events=[
                    ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "Hi"}, event_key=f"{leased.id}:assistant:1", ts="t1"),
                    ExecutionEventAppend(event_type="status", payload={"status": "succeeded"}, event_key=f"{leased.id}:status:terminal", ts="t2"),
                ],
                patch=FinishExecutionPatch(status="succeeded", returncode=0),
            ),
        )

        timeline = get_branch_timeline(self.project_id, branch.id)
        self.assertGreater(len(timeline.events), 0)
        user_msgs = [e for e in timeline.events if e.role == "user"]
        assistant_msgs = [e for e in timeline.events if e.role == "assistant"]
        self.assertTrue(any("Hello fork" in m.payload.get("text", "") for m in user_msgs))
        self.assertTrue(any("Hi" in m.payload.get("text", "") for m in assistant_msgs))


if __name__ == "__main__":
    unittest.main()
