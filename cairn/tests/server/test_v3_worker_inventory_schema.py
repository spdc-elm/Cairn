from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from cairn.server import db
from cairn.server.migrations import runner


class V3WorkerInventorySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        db._db_path = None

    def tearDown(self) -> None:
        db._db_path = None

    def test_db_with_old_0004_gets_question_capability_columns_from_0006(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "legacy-worker.db"
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                """
                CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            for version in (
                "0001_initial",
                "0002_current_additive_schema",
                "0003_run_provenance",
                "0004_worker_inventory_model",
            ):
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-05-19T00:00:00Z')",
                    (version,),
                )
            conn.execute(
                """
                CREATE TABLE worker_inventory (
                    name TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    model_profile TEXT,
                    model TEXT,
                    model_context_window INTEGER,
                    endpoint TEXT,
                    task_types_json TEXT NOT NULL,
                    max_running INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    allowed_environments_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

        db.configure(path)

        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(worker_inventory)").fetchall()}
            status = runner.status(conn)

        self.assertIn("question_capability_json", columns)
        self.assertIn("capability_updated_at", columns)
        self.assertIn("capability_source", columns)
        self.assertEqual(status.pending, ())


if __name__ == "__main__":
    unittest.main()
