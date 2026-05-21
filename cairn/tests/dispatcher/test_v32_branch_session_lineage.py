from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cairn.server import db
from cairn.server.models import CreateExecutionRequest, CreateProjectRequest, PatchExecutionRequest
from cairn.server.routers.branches import BranchMessageRequest, CreateBranchRequest, create_branch, post_branch_message
from cairn.server.routers.executions import create_project_execution, dispatcher_patch_execution
from cairn.server.routers.projects import create_project
from cairn.server.services import dumps_json


class V32BranchSessionLineageTests(unittest.TestCase):
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
                remote_session_out_id="source-session",
                remote_session_out_status="available",
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_branch_messages_create_execution_for_each_message(self) -> None:
        branch = create_branch(
            self.project_id,
            CreateBranchRequest(anchor_kind="fact", anchor_id="origin", mode="fresh_context"),
        )
        first = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="first?"))
        first_execution_id = first["execution"].id

        second = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="second?"))
        second_execution_id = second["execution"].id

        with db.get_conn() as conn:
            first = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (first_execution_id,)).fetchone()
            second = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (second_execution_id,)).fetchone()
            branches = conn.execute("SELECT * FROM branches WHERE project_id = ?", (self.project_id,)).fetchall()

        assert first is not None
        assert second is not None
        self.assertEqual(len(branches), 1)
        self.assertEqual(first["task_type"], "question")
        self.assertEqual(first["branch_id"], branches[0]["id"])
        self.assertEqual(second["branch_id"], branches[0]["id"])
        self.assertEqual(first["session_action"], "fresh_context")
        self.assertEqual(second["session_action"], "fresh_context")

    def test_fork_later_turn_continues_fork_session(self) -> None:
        self._assert_fork_later_turn_continues_worker_session("pi-main", "pi", "pi_session")

    def test_claudecode_fork_later_turn_continues_fork_session(self) -> None:
        self._assert_fork_later_turn_continues_worker_session("claude-main", "claudecode", "claude_session")

    def _assert_fork_later_turn_continues_worker_session(self, worker_name: str, worker_type: str, session_kind: str) -> None:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, task_types_json, max_running, priority,
                    question_capability_json, updated_at
                ) VALUES (?, ?, ?, 1, 0, ?, '2026-05-20T00:00:00Z')
                """,
                (
                    worker_name,
                    worker_type,
                    dumps_json(["question"]),
                    dumps_json({"can_resume_session": True, "can_fork_session": True, "unavailable_reasons": {}}),
                ),
            )
            conn.execute("UPDATE execution_runs SET worker_name = ?, worker_type = ? WHERE id = ?", (worker_name, worker_type, self.source.id))
            conn.execute("UPDATE execution_runs SET remote_session_out_kind = ? WHERE id = ?", (session_kind, self.source.id))
        branch = create_branch(
            self.project_id,
            CreateBranchRequest(
                anchor_kind="intent",
                anchor_id="i001",
                mode="fork",
                source_execution_id=self.source.id,
            ),
        )

        first = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="first?"))
        dispatcher_patch_execution(
            first["execution"].id,
            PatchExecutionRequest(
                status="succeeded",
                remote_session_out_kind="pi_session",
                remote_session_out_id="fork-session-1",
                remote_session_out_status="available",
            ),
        )
        second = post_branch_message(self.project_id, branch.id, BranchMessageRequest(message="second?"))

        with db.get_conn() as conn:
            first_row = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (first["execution"].id,)).fetchone()
            second_row = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (second["execution"].id,)).fetchone()

        assert first_row is not None
        assert second_row is not None
        self.assertEqual(first_row["session_action"], "fork_initial")
        self.assertEqual(first_row["remote_session_in_id"], "source-session")
        self.assertEqual(second_row["session_action"], "branch_continue")
        self.assertEqual(second_row["remote_session_in_id"], "fork-session-1")


if __name__ == "__main__":
    unittest.main()
