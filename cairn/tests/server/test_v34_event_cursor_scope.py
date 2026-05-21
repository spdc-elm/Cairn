from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import AppendExecutionEventsRequest, CreateExecutionRequest, CreateProjectRequest, ExecutionEventAppend
from cairn.server.routers.branches import BranchMessageRequest, CreateBranchRequest, create_branch, get_branch_timeline, post_branch_message
from cairn.server.routers.executions import create_project_execution, dispatcher_append_execution_events, get_project_execution_events
from cairn.server.routers.projects import create_project


class V34EventCursorScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_execution_events_reject_other_execution_cursor_in_same_project(self) -> None:
        one = create_project_execution(self.project_id, CreateExecutionRequest(task_type="explore", phase="bootstrap"))
        two = create_project_execution(self.project_id, CreateExecutionRequest(task_type="explore", phase="bootstrap"))
        first = dispatcher_append_execution_events(
            one.id,
            AppendExecutionEventsRequest(events=[ExecutionEventAppend(event_type="stdout", payload={"text": "one"}, event_key="one")]),
        )[0]

        with self.assertRaises(HTTPException) as conflict:
            get_project_execution_events(self.project_id, two.id, after_cursor=first.cursor)

        self.assertEqual(conflict.exception.status_code, 400)

    def test_branch_timeline_rejects_other_branch_cursor_in_same_project(self) -> None:
        branch_one = create_branch(self.project_id, CreateBranchRequest(mode="fresh_context"))
        branch_two = create_branch(self.project_id, CreateBranchRequest(mode="fresh_context"))
        message_one = post_branch_message(self.project_id, branch_one.id, BranchMessageRequest(message="one"))
        post_branch_message(self.project_id, branch_two.id, BranchMessageRequest(message="two"))
        page_one = get_branch_timeline(self.project_id, branch_one.id)
        cursor = page_one.events[-1].cursor

        with self.assertRaises(HTTPException) as conflict:
            get_branch_timeline(self.project_id, branch_two.id, after_cursor=cursor)

        self.assertEqual(message_one["execution"].branch_id, branch_one.id)
        self.assertEqual(conflict.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
