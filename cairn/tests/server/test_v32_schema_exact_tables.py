from __future__ import annotations

import sqlite3
import unittest

from cairn.server.app import app
from cairn.server.migrations import runner


class V32SchemaTests(unittest.TestCase):
    def test_fresh_schema_contains_execution_ledger_without_legacy_runtime_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        runner.migrate(conn)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

        self.assertIn("execution_runs", tables)
        self.assertIn("execution_events", tables)
        self.assertIn("branches", tables)
        self.assertIn("artifacts", tables)
        self.assertIn("evidence_links", tables)
        self.assertIn("execution_session_locks", tables)
        self.assertNotIn("run_provenance", tables)
        self.assertNotIn("question_threads", tables)
        self.assertNotIn("question_jobs", tables)
        self.assertNotIn("question_events", tables)
        self.assertNotIn("question_resume_locks", tables)

    def test_fresh_schema_removes_intent_and_project_live_lease_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        runner.migrate(conn)
        intent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
        project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}

        self.assertNotIn("worker", intent_columns)
        self.assertNotIn("last_heartbeat_at", intent_columns)
        self.assertNotIn("control_state", intent_columns)
        self.assertNotIn("to_fact_id", intent_columns)
        self.assertIn("concluded_fact_id", intent_columns)
        self.assertNotIn("reason_worker", project_columns)
        self.assertNotIn("reason_last_heartbeat_at", project_columns)

    def test_execution_runs_has_explicit_session_action_column(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        runner.migrate(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(execution_runs)")}

        self.assertIn("session_action", columns)
        self.assertIn("remote_session_in_id", columns)
        self.assertIn("remote_session_out_id", columns)

    def test_v32_app_does_not_mount_legacy_runs_router(self) -> None:
        paths = {route.path for route in app.routes}

        self.assertNotIn("/projects/{project_id}/runs", paths)
        self.assertNotIn("/projects/{project_id}/runs/provenance", paths)
        self.assertNotIn("/projects/{project_id}/runs/{run_id}/transcript", paths)


if __name__ == "__main__":
    unittest.main()
