from __future__ import annotations

import sqlite3
import json
from pathlib import PurePosixPath
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import (
    AnchorResolution,
    Fact,
    Intent,
    ProjectMeta,
    ProjectReason,
    ProviderEndpointPublic,
    RemoteSessionProvenance,
    RunProvenance,
    ProviderEndpointSecret,
    ProviderEndpointUpsert,
    RunProvenanceUpsert,
    WorkerRuntimeHealth,
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


def validate_requested_worker(worker: str | None) -> None:
    if worker is not None and not worker.strip():
        raise HTTPException(400, "requested_worker must not be empty")


def validate_positive_timeout(value: int | None, field: str) -> None:
    if value is not None and value <= 0:
        raise HTTPException(400, f"{field} must be positive")


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
        requested_worker=row["requested_worker"] if "requested_worker" in row.keys() else None,
        timeout_override_seconds=row["timeout_override_seconds"] if "timeout_override_seconds" in row.keys() else None,
        conclude_timeout_override_seconds=row["conclude_timeout_override_seconds"] if "conclude_timeout_override_seconds" in row.keys() else None,
        control_state=row["control_state"] if "control_state" in row.keys() else "normal",
        control_requested_at=row["control_requested_at"] if "control_requested_at" in row.keys() else None,
        control_requested_by=row["control_requested_by"] if "control_requested_by" in row.keys() else None,
        control_reason=row["control_reason"] if "control_reason" in row.keys() else None,
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def derive_fact_title(description: str, fact_id: str | None = None) -> str:
    normalized = " ".join(description.split()).strip()
    if not normalized:
        return fact_id or "Fact"
    chars = list(normalized)
    if len(chars) <= 24:
        return normalized
    return "".join(chars[:24]) + "..."


def fact_to_model(row: sqlite3.Row) -> Fact:
    title = row["title"] if "title" in row.keys() else None
    return Fact(
        id=row["id"],
        title=title or derive_fact_title(row["description"], row["id"]),
        description=row["description"],
        metadata=loads_json_object(row["metadata_json"] if "metadata_json" in row.keys() else None),
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
    if "reason_worker" not in row.keys() or row["reason_worker"] is None:
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
        auto_reason=bool(row["auto_reason"]) if "auto_reason" in row.keys() else False,
        allowed_auto_workers=loads_json_list(row["allowed_auto_workers_json"] if "allowed_auto_workers_json" in row.keys() else None),
        default_timeout_seconds=row["default_timeout_seconds"] if "default_timeout_seconds" in row.keys() else None,
        default_conclude_timeout_seconds=row["default_conclude_timeout_seconds"] if "default_conclude_timeout_seconds" in row.keys() else None,
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
        return f"/home/kali/workspace/.cairn/projects/{clean_project_id}"
    return None


def loads_json_object(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def loads_json_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    result: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            return None
        result.append(item)
    return result


def dumps_json(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def create_run_provenance(
    conn: sqlite3.Connection,
    *,
    run_log_id: str,
    project_id: str,
    intent_id: str | None = None,
    task_type: str,
    phase: str,
    worker_name: str,
    worker_type: str | None = None,
    environment_id: str | None = None,
    environment_backend: str | None = None,
    environment_target: str | None = None,
    workspace: str | None = None,
    model_profile_id: str | None = None,
    endpoint_id: str | None = None,
    timeout_seconds: int | None = None,
    report_path: str | None = None,
    report_run_id: str | None = None,
    remote_session: RemoteSessionProvenance | None = None,
    parent_run_log_id: str | None = None,
    parent_remote_session_id: str | None = None,
    question_mode: str | None = None,
    question_anchor_type: str | None = None,
    question_anchor_id: str | None = None,
    source_run_log_id: str | None = None,
    source_remote_session_id: str | None = None,
    session_effect: str | None = None,
    started_at: str | None = None,
    metadata: dict | None = None,
) -> RunProvenance:
    get_project_or_404(conn, project_id)
    body = RunProvenanceUpsert(
        run_log_id=run_log_id,
        intent_id=intent_id,
        task_type=task_type,
        phase=phase,
        worker_name=worker_name,
        worker_type=worker_type,
        environment_id=environment_id,
        environment_backend=environment_backend,
        environment_target=environment_target,
        workspace=workspace,
        model_profile_id=model_profile_id,
        endpoint_id=endpoint_id,
        timeout_seconds=timeout_seconds,
        report_path=report_path,
        report_run_id=report_run_id,
        remote_session=remote_session,
        parent_run_log_id=parent_run_log_id,
        parent_remote_session_id=parent_remote_session_id,
        question_mode=question_mode,
        question_anchor_type=question_anchor_type,
        question_anchor_id=question_anchor_id,
        source_run_log_id=source_run_log_id,
        source_remote_session_id=source_remote_session_id,
        session_effect=session_effect,
        started_at=started_at,
        metadata=metadata,
    )
    now = utcnow()
    session = body.remote_session or RemoteSessionProvenance()
    payload = {
        "run_log_id": body.run_log_id,
        "project_id": project_id,
        "intent_id": body.intent_id,
        "task_type": body.task_type,
        "phase": body.phase,
        "worker_name": body.worker_name,
        "worker_type": body.worker_type,
        "environment_id": body.environment_id,
        "environment_backend": body.environment_backend,
        "environment_target": body.environment_target,
        "workspace": body.workspace,
        "model_profile_id": body.model_profile_id,
        "endpoint_id": body.endpoint_id,
        "timeout_seconds": body.timeout_seconds,
        "report_path": body.report_path,
        "report_run_id": body.report_run_id,
        "remote_session_id": session.id,
        "remote_session_kind": session.kind,
        "remote_session_status": session.status,
        "remote_session_capture_method": session.capture_method,
        "parent_run_log_id": body.parent_run_log_id,
        "parent_remote_session_id": body.parent_remote_session_id,
        "question_mode": body.question_mode,
        "question_anchor_type": body.question_anchor_type,
        "question_anchor_id": body.question_anchor_id,
        "source_run_log_id": body.source_run_log_id,
        "source_remote_session_id": body.source_remote_session_id,
        "session_effect": body.session_effect,
        "started_at": body.started_at or now,
        "metadata_json": dumps_json(body.metadata),
        "created_at": now,
        "updated_at": now,
    }
    columns = tuple(payload)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column not in {"run_log_id", "created_at"}
    )
    conn.execute(
        f"""
        INSERT INTO run_provenance ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(run_log_id) DO UPDATE SET {updates}
        """,
        payload,
    )
    row = conn.execute(
        "SELECT * FROM run_provenance WHERE project_id = ? AND run_log_id = ?",
        (project_id, run_log_id),
    ).fetchone()
    assert row is not None
    return run_provenance_row_to_model(row)


def finish_run_provenance(
    conn: sqlite3.Connection,
    project_id: str,
    run_log_id: str,
    *,
    returncode: int,
    timed_out: bool,
    cancelled: bool,
    cancel_reason: str | None = None,
    finished_at: str | None = None,
) -> RunProvenance | None:
    now = utcnow()
    conn.execute(
        """
        UPDATE run_provenance
        SET finished_at = ?,
            returncode = ?,
            timed_out = ?,
            cancelled = ?,
            cancel_reason = ?,
            updated_at = ?
        WHERE project_id = ? AND run_log_id = ?
        """,
        (finished_at or now, returncode, int(timed_out), int(cancelled), cancel_reason, now, project_id, run_log_id),
    )
    return get_run_provenance_or_none(conn, project_id, run_log_id)


def update_run_remote_session(
    conn: sqlite3.Connection,
    project_id: str,
    run_log_id: str,
    *,
    remote_session_id: str | None,
    remote_session_kind: str | None,
    remote_session_status: str,
    remote_session_capture_method: str | None,
) -> RunProvenance | None:
    session = RemoteSessionProvenance(
        id=remote_session_id,
        kind=remote_session_kind,
        status=remote_session_status,
        capture_method=remote_session_capture_method,
    )
    now = utcnow()
    conn.execute(
        """
        UPDATE run_provenance
        SET remote_session_id = ?,
            remote_session_kind = ?,
            remote_session_status = ?,
            remote_session_capture_method = ?,
            updated_at = ?
        WHERE project_id = ? AND run_log_id = ?
        """,
        (session.id, session.kind, session.status, session.capture_method, now, project_id, run_log_id),
    )
    return get_run_provenance_or_none(conn, project_id, run_log_id)


def get_run_provenance_or_none(conn: sqlite3.Connection, project_id: str, run_log_id: str) -> RunProvenance | None:
    row = conn.execute(
        "SELECT * FROM run_provenance WHERE project_id = ? AND run_log_id = ?",
        (project_id, run_log_id),
    ).fetchone()
    return run_provenance_row_to_model(row) if row is not None else None


def list_run_provenance(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    intent_id: str | None = None,
    limit: int = 20,
    successful_only: bool = False,
) -> list[RunProvenance]:
    params: list[object] = [project_id]
    where = ["project_id = ?"]
    if intent_id is not None:
        where.append("intent_id = ?")
        params.append(intent_id)
    if successful_only:
        where.extend([
            "returncode = 0",
            "COALESCE(timed_out, 0) = 0",
            "COALESCE(cancelled, 0) = 0",
        ])
    params.append(max(1, min(limit, 1000)))
    rows = conn.execute(
        f"""
        SELECT *
        FROM run_provenance
        WHERE {" AND ".join(where)}
        ORDER BY COALESCE(finished_at, started_at) DESC, started_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [run_provenance_row_to_model(row) for row in rows]


def upsert_worker_runtime_health(conn: sqlite3.Connection, health: WorkerRuntimeHealth) -> WorkerRuntimeHealth:
    now = utcnow()
    checked_at = health.checked_at or now
    payload = {
        "environment_id": health.environment_id,
        "worker_name": health.worker_name,
        "worker_type": health.worker_type,
        "endpoint_id": _identity_text(health.endpoint_id),
        "model_profile_id": _identity_text(health.model_profile_id),
        "status": health.status,
        "checked_at": checked_at,
        "stale_after": health.stale_after,
        "disabled_until": health.disabled_until,
        "source": health.source,
        "dispatcher_id": health.dispatcher_id,
        "detail_json": dumps_json(_limit_health_detail(health.detail)),
        "created_at": now,
        "updated_at": now,
    }
    columns = tuple(payload)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column not in {"environment_id", "worker_name", "worker_type", "endpoint_id", "model_profile_id", "created_at"}
    )
    conn.execute(
        f"""
        INSERT INTO worker_runtime_health ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(environment_id, worker_name, worker_type, endpoint_id, model_profile_id)
        DO UPDATE SET {updates}
        """,
        payload,
    )
    row = conn.execute(
        """
        SELECT * FROM worker_runtime_health
        WHERE environment_id = ? AND worker_name = ? AND worker_type = ?
          AND endpoint_id = ? AND model_profile_id = ?
        """,
        (
            payload["environment_id"],
            payload["worker_name"],
            payload["worker_type"],
            payload["endpoint_id"],
            payload["model_profile_id"],
        ),
    ).fetchone()
    assert row is not None
    return worker_runtime_health_row_to_model(row)


def list_worker_runtime_health(conn: sqlite3.Connection, worker_name: str | None = None) -> list[WorkerRuntimeHealth]:
    if worker_name is None:
        rows = conn.execute("SELECT * FROM worker_runtime_health ORDER BY environment_id, worker_name").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM worker_runtime_health WHERE worker_name = ? ORDER BY environment_id, worker_type, endpoint_id, model_profile_id",
            (worker_name,),
        ).fetchall()
    return [worker_runtime_health_row_to_model(row) for row in rows]


def effective_worker_runtime_health(
    conn: sqlite3.Connection,
    health: WorkerRuntimeHealth,
    worker_row: sqlite3.Row | None = None,
) -> WorkerRuntimeHealth:
    detail = dict(health.detail or {})
    if _is_stale(health.stale_after):
        detail.setdefault("reason", "stale")
        return health.model_copy(update={"status": "unknown", "detail": detail})

    if worker_row is None:
        worker_row = conn.execute(
            "SELECT * FROM worker_inventory WHERE name = ?",
            (health.worker_name,),
        ).fetchone()
    if worker_row is None:
        detail.setdefault("reason", "worker_identity_mismatch")
        return health.model_copy(update={"status": "unknown", "detail": detail})

    if health.worker_type != worker_row["type"]:
        detail.setdefault("reason", "worker_identity_mismatch")
        return health.model_copy(update={"status": "unknown", "detail": detail})
    current_endpoint = worker_row["endpoint"] if "endpoint" in worker_row.keys() else None
    if _identity_text(health.endpoint_id) != _identity_text(current_endpoint):
        detail.setdefault("reason", "worker_identity_mismatch")
        return health.model_copy(update={"status": "unknown", "detail": detail})
    current_model_profile = worker_row["model_profile"] if "model_profile" in worker_row.keys() else None
    if _identity_text(health.model_profile_id) != _identity_text(current_model_profile):
        detail.setdefault("reason", "worker_identity_mismatch")
        return health.model_copy(update={"status": "unknown", "detail": detail})

    if health.worker_type != "mock" and health.endpoint_id:
        endpoint = conn.execute(
            """
            SELECT 1 FROM environment_provider_endpoints
            WHERE environment_id = ? AND endpoint_id = ? AND type = ?
            """,
            (health.environment_id, health.endpoint_id, health.worker_type),
        ).fetchone()
        if endpoint is None:
            detail.setdefault("reason", "worker_endpoint_unavailable")
            return health.model_copy(update={"status": "unknown", "detail": detail})

    return health


def worker_runtime_health_row_to_model(row: sqlite3.Row) -> WorkerRuntimeHealth:
    return WorkerRuntimeHealth(
        environment_id=row["environment_id"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        endpoint_id=_public_identity_text(row["endpoint_id"]),
        model_profile_id=_public_identity_text(row["model_profile_id"]),
        status=row["status"],
        checked_at=row["checked_at"],
        stale_after=row["stale_after"],
        disabled_until=row["disabled_until"],
        source=row["source"],
        dispatcher_id=row["dispatcher_id"],
        detail=loads_json_object(row["detail_json"]) or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def resolve_worker_question_availability(
    conn: sqlite3.Connection,
    provenance: RunProvenance,
    worker_name: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    modes = ["fresh_context"]
    reasons: dict[str, str] = {}
    session = provenance.remote_session
    if session.status != "available" or not session.id:
        reasons["resume"] = "source_session_missing"
        reasons["fork"] = "source_session_missing"
        return modes, reasons
    effective_worker_name = worker_name or provenance.worker_name
    if worker_name and provenance.worker_name and worker_name != provenance.worker_name:
        reasons["resume"] = "source_worker_mismatch"
        reasons["fork"] = "source_worker_mismatch"
        return modes, reasons
    row = conn.execute(
        "SELECT * FROM worker_inventory WHERE name = ?",
        (effective_worker_name,),
    ).fetchone() if effective_worker_name else None
    capability = loads_json_object(row["question_capability_json"]) if row is not None else None
    if not capability:
        reasons["resume"] = "worker_capability_stale"
        reasons["fork"] = "worker_capability_stale"
        return modes, reasons
    identity_reason = _source_identity_unavailable(conn, provenance, row)
    health_reason = _runtime_health_unavailable(conn, provenance, row)
    blocked_reason = identity_reason or health_reason
    declared_modes = capability.get("question_modes") or []
    unavailable = capability.get("unavailable_reasons") or {}
    if blocked_reason:
        reasons["resume"] = blocked_reason
        reasons["fork"] = blocked_reason
    if not blocked_reason and capability.get("can_resume_session") and "resume" in declared_modes:
        modes.insert(0, "resume")
    else:
        reasons.setdefault("resume", unavailable.get("resume") or "resume_unavailable")
    if not blocked_reason and capability.get("can_fork_session") and "fork" in declared_modes:
        modes.insert(0, "fork")
    else:
        reasons.setdefault("fork", unavailable.get("fork") or "fork_unavailable")
    return modes, reasons


def question_modes_from_inventory(
    conn: sqlite3.Connection,
    provenance: RunProvenance,
    worker_name: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    return resolve_worker_question_availability(conn, provenance, worker_name)


def resolve_anchor(
    conn: sqlite3.Connection,
    project_id: str,
    anchor_type: str,
    anchor_id: str,
    selected_run_log_id: str | None = None,
) -> AnchorResolution:
    if anchor_type not in {"fact", "intent", "run"}:
        raise HTTPException(400, "Invalid anchor type")
    get_project_or_404(conn, project_id)
    if anchor_type == "fact":
        return _resolve_fact_anchor(conn, project_id, anchor_id, selected_run_log_id)
    if anchor_type == "intent":
        return _resolve_intent_anchor(conn, project_id, anchor_id, selected_run_log_id)
    return _resolve_run_anchor(conn, project_id, anchor_id)


def _resolve_fact_anchor(
    conn: sqlite3.Connection,
    project_id: str,
    fact_id: str,
    selected_run_log_id: str | None,
) -> AnchorResolution:
    row = conn.execute(
        "SELECT * FROM facts WHERE id = ? AND project_id = ?",
        (fact_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Fact not found")
    metadata = loads_json_object(row["metadata_json"] if "metadata_json" in row.keys() else None) or {}
    provenance = metadata.get("provenance") if isinstance(metadata.get("provenance"), dict) else {}
    run_log_id = selected_run_log_id or _optional_text(provenance.get("producing_run_log_id"))
    if run_log_id:
        return _resolution_from_run(conn, project_id, "fact", fact_id, run_log_id, missing_reason="run_provenance_missing")
    if _optional_text(metadata.get("run_id")) or _optional_text(metadata.get("report_path")):
        return AnchorResolution(
            anchor_type="fact",
            anchor_id=fact_id,
            status="missing",
            reason="legacy_report_run_id_only",
            available_modes=["fresh_context"],
        )
    return AnchorResolution(
        anchor_type="fact",
        anchor_id=fact_id,
        status="missing",
        reason="producing_run_log_id_missing",
        available_modes=["fresh_context"],
    )


def _resolve_intent_anchor(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    selected_run_log_id: str | None,
) -> AnchorResolution:
    get_intent_or_404(conn, project_id, intent_id)
    if selected_run_log_id:
        return _resolution_from_run(conn, project_id, "intent", intent_id, selected_run_log_id, missing_reason="run_provenance_missing")
    runs = list_run_provenance(conn, project_id, intent_id=intent_id, limit=1, successful_only=True)
    if not runs:
        return AnchorResolution(
            anchor_type="intent",
            anchor_id=intent_id,
            status="missing",
            reason="successful_run_provenance_missing",
            available_modes=["fresh_context"],
        )
    return _resolution_from_provenance(conn, "intent", intent_id, runs[0])


def _resolve_run_anchor(conn: sqlite3.Connection, project_id: str, run_log_id: str) -> AnchorResolution:
    return _resolution_from_run(conn, project_id, "run", run_log_id, run_log_id, missing_reason="run_provenance_missing")


def _resolution_from_run(
    conn: sqlite3.Connection,
    project_id: str,
    anchor_type: str,
    anchor_id: str,
    run_log_id: str,
    *,
    missing_reason: str,
) -> AnchorResolution:
    provenance = get_run_provenance_or_none(conn, project_id, run_log_id)
    if provenance is None:
        return AnchorResolution(
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            source_run_log_id=run_log_id,
            status="missing",
            reason=missing_reason,
            available_modes=["fresh_context"],
        )
    return _resolution_from_provenance(conn, anchor_type, anchor_id, provenance)


def _resolution_from_provenance(
    conn: sqlite3.Connection,
    anchor_type: str,
    anchor_id: str,
    provenance: RunProvenance,
) -> AnchorResolution:
    modes, reasons = question_modes_from_inventory(conn, provenance)
    return AnchorResolution(
        anchor_type=anchor_type,
        anchor_id=anchor_id,
        source_run_log_id=provenance.run_log_id,
        status="exact",
        provenance=provenance,
        available_modes=modes,
        unavailable_reasons=reasons,
    )


def run_provenance_row_to_model(row: sqlite3.Row) -> RunProvenance:
    return RunProvenance(
        run_log_id=row["run_log_id"],
        project_id=row["project_id"],
        intent_id=row["intent_id"],
        task_type=row["task_type"],
        phase=row["phase"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        environment_id=row["environment_id"],
        environment_backend=row["environment_backend"],
        environment_target=row["environment_target"],
        workspace=row["workspace"],
        model_profile_id=row["model_profile_id"],
        endpoint_id=row["endpoint_id"],
        timeout_seconds=row["timeout_seconds"],
        report_path=row["report_path"],
        report_run_id=row["report_run_id"],
        remote_session=RemoteSessionProvenance(
            id=row["remote_session_id"],
            kind=row["remote_session_kind"],
            status=row["remote_session_status"],
            capture_method=row["remote_session_capture_method"],
        ),
        parent_run_log_id=row["parent_run_log_id"],
        parent_remote_session_id=row["parent_remote_session_id"],
        question_mode=row["question_mode"],
        question_anchor_type=row["question_anchor_type"],
        question_anchor_id=row["question_anchor_id"],
        source_run_log_id=row["source_run_log_id"],
        source_remote_session_id=row["source_remote_session_id"],
        session_effect=row["session_effect"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        returncode=row["returncode"],
        timed_out=_optional_bool_from_db(row["timed_out"]),
        cancelled=_optional_bool_from_db(row["cancelled"]),
        cancel_reason=row["cancel_reason"],
        metadata=loads_json_object(row["metadata_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _optional_bool_from_db(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity_text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _public_identity_text(value: str | None) -> str | None:
    text = _identity_text(value)
    return text or None


def _limit_health_detail(detail: dict | None) -> dict:
    if not isinstance(detail, dict):
        return {}
    limited: dict[str, object] = {}
    for key, value in detail.items():
        if isinstance(value, str):
            limited[key] = value[:1200]
        else:
            limited[key] = value
    return limited


def _source_identity_unavailable(conn: sqlite3.Connection, provenance: RunProvenance, worker_row: sqlite3.Row) -> str | None:
    current_type = worker_row["type"]
    if provenance.worker_type and current_type and provenance.worker_type != current_type:
        return "source_worker_identity_changed"
    current_endpoint = worker_row["endpoint"] if "endpoint" in worker_row.keys() else None
    if provenance.endpoint_id and current_endpoint and provenance.endpoint_id != current_endpoint:
        return "source_worker_identity_changed"
    current_model_profile = worker_row["model_profile"] if "model_profile" in worker_row.keys() else None
    if provenance.model_profile_id and current_model_profile and provenance.model_profile_id != current_model_profile:
        return "source_worker_identity_changed"
    worker_type = provenance.worker_type or current_type
    endpoint_id = provenance.endpoint_id or current_endpoint
    if worker_type != "mock" and provenance.environment_id:
        if not endpoint_id:
            return "worker_endpoint_unavailable"
        endpoint = conn.execute(
            """
            SELECT 1 FROM environment_provider_endpoints
            WHERE environment_id = ? AND endpoint_id = ? AND type = ?
            """,
            (provenance.environment_id, endpoint_id, worker_type),
        ).fetchone()
        if endpoint is None:
            return "worker_endpoint_unavailable"
    return None


def _runtime_health_unavailable(conn: sqlite3.Connection, provenance: RunProvenance, worker_row: sqlite3.Row) -> str | None:
    if not provenance.environment_id:
        return None
    worker_type = provenance.worker_type or worker_row["type"]
    endpoint_id = provenance.endpoint_id or worker_row["endpoint"]
    model_profile_id = provenance.model_profile_id or worker_row["model_profile"]
    row = conn.execute(
        """
        SELECT * FROM worker_runtime_health
        WHERE environment_id = ? AND worker_name = ? AND worker_type = ?
          AND endpoint_id = ? AND model_profile_id = ?
        """,
        (
            provenance.environment_id,
            provenance.worker_name,
            worker_type,
            _identity_text(endpoint_id),
            _identity_text(model_profile_id),
        ),
    ).fetchone()
    if row is None:
        return None
    if _is_stale(row["stale_after"]):
        return None
    if row["status"] == "unhealthy" and (row["disabled_until"] is None or _is_future(row["disabled_until"])):
        return "worker_environment_unhealthy"
    return None


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_future(value: str | None) -> bool:
    parsed = _parse_utc(value)
    return parsed is not None and parsed > datetime.now(timezone.utc)


def _is_stale(stale_after: str | None) -> bool:
    parsed = _parse_utc(stale_after)
    return parsed is not None and parsed <= datetime.now(timezone.utc)


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
