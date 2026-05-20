from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from cairn.server import db
from cairn.server.migrations import runner
from cairn.server.services import (
    create_run_provenance,
    dumps_json,
    resolve_anchor,
    update_run_remote_session,
)


class V3RunProvenanceSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_schema_contains_run_provenance(self) -> None:
        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(run_provenance)").fetchall()}
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(run_provenance)").fetchall()}
            versions = tuple(row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version"))

        self.assertIn("run_log_id", columns)
        self.assertIn("project_id", columns)
        self.assertIn("remote_session_status", columns)
        self.assertIn("report_run_id", columns)
        self.assertIn("idx_run_provenance_project_intent_started", indexes)
        self.assertEqual(versions, tuple(migration.version for migration in runner.available_migrations()))

    def test_legacy_fact_run_id_is_report_only_and_anchor_missing(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
                ("proj_001", "case", "2026-05-19T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO facts (id, project_id, title, description, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (
                    "f001",
                    "proj_001",
                    "legacy",
                    "old fact",
                    dumps_json({"run_id": "run_report_abc", "report_path": "/tmp/report.md"}),
                ),
            )

            resolved = resolve_anchor(conn, "proj_001", "fact", "f001")

        self.assertEqual(resolved.status, "missing")
        self.assertEqual(resolved.reason, "legacy_report_run_id_only")
        self.assertIsNone(resolved.source_run_log_id)
        self.assertIsNone(resolved.provenance)

    def test_exact_fact_anchor_uses_db_producing_run(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
                ("proj_001", "case", "2026-05-19T00:00:00Z"),
            )
            create_run_provenance(
                conn,
                run_log_id="run_log_001",
                project_id="proj_001",
                intent_id="i001",
                task_type="explore",
                phase="explore_execute",
                worker_name="codex-main",
                worker_type="codex",
                started_at="2026-05-19T00:00:00Z",
            )
            update_run_remote_session(
                conn,
                "proj_001",
                "run_log_001",
                remote_session_id="thread-123",
                remote_session_kind="codex_thread",
                remote_session_status="available",
                remote_session_capture_method="stdout_event",
            )
            conn.execute(
                "INSERT INTO facts (id, project_id, title, description, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (
                    "f001",
                    "proj_001",
                    "new",
                    "new fact",
                    dumps_json({"provenance": {"producing_run_log_id": "run_log_001"}}),
                ),
            )

            resolved = resolve_anchor(conn, "proj_001", "fact", "f001")

        self.assertEqual(resolved.status, "exact")
        self.assertEqual(resolved.source_run_log_id, "run_log_001")
        self.assertIsNotNone(resolved.provenance)
        self.assertEqual(resolved.provenance.remote_session.status, "available")

    def test_old_db_migration_adds_run_provenance_without_rewriting_legacy_metadata(self) -> None:
        legacy_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(legacy_tmp.cleanup)
        legacy_path = Path(legacy_tmp.name) / "legacy.db"
        with sqlite3.connect(str(legacy_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE facts (id TEXT NOT NULL, project_id TEXT NOT NULL, description TEXT NOT NULL, metadata_json TEXT, PRIMARY KEY (id, project_id))")
            conn.execute("CREATE TABLE intents (id TEXT NOT NULL, project_id TEXT NOT NULL, to_fact_id TEXT, description TEXT NOT NULL, creator TEXT NOT NULL, worker TEXT, last_heartbeat_at TEXT, created_at TEXT NOT NULL, concluded_at TEXT, PRIMARY KEY (id, project_id))")
            conn.execute("CREATE TABLE intent_sources (intent_id TEXT NOT NULL, project_id TEXT NOT NULL, fact_id TEXT NOT NULL, PRIMARY KEY (intent_id, project_id, fact_id))")
            conn.execute("CREATE TABLE work_environments (id TEXT PRIMARY KEY, label TEXT NOT NULL, backend TEXT NOT NULL, ssh_command TEXT, workspace_root TEXT, cleanup_json TEXT, terminal_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'legacy', '2026-05-19T00:00:00Z')")
            conn.execute(
                "INSERT INTO facts (id, project_id, description, metadata_json) VALUES ('f001', 'proj_001', 'old', ?)",
                (dumps_json({"run_id": "run_report_abc"}),),
            )
            conn.commit()

        db._db_path = None
        db.configure(legacy_path)

        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(run_provenance)").fetchall()}
            metadata_json = conn.execute(
                "SELECT metadata_json FROM facts WHERE id = 'f001' AND project_id = 'proj_001'"
            ).fetchone()["metadata_json"]
            resolved = resolve_anchor(conn, "proj_001", "fact", "f001")

        self.assertIn("run_log_id", columns)
        self.assertEqual(metadata_json, dumps_json({"run_id": "run_report_abc"}))
        self.assertEqual(resolved.status, "missing")
        self.assertEqual(resolved.reason, "legacy_report_run_id_only")


if __name__ == "__main__":
    unittest.main()
