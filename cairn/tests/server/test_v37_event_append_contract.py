from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
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
)
from cairn.server.routers.projects import create_project


class V37EventAppendContractTests(unittest.TestCase):
    """Server-side append/finish contract: owner guard, status guard, sink token, terminal barrier."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="v37", origin="start", goal="finish"))
        self.project_id = project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _leased_execution(self, dispatcher_id: str = "disp-A"):
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        return dispatcher_lease_pending_execution(
            LeaseExecutionRequest(
                execution_id=execution.id,
                dispatcher_id=dispatcher_id,
                worker_name="mock",
                worker_type="mock",
            )
        )

    # --- Status guard: pending execution rejects dispatcher append ---

    def test_pending_execution_rejects_dispatcher_append(self) -> None:
        """Dispatcher cannot append events to a pending (unclaimed) execution."""
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                execution.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp-A",
                    events=[
                        ExecutionEventAppend(
                            event_type="status",
                            payload={"status": "running"},
                            event_key=f"{execution.id}:status:1",
                            ts="2026-05-26T00:00:00Z",
                        )
                    ],
                ),
            )
        self.assertIn(ctx.exception.status_code, (403, 409))

    # --- Owner guard: wrong dispatcher cannot append ---

    def test_wrong_dispatcher_cannot_append(self) -> None:
        """A dispatcher that does not own the lease cannot append events."""
        execution = self._leased_execution(dispatcher_id="disp-A")
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                execution.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp-B",
                    events=[
                        ExecutionEventAppend(
                            event_type="status",
                            payload={"status": "running"},
                            event_key=f"{execution.id}:status:1",
                            ts="2026-05-26T00:00:00Z",
                        )
                    ],
                ),
            )
        self.assertIn(ctx.exception.status_code, (403, 409))

    # --- Sink token guard: stale sink from same dispatcher rejected ---

    def test_stale_sink_token_rejected(self) -> None:
        """Same dispatcher but with a stale/different sink_token must be rejected."""
        execution = self._leased_execution(dispatcher_id="disp-A")
        self.assertIsNotNone(execution.sink_token,
                             "lease_execution must return sink_token for the v3.7 single-writer contract")

        # Correct token should work
        result = dispatcher_append_execution_events(
            execution.id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp-A",
                sink_token=execution.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "running"},
                        event_key=f"{execution.id}:status:1",
                        ts="2026-05-26T00:00:00Z",
                    )
                ],
            ),
        )
        self.assertEqual(len(result), 1)

        # Wrong token must be rejected
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                execution.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp-A",
                    sink_token="wrong-stale-token",
                    events=[
                        ExecutionEventAppend(
                            event_type="stdout",
                            payload={"text": "stale"},
                            event_key=f"{execution.id}:stdout:stale",
                            ts="2026-05-26T00:00:01Z",
                        )
                    ],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("Sink token", ctx.exception.detail)

    def test_validation_error_response_does_not_echo_large_event_batch(self) -> None:
        execution = self._leased_execution(dispatcher_id="disp-A")
        events = [
            {
                "event_type": "stdout",
                "payload": {"text": f"secret-ish-large-payload-{index}"},
                "event_key": f"too-long-{index}",
                "ts": "2026-05-26T00:00:00Z",
            }
            for index in range(251)
        ]
        with TestClient(app) as client:
            response = client.post(
                f"/dispatcher/executions/{execution.id}/events",
                json={
                    "dispatcher_id": "disp-A",
                    "sink_token": execution.sink_token,
                    "events": events,
                },
            )
        self.assertEqual(response.status_code, 422)
        self.assertLess(len(response.text), 2000)
        self.assertNotIn("secret-ish-large-payload", response.text)

    # --- Terminal execution rejects new events ---

    def test_terminal_execution_rejects_new_events(self) -> None:
        """After terminal finish, appending a NEW event (different key) must be rejected."""
        execution = self._leased_execution()
        dispatcher_finish_execution(
            execution.id,
            FinishExecutionRequest(
                dispatcher_id="disp-A",
                sink_token=execution.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "succeeded"},
                        event_key=f"{execution.id}:status:terminal",
                        ts="2026-05-26T00:00:01Z",
                    )
                ],
                patch=FinishExecutionPatch(status="succeeded", returncode=0),
            ),
        )
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                execution.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp-A",
                    sink_token=execution.sink_token,
                    events=[
                        ExecutionEventAppend(
                            event_type="message",
                            role="assistant",
                            payload={"text": "late arrival"},
                            event_key=f"{execution.id}:late:1",
                            ts="2026-05-26T00:00:02Z",
                        )
                    ],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    # --- Terminal execution allows idempotent replay of same key+canonical ---

    def test_terminal_execution_allows_idempotent_replay(self) -> None:
        """After terminal, same key + same canonical event replays the existing row."""
        execution = self._leased_execution()
        terminal_event = ExecutionEventAppend(
            event_type="status",
            payload={"status": "succeeded"},
            event_key=f"{execution.id}:status:terminal",
            ts="2026-05-26T00:00:01Z",
        )
        dispatcher_finish_execution(
            execution.id,
            FinishExecutionRequest(
                dispatcher_id="disp-A",
                sink_token=execution.sink_token,
                events=[terminal_event],
                patch=FinishExecutionPatch(status="succeeded", returncode=0),
            ),
        )
        # Replaying same event should succeed (idempotent)
        result = dispatcher_append_execution_events(
            execution.id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp-A",
                sink_token=execution.sink_token,
                events=[terminal_event],
            ),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].event_key, f"{execution.id}:status:terminal")

    # --- Mid-batch conflict rolls back entire batch ---

    def test_mid_batch_conflict_rolls_back_entire_batch(self) -> None:
        """If event 2 of a 3-event batch conflicts, none of the events in that batch should be newly persisted."""
        execution = self._leased_execution()
        # Pre-write an event with key "conflict-key"
        dispatcher_append_execution_events(
            execution.id,
            AppendExecutionEventsRequest(
                dispatcher_id="disp-A",
                sink_token=execution.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="message",
                        role="assistant",
                        payload={"text": "original"},
                        event_key="conflict-key",
                        ts="2026-05-26T00:00:00Z",
                    )
                ],
            ),
        )
        # Now send batch where event 2 conflicts
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_append_execution_events(
                execution.id,
                AppendExecutionEventsRequest(
                    dispatcher_id="disp-A",
                    sink_token=execution.sink_token,
                    events=[
                        ExecutionEventAppend(
                            event_type="stdout",
                            payload={"text": "new-1"},
                            event_key="batch-new-1",
                            ts="2026-05-26T00:00:01Z",
                        ),
                        ExecutionEventAppend(
                            event_type="message",
                            role="assistant",
                            payload={"text": "DIFFERENT"},
                            event_key="conflict-key",
                            ts="2026-05-26T00:00:00Z",
                        ),
                        ExecutionEventAppend(
                            event_type="stdout",
                            payload={"text": "new-3"},
                            event_key="batch-new-3",
                            ts="2026-05-26T00:00:02Z",
                        ),
                    ],
                ),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        # Verify "batch-new-1" was NOT persisted (rollback)
        from cairn.server.routers.executions import get_project_execution_events
        page = get_project_execution_events(self.project_id, execution.id)
        event_keys = [e.event_key for e in page.events]
        self.assertNotIn("batch-new-1", event_keys,
                         "Mid-batch conflict must roll back preceding events in same batch")

    # --- Dispatcher patch on terminal execution is rejected (except allowed followup) ---

    def test_dispatcher_patch_status_on_terminal_rejected(self) -> None:
        """After terminal, attempting to change status via patch must be rejected."""
        execution = self._leased_execution()
        dispatcher_finish_execution(
            execution.id,
            FinishExecutionRequest(
                dispatcher_id="disp-A",
                sink_token=execution.sink_token,
                events=[
                    ExecutionEventAppend(
                        event_type="status",
                        payload={"status": "failed"},
                        event_key=f"{execution.id}:status:terminal",
                        ts="2026-05-26T00:00:01Z",
                    )
                ],
                patch=FinishExecutionPatch(status="failed", returncode=1, error_code="worker_crashed"),
            ),
        )
        with self.assertRaises(HTTPException) as ctx:
            dispatcher_patch_execution(
                execution.id,
                PatchExecutionRequest(dispatcher_id="disp-A", status="succeeded"),
            )
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
