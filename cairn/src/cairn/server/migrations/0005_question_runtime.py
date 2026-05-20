from __future__ import annotations

import sqlite3

VERSION = "0005_question_runtime"
DESCRIPTION = "Add short-lived interactive question runtime tables"


def apply(conn: sqlite3.Connection) -> None:
    statements = """
        CREATE TABLE IF NOT EXISTS question_threads (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            anchor_type TEXT NOT NULL,
            anchor_id TEXT NOT NULL,
            source_run_log_id TEXT,
            source_remote_session_kind TEXT,
            source_remote_session_id TEXT,
            source_remote_session_status TEXT NOT NULL,
            worker_name TEXT,
            mode TEXT NOT NULL,
            session_effect TEXT NOT NULL,
            status TEXT NOT NULL,
            notice TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            closed_at TEXT,
            metadata_json TEXT
        );

        CREATE TABLE IF NOT EXISTS question_jobs (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES question_threads(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            mode TEXT NOT NULL,
            message TEXT NOT NULL,
            prompt_context_json TEXT,
            status TEXT NOT NULL,
            claimed_by TEXT,
            claimed_at TEXT,
            claim_expires_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            result_text TEXT,
            error_code TEXT,
            error_detail TEXT,
            run_log_id TEXT,
            question_remote_session_kind TEXT,
            question_remote_session_id TEXT,
            question_remote_session_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(thread_id, seq)
        );

        CREATE TABLE IF NOT EXISTS question_events (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES question_threads(id) ON DELETE CASCADE,
            job_id TEXT REFERENCES question_jobs(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            event_key TEXT,
            event_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS question_resume_locks (
            project_id TEXT NOT NULL,
            remote_session_kind TEXT NOT NULL,
            remote_session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            job_id TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, remote_session_kind, remote_session_id)
        );

        CREATE INDEX IF NOT EXISTS idx_question_jobs_status_claim
        ON question_jobs (status, claim_expires_at, created_at);

        CREATE INDEX IF NOT EXISTS idx_question_jobs_project_thread_seq
        ON question_jobs (project_id, thread_id, seq);

        CREATE INDEX IF NOT EXISTS idx_question_events_thread_seq
        ON question_events (thread_id, seq);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_question_events_job_key
        ON question_events (job_id, event_key) WHERE event_key IS NOT NULL;
        """
    for statement in statements.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)
