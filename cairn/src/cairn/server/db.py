from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    environment_id TEXT,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS work_environments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    backend TEXT NOT NULL,
    ssh_command TEXT,
    workspace_root TEXT,
    harness TEXT NOT NULL DEFAULT 'pi',
    cleanup_json TEXT,
    terminal_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_health_status TEXT,
    last_healthcheck_json TEXT
);

CREATE TABLE IF NOT EXISTS environment_provider_endpoints (
    environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL,
    type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    provider_api TEXT,
    api_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (environment_id, endpoint_id)
);

INSERT OR IGNORE INTO work_environments (
    id, label, backend, workspace_root, harness, cleanup_json, terminal_json, created_at, updated_at
) VALUES (
    'docker-default', 'Docker Default', 'docker', NULL, 'pi', '{"completed_action":"stop"}', '{"mode":"none"}', strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now')
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "environment_id" not in project_columns:
        conn.execute("ALTER TABLE projects ADD COLUMN environment_id TEXT")
    environment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(work_environments)").fetchall()}
    for name, ddl in {
        "cleanup_json": "ALTER TABLE work_environments ADD COLUMN cleanup_json TEXT",
        "terminal_json": "ALTER TABLE work_environments ADD COLUMN terminal_json TEXT",
    }.items():
        if name not in environment_columns:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_provider_endpoints (
            environment_id TEXT NOT NULL REFERENCES work_environments(id) ON DELETE CASCADE,
            endpoint_id TEXT NOT NULL,
            type TEXT NOT NULL,
            base_url TEXT NOT NULL,
            provider_api TEXT,
            api_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (environment_id, endpoint_id)
        )
        """
    )


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
