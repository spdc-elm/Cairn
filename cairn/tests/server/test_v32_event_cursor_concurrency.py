from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import AppendExecutionEventsRequest, CreateExecutionRequest, CreateProjectRequest, ExecutionEventAppend
from cairn.server.routers.executions import create_project_execution, dispatcher_append_execution_events, get_project_execution_events
from cairn.server.routers.projects import create_project


class V32EventCursorConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cairn.db"
        db._db_path = None
        db.configure(self.path)
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = self.project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_cursor_is_total_order_across_executions(self) -> None:
        first = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        second = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )

        first_event = dispatcher_append_execution_events(
            first.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="stdout", payload={"text": "first"}, event_key="a")]
            ),
        )[0]
        second_event = dispatcher_append_execution_events(
            second.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="stdout", payload={"text": "second"}, event_key="b")]
            ),
        )[0]

        self.assertEqual(first_event.project_seq, 1)
        self.assertEqual(second_event.project_seq, 2)
        self.assertEqual(second_event.cursor, "evt_2")

    def test_foreign_and_invalid_cursor_return_400(self) -> None:
        execution = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="reason", phase="run"),
        )
        event = dispatcher_append_execution_events(
            execution.id,
            AppendExecutionEventsRequest(
                events=[ExecutionEventAppend(event_type="stdout", payload={"text": "first"}, event_key="a")]
            ),
        )[0]
        other = create_project(CreateProjectRequest(title="other", origin="start", goal="finish"))
        other_execution = create_project_execution(
            other.project.id,
            CreateExecutionRequest(task_type="reason", phase="run"),
        )

        with self.assertRaises(HTTPException) as invalid:
            get_project_execution_events(self.project_id, execution.id, after_cursor="evt_999")
        self.assertEqual(invalid.exception.status_code, 400)

        with self.assertRaises(HTTPException) as foreign:
            get_project_execution_events(other.project.id, other_execution.id, after_cursor=event.cursor)
        self.assertEqual(foreign.exception.status_code, 400)

    def test_two_connections_allocate_without_duplicate_project_seq(self) -> None:
        first = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        second = create_project_execution(
            self.project_id,
            CreateExecutionRequest(task_type="explore", phase="bootstrap"),
        )
        from cairn.server.services import append_execution_events

        with db.connect(self.path) as conn1:
            conn1.execute("BEGIN IMMEDIATE")
            one = append_execution_events(
                conn1,
                first.id,
                AppendExecutionEventsRequest(
                    events=[ExecutionEventAppend(event_type="stdout", payload={"text": "one"}, event_key="one")]
                ),
            )[0]
        with db.connect(self.path) as conn2:
            conn2.execute("BEGIN IMMEDIATE")
            two = append_execution_events(
                conn2,
                second.id,
                AppendExecutionEventsRequest(
                    events=[ExecutionEventAppend(event_type="stdout", payload={"text": "two"}, event_key="two")]
                ),
            )[0]

        self.assertEqual([one.project_seq, two.project_seq], [1, 2])


if __name__ == "__main__":
    unittest.main()
