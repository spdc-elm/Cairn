from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import AppendExecutionEventsRequest, CreateExecutionRequest, CreateProjectRequest, ExecutionEventAppend
from cairn.server.routers.executions import create_project_execution, dispatcher_append_execution_events
from cairn.server.routers.projects import create_project


class V34EventIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = project.project.id
        self.execution = create_project_execution(self.project_id, CreateExecutionRequest(task_type="explore", phase="bootstrap"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_event_key_same_canonical_event_replays_existing_row(self) -> None:
        body = AppendExecutionEventsRequest(
            events=[
                ExecutionEventAppend(
                    event_type="message",
                    role="assistant",
                    payload={"text": "final"},
                    event_key=f"{self.execution.id}:assistant:final",
                    ts="2026-05-21T00:00:00Z",
                )
            ]
        )

        first = dispatcher_append_execution_events(self.execution.id, body)[0]
        second = dispatcher_append_execution_events(self.execution.id, body)[0]
        next_event = dispatcher_append_execution_events(
            self.execution.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="status", payload={"status": "succeeded"}, event_key="status", ts="2026-05-21T00:00:01Z")]
            ),
        )[0]

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.cursor, second.cursor)
        self.assertEqual(next_event.project_seq, first.project_seq + 1)

    def test_same_event_key_different_payload_conflicts_without_consuming_cursor(self) -> None:
        first = dispatcher_append_execution_events(
            self.execution.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "one"}, event_key="same", ts="t1")]
            ),
        )[0]

        with self.assertRaises(HTTPException) as conflict:
            dispatcher_append_execution_events(
                self.execution.id,
                AppendExecutionEventsRequest(
                    events=[ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "two"}, event_key="same", ts="t1")]
                ),
            )

        self.assertEqual(conflict.exception.status_code, 409)
        next_event = dispatcher_append_execution_events(
            self.execution.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="status", payload={"status": "failed"}, event_key="next", ts="t2")]
            ),
        )[0]
        self.assertEqual(next_event.project_seq, first.project_seq + 1)


if __name__ == "__main__":
    unittest.main()
