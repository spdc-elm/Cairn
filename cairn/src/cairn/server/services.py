from __future__ import annotations

import sqlite3
import json
from pathlib import PurePosixPath
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import (
    Intent,
    ProjectMeta,
    ProjectReason,
    ProviderEndpointPublic,
    ProviderEndpointSecret,
    ProviderEndpointUpsert,
    WorkEnvironmentPublic,
    WorkEnvironmentUpsert,
)

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_intent_delete_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row, environment: WorkEnvironmentPublic | None = None) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
        environment_id=row["environment_id"] if "environment_id" in row.keys() else None,
        environment=environment,
        planned_workspace=planned_workspace_for(row["id"], environment),
    )


def environment_row_to_public(
    row: sqlite3.Row,
    *,
    conn: sqlite3.Connection | None = None,
    include_secret: bool = False,
) -> WorkEnvironmentPublic:
    health = None
    raw_health = row["last_healthcheck_json"] if "last_healthcheck_json" in row.keys() else None
    if raw_health:
        try:
            health = json.loads(raw_health)
        except json.JSONDecodeError:
            health = None
    endpoints: list[ProviderEndpointPublic] = []
    if conn is not None:
        endpoints = list_environment_provider_endpoints(
            conn,
            row["id"],
            include_secret=include_secret,
        )
    return WorkEnvironmentPublic(
        id=row["id"],
        label=row["label"],
        backend=row["backend"],
        ssh_command=row["ssh_command"],
        workspace_root=row["workspace_root"],
        cleanup=_loads_optional_json(row["cleanup_json"]) if "cleanup_json" in row.keys() else None,
        terminal=_loads_optional_json(row["terminal_json"]) if "terminal_json" in row.keys() else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_health_status=row["last_health_status"],
        last_healthcheck=health,
        provider_endpoints=endpoints,
    )


def get_environment_or_404(conn: sqlite3.Connection, environment_id: str, *, include_secret: bool = False) -> WorkEnvironmentPublic:
    row = conn.execute("SELECT * FROM work_environments WHERE id = ?", (environment_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Environment not found")
    return environment_row_to_public(row, conn=conn, include_secret=include_secret)


def default_environment(conn: sqlite3.Connection) -> WorkEnvironmentPublic:
    row = conn.execute(
        "SELECT * FROM work_environments ORDER BY CASE WHEN id = 'docker-default' THEN 0 ELSE 1 END, created_at LIMIT 1"
    ).fetchone()
    assert row is not None
    return environment_row_to_public(row, conn=conn)


def endpoint_row_to_public(
    row: sqlite3.Row,
    *,
    include_secret: bool = False,
) -> ProviderEndpointPublic:
    api_key = row["api_key"]
    model_type = ProviderEndpointSecret if include_secret else ProviderEndpointPublic
    payload = {
        "id": row["endpoint_id"],
        "type": row["type"],
        "base_url": row["base_url"],
        "provider_api": row["provider_api"],
        "has_api_key": bool(api_key),
        "api_key_preview": _api_key_preview(api_key),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_secret:
        payload["api_key"] = api_key
    return model_type.model_validate(payload)


def list_environment_provider_endpoints(
    conn: sqlite3.Connection,
    environment_id: str,
    *,
    include_secret: bool = False,
) -> list[ProviderEndpointPublic]:
    rows = conn.execute(
        """
        SELECT *
        FROM environment_provider_endpoints
        WHERE environment_id = ?
        ORDER BY endpoint_id
        """,
        (environment_id,),
    ).fetchall()
    return [endpoint_row_to_public(row, include_secret=include_secret) for row in rows]


def get_environment_provider_endpoint_or_404(
    conn: sqlite3.Connection,
    environment_id: str,
    endpoint_id: str,
    *,
    include_secret: bool = False,
) -> ProviderEndpointPublic:
    get_environment_or_404(conn, environment_id)
    row = conn.execute(
        """
        SELECT *
        FROM environment_provider_endpoints
        WHERE environment_id = ? AND endpoint_id = ?
        """,
        (environment_id, endpoint_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Provider endpoint not found")
    return endpoint_row_to_public(row, include_secret=include_secret)


def upsert_environment_provider_endpoint(
    conn: sqlite3.Connection,
    environment_id: str,
    body: ProviderEndpointUpsert,
) -> ProviderEndpointPublic:
    get_environment_or_404(conn, environment_id)
    now = utcnow()
    existing = conn.execute(
        """
        SELECT *
        FROM environment_provider_endpoints
        WHERE environment_id = ? AND endpoint_id = ?
        """,
        (environment_id, body.id),
    ).fetchone()
    api_key = _next_api_key(existing["api_key"] if existing is not None else None, body)
    if existing is None:
        conn.execute(
            """
            INSERT INTO environment_provider_endpoints (
                environment_id, endpoint_id, type, base_url, provider_api, api_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                environment_id,
                body.id,
                body.type,
                body.base_url,
                body.provider_api,
                api_key,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE environment_provider_endpoints
            SET type = ?,
                base_url = ?,
                provider_api = ?,
                api_key = ?,
                updated_at = ?
            WHERE environment_id = ? AND endpoint_id = ?
            """,
            (
                body.type,
                body.base_url,
                body.provider_api,
                api_key,
                now,
                environment_id,
                body.id,
            ),
        )
    return get_environment_provider_endpoint_or_404(conn, environment_id, body.id)


def _api_key_preview(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:3]}...{api_key[-4:]}"


def _next_api_key(existing: str | None, body: ProviderEndpointUpsert) -> str | None:
    if body.clear_api_key:
        return None
    if body.api_key is None:
        return existing
    return body.api_key


def slugify_environment_id(label: str) -> str:
    cleaned = []
    for ch in label.lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif cleaned and cleaned[-1] != "-":
            cleaned.append("-")
    text = "".join(cleaned).strip("-")
    return text or "environment"


def validate_environment_body(body: WorkEnvironmentUpsert) -> None:
    if body.backend == "ssh":
        if not body.ssh_command:
            raise HTTPException(400, "SSH environment requires ssh_command")
        root = (body.workspace_root or "").rstrip("/")
        if root in {"", "/", "/home", "/home/kali", "/home/kali/ctf"} or root.startswith("/home/kali/ctf/"):
            raise HTTPException(400, "Unsafe SSH workspace_root")
    if body.backend == "docker" and body.id not in (None, "docker-default"):
        raise HTTPException(400, "Only docker-default is supported from the server UI")


def planned_workspace_for(project_id: str, environment: WorkEnvironmentPublic | None) -> str | None:
    if environment is None:
        return None
    clean_project_id = project_id.replace("/", "-").replace("..", "-")
    if environment.backend == "ssh":
        root = (environment.workspace_root or "/home/kali/cairn-workspaces").rstrip("/")
        return str(PurePosixPath(root) / clean_project_id)
    if environment.backend == "docker":
        return f"docker:{clean_project_id}"
    return None


def _loads_optional_json(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
