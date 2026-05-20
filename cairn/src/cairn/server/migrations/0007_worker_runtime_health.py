from __future__ import annotations

import sqlite3

VERSION = "0007_worker_runtime_health"
DESCRIPTION = "Track worker runtime health by execution identity"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_runtime_health (
            environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
            worker_name TEXT NOT NULL,
            worker_type TEXT NOT NULL,
            endpoint_id TEXT NOT NULL DEFAULT '',
            model_profile_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('ok', 'unhealthy', 'unknown')),
            checked_at TEXT NOT NULL,
            stale_after TEXT,
            disabled_until TEXT,
            source TEXT,
            dispatcher_id TEXT,
            detail_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (environment_id, worker_name, worker_type, endpoint_id, model_profile_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_runtime_health_worker
        ON worker_runtime_health (worker_name, worker_type, endpoint_id, model_profile_id)
        """
    )
    for table in ("question_threads", "question_jobs"):
        _add_column(conn, table, "execution_environment_id", "TEXT")
        _add_column(conn, table, "execution_worker_type", "TEXT")
        _add_column(conn, table, "execution_endpoint_id", "TEXT")
        _add_column(conn, table, "execution_model_profile_id", "TEXT")


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
