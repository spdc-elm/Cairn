from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from pydantic import ValidationError

from cairn.server import db
from cairn.server.migrations import runner
from cairn.server.models import CreateIntentRequest, CreateProjectRequest, Intent
from cairn.server.services import dumps_json, fact_to_model, loads_json_list, project_meta_from_row


class CommandBlackboardV2SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_schema_contains_v2_columns(self) -> None:
        with db.get_conn() as conn:
            project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
            fact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
            intent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
            versions = tuple(row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))

        self.assertIn("auto_reason", project_columns)
        self.assertIn("allowed_auto_workers_json", project_columns)
        self.assertIn("default_timeout_seconds", project_columns)
        self.assertIn("title", fact_columns)
        self.assertIn("metadata_json", fact_columns)
        self.assertIn("requested_worker", intent_columns)
        self.assertIn("concluded_fact_id", intent_columns)
        self.assertNotIn("control_state", intent_columns)
        self.assertNotIn("last_heartbeat_at", intent_columns)
        self.assertEqual(versions, tuple(migration.version for migration in runner.available_migrations()))

    def test_old_db_migration_preserves_rows_with_v2_defaults(self) -> None:
        legacy_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(legacy_tmp.cleanup)
        legacy_path = Path(legacy_tmp.name) / "legacy.db"
        with sqlite3.connect(str(legacy_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE facts (id TEXT NOT NULL, project_id TEXT NOT NULL, description TEXT NOT NULL, PRIMARY KEY (id, project_id))")
            conn.execute("CREATE TABLE intents (id TEXT NOT NULL, project_id TEXT NOT NULL, to_fact_id TEXT, description TEXT NOT NULL, creator TEXT NOT NULL, worker TEXT, last_heartbeat_at TEXT, created_at TEXT NOT NULL, concluded_at TEXT, PRIMARY KEY (id, project_id))")
            conn.execute("CREATE TABLE intent_sources (intent_id TEXT NOT NULL, project_id TEXT NOT NULL, fact_id TEXT NOT NULL, PRIMARY KEY (intent_id, project_id, fact_id))")
            conn.execute("CREATE TABLE work_environments (id TEXT PRIMARY KEY, label TEXT NOT NULL, backend TEXT NOT NULL, ssh_command TEXT, workspace_root TEXT, cleanup_json TEXT, terminal_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_health_status TEXT, last_healthcheck_json TEXT)")
            conn.execute("INSERT INTO projects (id, title, status, created_at) VALUES ('proj_001', 'old', 'active', '2026-01-01T00:00:00Z')")
            conn.execute("INSERT INTO facts (id, project_id, description) VALUES ('origin', 'proj_001', 'start')")
            conn.execute("INSERT INTO intents (id, project_id, description, creator, created_at) VALUES ('i001', 'proj_001', 'do it', 'human', '2026-01-01T00:00:00Z')")
            conn.commit()

        db._db_path = None
        db.configure(legacy_path)
        with db.get_conn() as conn:
            project = project_meta_from_row(conn.execute("SELECT * FROM projects WHERE id = 'proj_001'").fetchone())
            fact = fact_to_model(conn.execute("SELECT * FROM facts WHERE id = 'origin' AND project_id = 'proj_001'").fetchone())
            versions = tuple(row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))
            intent = Intent.model_validate({
                "id": "i001",
                "from": [],
                "description": "do it",
                "creator": "human",
                "created_at": "2026-01-01T00:00:00Z",
            })

        self.assertFalse(project.auto_reason)
        self.assertIsNone(project.allowed_auto_workers)
        self.assertEqual(fact.title, "start")
        self.assertIsNone(fact.metadata)
        self.assertEqual(intent.control_state, "normal")
        self.assertEqual(versions, tuple(migration.version for migration in runner.available_migrations()))

    def test_migration_runner_is_idempotent(self) -> None:
        with db.get_conn() as conn:
            first = runner.migrate(conn)
            second = runner.migrate(conn)
            version_count = conn.execute("SELECT COUNT(*) AS count FROM schema_migrations").fetchone()["count"]

        self.assertEqual(first.pending, ())
        self.assertEqual(second.pending, ())
        self.assertEqual(version_count, len(runner.available_migrations()))

    def test_failed_migration_rolls_back_current_step(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "rollback.db"

        def apply_ok(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE ok_table (id TEXT PRIMARY KEY)")

        def apply_bad(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE bad_table (id TEXT PRIMARY KEY)")
            raise RuntimeError("boom")

        migrations = (
            runner.Migration("0001_ok", "ok", apply_ok),
            runner.Migration("0002_bad", "bad", apply_bad),
        )
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            with self.assertRaises(RuntimeError):
                runner.migrate(conn, migrations)
            versions = tuple(row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

        self.assertEqual(versions, ("0001_ok",))
        self.assertIn("ok_table", tables)
        self.assertNotIn("bad_table", tables)

    def test_json_list_round_trip(self) -> None:
        payload = ["pi-GPT5.5", "codex-main"]
        self.assertEqual(loads_json_list(dumps_json(payload)), payload)

    def test_invalid_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CreateProjectRequest(title="x", origin="o", goal="g", default_timeout_seconds=0)
        with self.assertRaises(ValidationError):
            CreateIntentRequest(**{"from": ["origin"], "description": "d", "creator": "human", "timeout_override_seconds": -1})


if __name__ == "__main__":
    unittest.main()
