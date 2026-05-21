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
from cairn.server.routers.projects import create_project


class V34ExecutionFinishApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _leased_execution(self):
        execution = create_project_execution(self.project_id, CreateExecutionRequest(task_type="explore", phase="bootstrap"))
        return dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=execution.id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )

    def test_finish_commits_events_and_terminal_status_atomically(self) -> None:
        execution = self._leased_execution()

        finished = dispatcher_finish_execution(
            execution.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                events=[
                    ExecutionEventAppend(
                        event_type="message",
                        role="assistant",
                        payload={"text": "final"},
                        event_key=f"{execution.id}:assistant:final",
                        ts="2026-05-21T00:00:00Z",
                    ),
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "succeeded"},
                        event_key=f"{execution.id}:status:terminal",
                        ts="2026-05-21T00:00:01Z",
                    ),
                ],
                patch=FinishExecutionPatch(
                    status="succeeded",
                    returncode=0,
                    remote_session_out_kind="mock_session",
                    remote_session_out_id="session-1",
                    remote_session_out_status="available",
                ),
            ),
        )

        page = get_project_execution_events(self.project_id, execution.id)
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.remote_session_out_id, "session-1")
        self.assertEqual([event.event_type for event in page.events], ["message", "status"])

    def test_finish_without_session_fields_preserves_existing_session_output(self) -> None:
        execution = self._leased_execution()
        dispatcher_patch_execution(
            execution.id,
            PatchExecutionRequest(
                remote_session_out_kind="pi_session",
                remote_session_out_id="session-before-finish",
                remote_session_out_status="available",
            ),
        )

        finished = dispatcher_finish_execution(
            execution.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                events=[ExecutionEventAppend(event_type="status", payload={"status": "succeeded"}, event_key="terminal", ts="t1")],
                patch=FinishExecutionPatch(status="succeeded", returncode=0),
            ),
        )

        self.assertEqual(finished.remote_session_out_kind, "pi_session")
        self.assertEqual(finished.remote_session_out_id, "session-before-finish")
        self.assertEqual(finished.remote_session_out_status, "available")

    def test_finish_conflicting_event_rolls_back_terminal_patch(self) -> None:
        execution = self._leased_execution()
        dispatcher_append_execution_events(
            execution.id,
            AppendExecutionEventsRequest(
                events=[
                    ExecutionEventAppend(
                        event_type="message",
                        role="assistant",
                        payload={"text": "old"},
                        event_key="final",
                        ts="t1",
                    )
                ]
            ),
        )

        with self.assertRaises(HTTPException) as conflict:
            dispatcher_finish_execution(
                execution.id,
                FinishExecutionRequest(
                    dispatcher_id="disp",
                    events=[
                        ExecutionEventAppend(
                            event_type="message",
                            role="assistant",
                            payload={"text": "new"},
                            event_key="final",
                            ts="t1",
                        )
                    ],
                    patch=FinishExecutionPatch(status="succeeded", returncode=0),
                ),
            )

        current = dispatcher_patch_execution(execution.id, PatchExecutionRequest())
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(current.status, "leased")

    def test_duplicate_finish_is_idempotent_but_different_payload_conflicts(self) -> None:
        execution = self._leased_execution()
        body = FinishExecutionRequest(
            dispatcher_id="disp",
            events=[ExecutionEventAppend(event_type="status", payload={"status": "failed"}, event_key="terminal", ts="t1")],
            patch=FinishExecutionPatch(status="failed", returncode=1, error_code="worker_process_failed"),
        )

        first = dispatcher_finish_execution(execution.id, body)
        second = dispatcher_finish_execution(execution.id, body)

        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")
        with self.assertRaises(HTTPException) as conflict:
            dispatcher_finish_execution(
                execution.id,
                FinishExecutionRequest(
                    dispatcher_id="disp",
                    events=[ExecutionEventAppend(event_type="status", payload={"status": "succeeded"}, event_key="terminal", ts="t1")],
                    patch=FinishExecutionPatch(status="succeeded", returncode=0),
                ),
            )
        self.assertEqual(conflict.exception.status_code, 409)

    def test_non_owner_and_post_terminal_heartbeat_are_rejected(self) -> None:
        execution = self._leased_execution()
        with self.assertRaises(HTTPException) as owner:
            dispatcher_finish_execution(
                execution.id,
                FinishExecutionRequest(dispatcher_id="other", events=[], patch=FinishExecutionPatch(status="failed")),
            )
        self.assertEqual(owner.exception.status_code, 403)

        dispatcher_finish_execution(
            execution.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                events=[ExecutionEventAppend(event_type="status", payload={"status": "cancelled"}, event_key="terminal", ts="t1")],
                patch=FinishExecutionPatch(status="cancelled"),
            ),
        )
        with self.assertRaises(HTTPException) as heartbeat:
            dispatcher_patch_execution(execution.id, PatchExecutionRequest(last_heartbeat_at="2026-05-21T00:00:02Z", lease_seconds=60))
        self.assertEqual(heartbeat.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
