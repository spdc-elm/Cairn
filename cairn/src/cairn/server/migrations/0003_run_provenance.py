from __future__ import annotations

import sqlite3

VERSION = "0003_run_provenance"
DESCRIPTION = "Add worker run and remote session provenance"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_provenance (
            run_log_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            intent_id TEXT,
            task_type TEXT NOT NULL,
            phase TEXT NOT NULL,
            worker_name TEXT NOT NULL,
            worker_type TEXT,
            environment_id TEXT,
            environment_backend TEXT,
            environment_target TEXT,
            workspace TEXT,
            model_profile_id TEXT,
            endpoint_id TEXT,
            timeout_seconds INTEGER,
            report_path TEXT,
            report_run_id TEXT,
            remote_session_id TEXT,
            remote_session_kind TEXT,
            remote_session_status TEXT NOT NULL DEFAULT 'unresolved'
                CHECK (remote_session_status IN ('available', 'missing', 'unresolved')),
            remote_session_capture_method TEXT,
            parent_run_log_id TEXT,
            parent_remote_session_id TEXT,
            question_mode TEXT,
            question_anchor_type TEXT,
            question_anchor_id TEXT,
            source_run_log_id TEXT,
            source_remote_session_id TEXT,
            session_effect TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            returncode INTEGER,
            timed_out INTEGER,
            cancelled INTEGER,
            cancel_reason TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_provenance_project_intent_started "
        "ON run_provenance (project_id, intent_id, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_provenance_project_task_started "
        "ON run_provenance (project_id, task_type, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_provenance_project_source_run "
        "ON run_provenance (project_id, source_run_log_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_provenance_remote_session "
        "ON run_provenance (remote_session_kind, remote_session_id)"
    )
