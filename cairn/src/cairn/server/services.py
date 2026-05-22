from __future__ import annotations

import sqlite3
import json
import hashlib
from pathlib import PurePosixPath
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from cairn.server.models import (
    AppendExecutionEventsRequest,
    Artifact,
    AgentContextSummary,
    AgentContextTemplate,
    AgentContextTemplateCreate,
    AgentContextTemplateUpdate,
    CreateExecutionRequest,
    ConcludeResponse,
    ExecutionEvent,
    ExecutionConclusionReportRequest,
    ExecutionRun,
    FinishExecutionRequest,
    Fact,
    Intent,
    LeaseExecutionRequest,
    PatchExecutionRequest,
    ProjectMeta,
    ProjectAgentContext,
    ProjectAgentContextUpsert,
    ProjectReason,
    ProviderEndpointPublic,
    ProviderEndpointSecret,
    ProviderEndpointUpsert,
    UploadExecutionArtifactRequest,
    WorkerRuntimeHealth,
    WorkEnvironmentPublic,
    WorkEnvironmentUpsert,
)
from cairn.shared.endpoints import normalize_provider_base_url

SYSTEM_HEALTHCHECK_PROJECT_ID = "__system_healthchecks__"
TERMINAL_EXECUTION_STATUSES = {"succeeded", "failed", "cancelled"}

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def next_agent_context_template_id(conn: sqlite3.Connection) -> str:
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('agent_context_template', 0)")
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'agent_context_template'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'agent_context_template'").fetchone()
    return f"act_{row['value']:03d}"


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


def next_execution_id(conn: sqlite3.Connection, project_id: str) -> str:
    return f"{project_id}_{_next_scoped_id(conn, 'execution', 'ex', project_id)}"


def next_branch_id(conn: sqlite3.Connection, project_id: str) -> str:
    return f"{project_id}_{_next_scoped_id(conn, 'branch', 'br', project_id)}"


def next_artifact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return f"{project_id}_{_next_scoped_id(conn, 'artifact', 'art', project_id)}"


def next_evidence_link_id(conn: sqlite3.Connection, project_id: str) -> str:
    return f"{project_id}_{_next_scoped_id(conn, 'evidence_link', 'evl', project_id)}"


def _next_scoped_value(conn: sqlite3.Connection, project_id: str, kind: str) -> int:
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
    return int(row["value"])


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


def _row_has(row: sqlite3.Row, column: str) -> bool:
    return column in row.keys()


def _intent_result_fact_id(row: sqlite3.Row) -> str | None:
    if _row_has(row, "concluded_fact_id"):
        return row["concluded_fact_id"]
    if _row_has(row, "to_fact_id"):
        return row["to_fact_id"]
    return None


def _latest_intent_execution(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    *,
    active_only: bool = False,
) -> sqlite3.Row | None:
    status_filter = "AND status IN ('pending', 'leased', 'running')" if active_only else ""
    return conn.execute(
        f"""
        SELECT *
        FROM execution_runs
        WHERE project_id = ?
          AND intent_id = ?
          {status_filter}
        ORDER BY
          CASE status WHEN 'running' THEN 0 WHEN 'leased' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,
          COALESCE(updated_at, created_at) DESC,
          created_at DESC
        LIMIT 1
        """,
        (project_id, intent_id),
    ).fetchone()


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if _intent_result_fact_id(row) is not None:
        raise HTTPException(409, "Intent already concluded")
    active = _latest_intent_execution(conn, project_id, intent_id, active_only=True)
    active_worker = active["worker_name"] if active is not None else None
    if active_worker is not None and active_worker != worker:
        raise HTTPException(409, f"Intent is currently claimed by {active_worker}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if _intent_result_fact_id(row) is not None:
        raise HTTPException(409, "Intent already concluded")
    active = _latest_intent_execution(conn, project_id, intent_id, active_only=True)
    active_worker = active["worker_name"] if active is not None else None
    if active_worker is None:
        return row
    if active_worker != worker:
        raise HTTPException(409, f"Intent is currently claimed by {active_worker}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND concluded_fact_id = 'goal'",
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
    active = _latest_intent_execution(conn, project_id, row["id"], active_only=True)
    latest = active or _latest_intent_execution(conn, project_id, row["id"])
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=_intent_result_fact_id(row),
        description=row["description"],
        creator=row["creator"],
        worker=latest["worker_name"] if latest is not None else (row["worker"] if _row_has(row, "worker") else None),
        requested_worker=row["requested_worker"] if _row_has(row, "requested_worker") else None,
        timeout_override_seconds=row["timeout_override_seconds"] if _row_has(row, "timeout_override_seconds") else None,
        conclude_timeout_override_seconds=row["conclude_timeout_override_seconds"] if _row_has(row, "conclude_timeout_override_seconds") else None,
        control_state=latest["control_state"] if latest is not None else (row["control_state"] if _row_has(row, "control_state") else "normal"),
        control_requested_at=latest["control_requested_at"] if latest is not None else (row["control_requested_at"] if _row_has(row, "control_requested_at") else None),
        control_requested_by=None,
        control_reason=latest["control_reason"] if latest is not None else (row["control_reason"] if _row_has(row, "control_reason") else None),
        last_heartbeat_at=latest["last_heartbeat_at"] if latest is not None else (row["last_heartbeat_at"] if _row_has(row, "last_heartbeat_at") else None),
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


def project_reason_from_row(row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> ProjectReason | None:
    if conn is not None:
        execution = conn.execute(
            """
            SELECT *
            FROM execution_runs
            WHERE project_id = ?
              AND task_type = 'reason'
              AND status IN ('pending', 'leased', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        if execution is not None and execution["worker_name"] is not None:
            return ProjectReason(
                worker=execution["worker_name"],
                trigger=(loads_json_object(execution["metadata_json"]) or {}).get("trigger") or execution["phase"],
                started_at=execution["started_at"] or execution["leased_at"] or execution["created_at"],
                last_heartbeat_at=execution["last_heartbeat_at"] or execution["updated_at"],
            )
    if "reason_worker" not in row.keys() or row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row, environment: WorkEnvironmentPublic | None = None, conn: sqlite3.Connection | None = None) -> ProjectMeta:
    agent_context_summary = get_project_agent_context_summary(conn, row["id"]) if conn is not None else None
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        reason=project_reason_from_row(row, conn),
        environment_id=row["environment_id"] if "environment_id" in row.keys() else None,
        environment=environment,
        planned_workspace=planned_workspace_for(row["id"], environment),
        auto_reason=bool(row["auto_reason"]) if "auto_reason" in row.keys() else False,
        allowed_auto_workers=loads_json_list(row["allowed_auto_workers_json"] if "allowed_auto_workers_json" in row.keys() else None),
        default_timeout_seconds=row["default_timeout_seconds"] if "default_timeout_seconds" in row.keys() else None,
        default_conclude_timeout_seconds=row["default_conclude_timeout_seconds"] if "default_conclude_timeout_seconds" in row.keys() else None,
        agent_context_summary=agent_context_summary,
    )


def compute_agent_context_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def agent_context_template_from_row(row: sqlite3.Row) -> AgentContextTemplate:
    return AgentContextTemplate(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        kind=row["kind"],
        content=row["content"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def project_agent_context_from_row(row: sqlite3.Row) -> ProjectAgentContext:
    return ProjectAgentContext(
        project_id=row["project_id"],
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        source_template_id=row["source_template_id"],
        source_template_hash=row["source_template_hash"],
        content=row["content"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def agent_context_summary_from_row(row: sqlite3.Row) -> AgentContextSummary:
    return AgentContextSummary(
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        source_template_id=row["source_template_id"],
        source_template_hash=row["source_template_hash"],
        content_hash=row["content_hash"],
        updated_at=row["updated_at"],
    )


def list_agent_context_templates(conn: sqlite3.Connection) -> list[AgentContextTemplate]:
    rows = conn.execute("SELECT * FROM agent_context_templates ORDER BY updated_at DESC, id").fetchall()
    return [agent_context_template_from_row(row) for row in rows]


def get_agent_context_template_or_404(conn: sqlite3.Connection, template_id: str) -> AgentContextTemplate:
    row = conn.execute("SELECT * FROM agent_context_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Agent context template not found")
    return agent_context_template_from_row(row)


def create_agent_context_template(conn: sqlite3.Connection, body: AgentContextTemplateCreate) -> AgentContextTemplate:
    now = utcnow()
    template_id = next_agent_context_template_id(conn)
    content_hash = compute_agent_context_hash(body.content)
    conn.execute(
        """
        INSERT INTO agent_context_templates (id, name, description, kind, content, content_hash, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (template_id, body.name, body.description, body.kind, body.content, content_hash, now, now),
    )
    return get_agent_context_template_or_404(conn, template_id)


def update_agent_context_template(conn: sqlite3.Connection, template_id: str, body: AgentContextTemplateUpdate) -> AgentContextTemplate:
    current = get_agent_context_template_or_404(conn, template_id)
    fields = body.model_fields_set
    updates: list[str] = []
    params: list[object] = []
    if "name" in fields:
        updates.append("name = ?")
        params.append(body.name)
    if "description" in fields:
        updates.append("description = ?")
        params.append(body.description)
    if "content" in fields:
        content = body.content if body.content is not None else current.content
        updates.append("content = ?")
        params.append(content)
        updates.append("content_hash = ?")
        params.append(compute_agent_context_hash(content))
    if updates:
        updates.append("updated_at = ?")
        params.append(utcnow())
        params.append(template_id)
        conn.execute(f"UPDATE agent_context_templates SET {', '.join(updates)} WHERE id = ?", tuple(params))
    return get_agent_context_template_or_404(conn, template_id)


def delete_agent_context_template(conn: sqlite3.Connection, template_id: str) -> None:
    get_agent_context_template_or_404(conn, template_id)
    conn.execute("DELETE FROM agent_context_templates WHERE id = ?", (template_id,))


def get_project_agent_context(conn: sqlite3.Connection, project_id: str, *, kind: str = "agents_md") -> ProjectAgentContext | None:
    get_project_or_404(conn, project_id)
    row = conn.execute(
        "SELECT * FROM project_agent_contexts WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    return project_agent_context_from_row(row) if row is not None else None


def get_project_agent_context_summary(conn: sqlite3.Connection | None, project_id: str) -> AgentContextSummary | None:
    if conn is None:
        return None
    row = conn.execute(
        "SELECT * FROM project_agent_contexts WHERE project_id = ? AND kind = 'agents_md'",
        (project_id,),
    ).fetchone()
    return agent_context_summary_from_row(row) if row is not None else None


def upsert_project_agent_context(conn: sqlite3.Connection, project_id: str, body: ProjectAgentContextUpsert) -> ProjectAgentContext:
    get_project_or_404(conn, project_id)
    template = None
    if body.template_id:
        template = get_agent_context_template_or_404(conn, body.template_id)
    content = body.content
    if content is None and template is not None:
        content = template.content
    content = content or ""
    if body.enabled and not content.strip():
        raise HTTPException(400, "Enabled agent context requires non-empty content")
    if len(content.encode("utf-8")) > 128 * 1024:
        raise HTTPException(400, "Agent context content exceeds 128 KiB")
    now = utcnow()
    content_hash = compute_agent_context_hash(content)
    source_template_id = template.id if template is not None else None
    source_template_hash = template.content_hash if template is not None else None
    existing = conn.execute(
        "SELECT * FROM project_agent_contexts WHERE project_id = ? AND kind = ?",
        (project_id, body.kind),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO project_agent_contexts (
                project_id, kind, enabled, source_template_id, source_template_hash,
                content, content_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                body.kind,
                1 if body.enabled else 0,
                source_template_id,
                source_template_hash,
                content,
                content_hash,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE project_agent_contexts
            SET enabled = ?,
                source_template_id = ?,
                source_template_hash = ?,
                content = ?,
                content_hash = ?,
                updated_at = ?
            WHERE project_id = ? AND kind = ?
            """,
            (
                1 if body.enabled else 0,
                source_template_id,
                source_template_hash,
                content,
                content_hash,
                now,
                project_id,
                body.kind,
            ),
        )
    result = get_project_agent_context(conn, project_id, kind=body.kind)
    assert result is not None
    return result


def environment_row_to_public(
    row: sqlite3.Row,
    *,
    conn: sqlite3.Connection | None = None,
    include_secret: bool = False,
) -> WorkEnvironmentPublic:
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
        last_health_status="untested",
        last_healthcheck=None,
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
    base_url = normalize_provider_base_url(
        endpoint_type=body.type,
        base_url=body.base_url,
        provider_api=body.provider_api,
    )
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
                base_url,
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
                base_url,
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


def execution_run_row_to_model(row: sqlite3.Row) -> ExecutionRun:
    return ExecutionRun(
        id=row["id"],
        project_id=row["project_id"],
        intent_id=row["intent_id"],
        branch_id=row["branch_id"],
        parent_execution_id=row["parent_execution_id"],
        task_type=row["task_type"],
        phase=row["phase"],
        session_action=row["session_action"],
        worker_name=row["worker_name"],
        worker_type=row["worker_type"],
        environment_id=row["environment_id"],
        endpoint_id=row["endpoint_id"],
        model_profile_id=row["model_profile_id"],
        workspace=row["workspace"],
        status=row["status"],
        leased_by=row["leased_by"],
        leased_at=row["leased_at"],
        lease_expires_at=row["lease_expires_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        control_state=row["control_state"],
        control_requested_at=row["control_requested_at"],
        control_reason=row["control_reason"],
        remote_session_in_kind=row["remote_session_in_kind"],
        remote_session_in_id=row["remote_session_in_id"],
        remote_session_in_status=row["remote_session_in_status"],
        remote_session_out_kind=row["remote_session_out_kind"],
        remote_session_out_id=row["remote_session_out_id"],
        remote_session_out_status=row["remote_session_out_status"],
        input_snapshot=loads_json_object(row["input_snapshot_json"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        returncode=row["returncode"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        metadata=loads_json_object(row["metadata_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def execution_event_row_to_model(row: sqlite3.Row) -> ExecutionEvent:
    payload = loads_json_object(row["payload_json"]) or {}
    return ExecutionEvent(
        id=row["id"],
        execution_id=row["execution_id"],
        project_id=row["project_id"],
        seq=row["seq"],
        project_seq=row["project_seq"],
        cursor=row["cursor"],
        ts=row["ts"],
        event_type=row["event_type"],
        role=row["role"],
        payload=payload,
        event_key=row["event_key"],
        created_at=row["created_at"],
    )


def artifact_row_to_model(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        project_id=row["project_id"],
        produced_by_execution_id=row["produced_by_execution_id"],
        type=row["type"],
        uri=row["uri"],
        path=row["path"],
        content_hash=row["content_hash"],
        summary=row["summary"],
        metadata=loads_json_object(row["metadata_json"]),
        created_at=row["created_at"],
    )


def create_execution_run(conn: sqlite3.Connection, project_id: str, body: CreateExecutionRequest) -> ExecutionRun:
    get_project_or_404(conn, project_id)
    _validate_execution_owner(body)
    if body.task_type == "reason":
        _reject_active_project_execution(conn, project_id, "reason", body.phase)
    if body.task_type == "healthcheck":
        _reject_active_project_execution(conn, project_id, "healthcheck", body.phase)
    if body.intent_id is not None:
        get_intent_or_404(conn, project_id, body.intent_id)
    if body.branch_id is not None:
        get_branch_or_404(conn, project_id, body.branch_id)
    now = utcnow()
    execution_id = next_execution_id(conn, project_id)
    conn.execute(
        """
        INSERT INTO execution_runs (
            id, project_id, intent_id, branch_id, parent_execution_id,
            task_type, phase, session_action, status,
            remote_session_in_kind, remote_session_in_id, remote_session_in_status,
            input_snapshot_json, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            execution_id,
            project_id,
            body.intent_id,
            body.branch_id,
            body.parent_execution_id,
            body.task_type,
            body.phase,
            body.session_action,
            body.remote_session_in_kind,
            body.remote_session_in_id,
            body.remote_session_in_status,
            dumps_json(body.input_snapshot),
            dumps_json(body.metadata),
            now,
            now,
        ),
    )
    return get_execution_or_404(conn, project_id, execution_id)


def ensure_system_healthcheck_project(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM projects WHERE id = ?", (SYSTEM_HEALTHCHECK_PROJECT_ID,)).fetchone()
    if row is not None:
        return SYSTEM_HEALTHCHECK_PROJECT_ID
    conn.execute(
        """
        INSERT INTO projects (
            id, title, status, created_at, auto_reason, allowed_auto_workers_json,
            default_timeout_seconds, default_conclude_timeout_seconds, environment_id
        ) VALUES (?, ?, 'stopped', ?, 0, NULL, NULL, NULL, NULL)
        """,
        (
            SYSTEM_HEALTHCHECK_PROJECT_ID,
            "System healthchecks",
            utcnow(),
        ),
    )
    return SYSTEM_HEALTHCHECK_PROJECT_ID


def create_environment_healthcheck_requests(conn: sqlite3.Connection, environment_id: str) -> dict:
    environment = get_environment_or_404(conn, environment_id)
    project_id = ensure_system_healthcheck_project(conn)
    _expire_stale_healthcheck_executions(conn)
    workers = _eligible_healthcheck_workers(conn, environment_id)
    now = utcnow()
    executions: list[ExecutionRun] = []
    for worker in workers:
        active = conn.execute(
            """
            SELECT *
            FROM execution_runs
            WHERE project_id = ?
              AND task_type = 'healthcheck'
              AND phase = 'healthcheck'
              AND environment_id = ?
              AND worker_name = ?
              AND status IN ('pending', 'leased', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id, environment_id, worker["name"]),
        ).fetchone()
        if active is not None:
            executions.append(execution_run_row_to_model(active))
            continue
        execution_id = next_execution_id(conn, project_id)
        metadata = {
            "environment_id": environment_id,
            "worker_names": [worker["name"]],
            "requested_by": "environment_healthcheck",
        }
        conn.execute(
            """
            INSERT INTO execution_runs (
                id, project_id, task_type, phase, worker_name, worker_type,
                environment_id, endpoint_id, model_profile_id, status,
                input_snapshot_json, metadata_json, created_at, updated_at
            ) VALUES (?, ?, 'healthcheck', 'healthcheck', ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                execution_id,
                project_id,
                worker["name"],
                worker["type"],
                environment_id,
                worker["endpoint"],
                worker["model_profile"],
                dumps_json({"environment_id": environment_id, "worker_name": worker["name"]}),
                dumps_json(metadata),
                now,
                now,
            ),
        )
        executions.append(get_execution_or_404(conn, project_id, execution_id))
    snapshot = _healthcheck_request_snapshot(environment, executions)
    conn.execute(
        "UPDATE work_environments SET last_health_status = ?, last_healthcheck_json = ?, updated_at = ? WHERE id = ?",
        (snapshot["status"], dumps_json(snapshot), now, environment_id),
    )
    return snapshot


def claim_pending_healthcheck_executions(
    conn: sqlite3.Connection,
    *,
    dispatcher_id: str,
    worker_names: list[str],
    environment_ids: list[str],
    limit: int,
    lease_seconds: int = 60,
) -> list[ExecutionRun]:
    _expire_stale_healthcheck_executions(conn)
    if not worker_names or not environment_ids or limit <= 0:
        return []
    worker_placeholders = ",".join("?" for _ in worker_names)
    environment_placeholders = ",".join("?" for _ in environment_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM execution_runs
        WHERE task_type = 'healthcheck'
          AND phase = 'healthcheck'
          AND status = 'pending'
          AND worker_name IN ({worker_placeholders})
          AND environment_id IN ({environment_placeholders})
        ORDER BY created_at, id
        LIMIT ?
        """,
        (*worker_names, *environment_ids, limit),
    ).fetchall()
    now = utcnow()
    lease_expires_at = _add_seconds(now, lease_seconds)
    claimed: list[ExecutionRun] = []
    for row in rows:
        conn.execute(
            """
            UPDATE execution_runs
            SET status = 'leased',
                leased_by = ?,
                leased_at = ?,
                lease_expires_at = ?,
                last_heartbeat_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (dispatcher_id, now, lease_expires_at, now, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (row["id"],)).fetchone()
        if updated is not None and updated["status"] == "leased":
            claimed.append(execution_run_row_to_model(updated))
    return claimed


def claim_pending_question_executions(
    conn: sqlite3.Connection,
    *,
    dispatcher_id: str,
    worker_names: list[str],
    environment_ids: list[str],
    limit: int,
    lease_seconds: int = 60,
) -> list[ExecutionRun]:
    if not worker_names or not environment_ids or limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM execution_runs
        WHERE task_type = 'question'
          AND phase = 'followup'
          AND status = 'pending'
        ORDER BY created_at, id
        LIMIT ?
        """,
        (limit * 4,),
    ).fetchall()
    now = utcnow()
    lease_expires_at = _add_seconds(now, lease_seconds)
    claimed: list[ExecutionRun] = []
    for row in rows:
        identity = _question_execution_claim_identity(conn, row, worker_names, environment_ids)
        if identity is None:
            continue
        conn.execute(
            """
            UPDATE execution_runs
            SET status = 'leased',
                leased_by = ?,
                leased_at = ?,
                lease_expires_at = ?,
                last_heartbeat_at = ?,
                worker_name = ?,
                worker_type = ?,
                environment_id = ?,
                endpoint_id = ?,
                model_profile_id = ?,
                workspace = ?,
                updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                dispatcher_id,
                now,
                lease_expires_at,
                now,
                identity["worker_name"],
                identity["worker_type"],
                identity["environment_id"],
                identity["endpoint_id"],
                identity["model_profile_id"],
                identity["workspace"],
                now,
                row["id"],
            ),
        )
        updated = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (row["id"],)).fetchone()
        if updated is not None and updated["status"] == "leased":
            claimed.append(execution_run_row_to_model(updated))
            if len(claimed) >= limit:
                break
    return claimed


def _question_execution_claim_identity(
    conn: sqlite3.Connection,
    execution: sqlite3.Row,
    worker_names: list[str],
    environment_ids: list[str],
) -> dict[str, str | None] | None:
    branch = conn.execute(
        "SELECT * FROM branches WHERE project_id = ? AND id = ?",
        (execution["project_id"], execution["branch_id"]),
    ).fetchone()
    if branch is None:
        return None
    source = _question_execution_source_for_identity(conn, execution, branch)
    project = get_project_or_404(conn, execution["project_id"])
    if source is not None:
        worker_name = source["worker_name"]
        if worker_name not in worker_names:
            return None
        environment_id = source["environment_id"] or project["environment_id"] or environment_ids[0]
        if environment_id not in environment_ids:
            return None
        environment = get_environment_or_404(conn, environment_id) if environment_id else None
        return {
            "worker_name": worker_name,
            "worker_type": source["worker_type"],
            "environment_id": environment_id,
            "endpoint_id": source["endpoint_id"],
            "model_profile_id": source["model_profile_id"],
            "workspace": source["workspace"] or planned_workspace_for(execution["project_id"], environment),
        }
    worker_name = worker_names[0]
    worker = conn.execute("SELECT * FROM worker_inventory WHERE name = ?", (worker_name,)).fetchone()
    environment_id = project["environment_id"] or environment_ids[0]
    if environment_id not in environment_ids:
        return None
    environment = get_environment_or_404(conn, environment_id) if environment_id else None
    return {
        "worker_name": worker_name,
        "worker_type": worker["type"] if worker is not None else None,
        "environment_id": environment_id,
        "endpoint_id": worker["endpoint"] if worker is not None else None,
        "model_profile_id": worker["model_profile"] if worker is not None else None,
        "workspace": planned_workspace_for(execution["project_id"], environment),
    }


def _question_execution_source_for_identity(
    conn: sqlite3.Connection,
    execution: sqlite3.Row,
    branch: sqlite3.Row,
) -> sqlite3.Row | None:
    if execution["session_action"] == "fresh_context":
        return None
    latest = conn.execute(
        """
        SELECT *
        FROM execution_runs
        WHERE project_id = ?
          AND branch_id = ?
          AND status = 'succeeded'
          AND remote_session_out_status = 'available'
        ORDER BY finished_at DESC, created_at DESC
        LIMIT 1
        """,
        (execution["project_id"], execution["branch_id"]),
    ).fetchone()
    if latest is not None and latest["worker_name"] is not None:
        return latest
    if branch["source_execution_id"] is None:
        return None
    return conn.execute(
        "SELECT * FROM execution_runs WHERE project_id = ? AND id = ?",
        (execution["project_id"], branch["source_execution_id"]),
    ).fetchone()


def _expire_stale_healthcheck_executions(conn: sqlite3.Connection) -> None:
    now = utcnow()
    conn.execute(
        """
        UPDATE execution_runs
        SET status = 'failed',
            finished_at = COALESCE(finished_at, ?),
            error_code = COALESCE(error_code, 'healthcheck_lease_expired'),
            error_detail = COALESCE(error_detail, 'dispatcher lease expired before healthcheck completed'),
            updated_at = ?
        WHERE task_type = 'healthcheck'
          AND phase = 'healthcheck'
          AND status IN ('leased', 'running')
          AND lease_expires_at IS NOT NULL
          AND julianday(lease_expires_at) < julianday(?)
        """,
        (now, now, now),
    )


def _eligible_healthcheck_workers(conn: sqlite3.Connection, environment_id: str) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM worker_inventory ORDER BY priority, name").fetchall()
    eligible: list[sqlite3.Row] = []
    for row in rows:
        allowed = loads_json_list(row["allowed_environments_json"])
        if allowed is not None and environment_id not in allowed:
            continue
        if row["type"] != "mock":
            endpoint = row["endpoint"]
            if not endpoint:
                continue
            endpoint_row = conn.execute(
                """
                SELECT 1
                FROM environment_provider_endpoints
                WHERE environment_id = ? AND endpoint_id = ? AND type = ?
                """,
                (environment_id, endpoint, row["type"]),
            ).fetchone()
            if endpoint_row is None:
                continue
        eligible.append(row)
    return eligible


def _healthcheck_request_snapshot(environment: WorkEnvironmentPublic, executions: list[ExecutionRun]) -> dict:
    checks = []
    for execution in executions:
        checks.append(
            {
                "name": f"worker:{execution.worker_name}",
                "worker_name": execution.worker_name,
                "execution_id": execution.id,
                "project_id": execution.project_id,
                "status": "queued" if execution.status == "pending" else execution.status,
                "duration_ms": 0,
                "command": "-",
                "stdout": "",
                "stderr": "",
            }
        )
    status = "queued" if checks else "no_workers"
    return {
        "environment_id": environment.id,
        "backend": environment.backend,
        "status": status,
        "project_id": SYSTEM_HEALTHCHECK_PROJECT_ID,
        "executions": [execution.model_dump() for execution in executions],
        "checks": checks,
    }


def _validate_execution_owner(body: CreateExecutionRequest) -> None:
    if body.task_type == "conclude" and body.intent_id is None:
        raise HTTPException(400, "conclude execution requires intent_id")
    if body.task_type == "question" and body.branch_id is None:
        raise HTTPException(400, "question execution requires branch_id")
    if body.task_type == "explore" and body.phase != "bootstrap" and body.intent_id is None:
        raise HTTPException(400, "explore execution requires intent_id outside bootstrap")
    if body.task_type in {"reason", "healthcheck"}:
        return
    if body.task_type not in {"explore", "conclude", "question"} and body.intent_id is None and body.branch_id is None:
        raise HTTPException(400, "execution requires intent_id or branch_id")


def get_execution_or_404(conn: sqlite3.Connection, project_id: str | None, execution_id: str) -> ExecutionRun:
    if project_id is None:
        row = conn.execute("SELECT * FROM execution_runs WHERE id = ?", (execution_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM execution_runs WHERE project_id = ? AND id = ?",
            (project_id, execution_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Execution not found")
    return execution_run_row_to_model(row)


def get_branch_or_404(conn: sqlite3.Connection, project_id: str, branch_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM branches WHERE project_id = ? AND id = ?",
        (project_id, branch_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Branch not found")
    return row


def lease_execution(conn: sqlite3.Connection, body: LeaseExecutionRequest, *, intent_id: str | None = None) -> ExecutionRun:
    now = utcnow()
    lease_expires_at = _add_seconds(now, body.lease_seconds)
    if intent_id is not None:
        if body.project_id is None:
            raise HTTPException(400, "project_id is required")
        get_intent_or_404(conn, body.project_id, intent_id)
        task_type = body.task_type or "explore"
        phase = body.phase or "run"
        if not body.allow_parallel:
            _reject_active_intent_execution(conn, body.project_id, intent_id, task_type, phase)
        execution = create_execution_run(
            conn,
            body.project_id,
            CreateExecutionRequest(intent_id=intent_id, task_type=task_type, phase=phase),
        )
    else:
        if body.execution_id is None:
            raise HTTPException(400, "execution_id is required")
        execution = get_execution_or_404(conn, body.project_id, body.execution_id)
        if execution.status != "pending":
            raise HTTPException(409, f"Execution is {execution.status}")
    conn.execute(
        """
        UPDATE execution_runs
        SET status = 'leased',
            leased_by = ?,
            leased_at = ?,
            lease_expires_at = ?,
            last_heartbeat_at = ?,
            worker_name = ?,
            worker_type = ?,
            environment_id = ?,
            endpoint_id = ?,
            model_profile_id = ?,
            workspace = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            body.dispatcher_id,
            now,
            lease_expires_at,
            now,
            body.worker_name,
            body.worker_type,
            body.environment_id,
            body.endpoint_id,
            body.model_profile_id,
            body.workspace,
            now,
            execution.id,
        ),
    )
    leased = get_execution_or_404(conn, None, execution.id)
    return leased


def _reject_active_intent_execution(
    conn: sqlite3.Connection,
    project_id: str,
    intent_id: str,
    task_type: str,
    phase: str,
) -> None:
    row = conn.execute(
        """
        SELECT id FROM execution_runs
        WHERE project_id = ?
          AND intent_id = ?
          AND task_type = ?
          AND phase = ?
          AND status IN ('pending', 'leased', 'running')
        LIMIT 1
        """,
        (project_id, intent_id, task_type, phase),
    ).fetchone()
    if row is not None:
        raise HTTPException(409, "Intent already has an active execution for this task and phase")


def _reject_active_project_execution(
    conn: sqlite3.Connection,
    project_id: str,
    task_type: str,
    phase: str,
) -> None:
    row = conn.execute(
        """
        SELECT id FROM execution_runs
        WHERE project_id = ?
          AND task_type = ?
          AND phase = ?
          AND status IN ('pending', 'leased', 'running')
        LIMIT 1
        """,
        (project_id, task_type, phase),
    ).fetchone()
    if row is not None:
        raise HTTPException(409, f"Project already has an active {task_type} execution")


def patch_execution(conn: sqlite3.Connection, execution_id: str, body: PatchExecutionRequest) -> ExecutionRun:
    current = get_execution_or_404(conn, None, execution_id)
    fields = body.model_fields_set
    if current.status in TERMINAL_EXECUTION_STATUSES:
        allowed_terminal_followup = {
            "dispatcher_id",
            "remote_session_out_kind",
            "remote_session_out_id",
            "remote_session_out_status",
            "metadata",
        }
        if "status" in fields and body.status != current.status:
            raise HTTPException(409, "Execution is already terminal")
        disallowed = fields - allowed_terminal_followup - {"status"}
        if disallowed:
            raise HTTPException(409, "Execution is already terminal")
    updates: list[str] = []
    params: list[object] = []
    if "status" in fields:
        updates.append("status = ?")
        params.append(body.status)
        if body.status == "running" and current.started_at is None and "started_at" not in fields:
            updates.append("started_at = ?")
            params.append(utcnow())
        if body.status in {"succeeded", "failed", "cancelled"} and "finished_at" not in fields:
            updates.append("finished_at = ?")
            params.append(utcnow())
    if "last_heartbeat_at" in fields or body.lease_seconds is not None:
        heartbeat_at = body.last_heartbeat_at or utcnow()
        updates.append("last_heartbeat_at = ?")
        params.append(heartbeat_at)
        if body.lease_seconds is not None:
            updates.append("lease_expires_at = ?")
            params.append(_add_seconds(heartbeat_at, body.lease_seconds))
    for model_field, column in (
        ("control_state", "control_state"),
        ("control_reason", "control_reason"),
        ("remote_session_out_kind", "remote_session_out_kind"),
        ("remote_session_out_id", "remote_session_out_id"),
        ("remote_session_out_status", "remote_session_out_status"),
        ("started_at", "started_at"),
        ("finished_at", "finished_at"),
        ("returncode", "returncode"),
        ("error_code", "error_code"),
        ("error_detail", "error_detail"),
    ):
        if model_field in fields:
            updates.append(f"{column} = ?")
            params.append(getattr(body, model_field))
    if "metadata" in fields:
        updates.append("metadata_json = ?")
        merged_metadata = {**(current.metadata or {}), **(body.metadata or {})}
        params.append(dumps_json(merged_metadata))
    if not updates:
        return current
    updates.append("updated_at = ?")
    params.append(utcnow())
    params.append(execution_id)
    conn.execute(
        f"UPDATE execution_runs SET {', '.join(updates)} WHERE id = ?",
        tuple(params),
    )
    updated = get_execution_or_404(conn, None, execution_id)
    if updated.status in {"succeeded", "failed", "cancelled"}:
        release_execution_session_lock(conn, updated.id)
    return updated


def submit_execution_conclusion_report(
    conn: sqlite3.Connection,
    execution_id: str,
    body: ExecutionConclusionReportRequest,
) -> ConcludeResponse:
    execution = get_execution_or_404(conn, None, execution_id)
    if execution.intent_id is None:
        raise HTTPException(400, "Conclusion report requires an intent execution")
    intent = get_intent_or_404(conn, execution.project_id, execution.intent_id)
    existing_fact_id = _intent_result_fact_id(intent)
    if existing_fact_id is not None:
        raise HTTPException(409, "Intent already has a primary result fact")
    now = utcnow()
    fact_id = next_fact_id(conn, execution.project_id)
    title = body.title or derive_fact_title(body.description, fact_id)
    metadata = body.metadata or {}
    if body.confidence is not None:
        metadata = {**metadata, "confidence": body.confidence}
    conn.execute(
        """
        INSERT INTO facts (
            id, project_id, kind, status, title, description, metadata_json,
            produced_by_execution_id, produced_by_intent_id, created_at, updated_at
        ) VALUES (?, ?, 'fact', 'active', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id,
            execution.project_id,
            title,
            body.description,
            dumps_json(metadata),
            execution.id,
            execution.intent_id,
            now,
            now,
        ),
    )
    conn.execute(
        """
        UPDATE intents
        SET concluded_fact_id = ?,
            concluded_at = ?
        WHERE id = ? AND project_id = ?
        """,
        (fact_id, now, execution.intent_id, execution.project_id),
    )
    for artifact_id in body.artifact_ids:
        conn.execute(
            """
            INSERT INTO evidence_links (
                id, project_id, fact_id, artifact_id, execution_id, relation, created_at
            ) VALUES (?, ?, ?, ?, NULL, 'supports', ?)
            """,
            (
                next_evidence_link_id(conn, execution.project_id),
                execution.project_id,
                fact_id,
                artifact_id,
                now,
            ),
        )
    conn.execute(
        """
        INSERT INTO evidence_links (
            id, project_id, fact_id, artifact_id, execution_id, relation, created_at
        ) VALUES (?, ?, ?, NULL, ?, 'derived_from', ?)
        """,
        (
            next_evidence_link_id(conn, execution.project_id),
            execution.project_id,
            fact_id,
            execution.id,
            now,
        ),
    )
    updated_intent = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (execution.intent_id, execution.project_id),
    ).fetchone()
    fact = conn.execute(
        "SELECT * FROM facts WHERE id = ? AND project_id = ?",
        (fact_id, execution.project_id),
    ).fetchone()
    return ConcludeResponse(fact=fact_to_model(fact), intent=intent_to_model(conn, updated_intent, execution.project_id))


def upload_execution_artifact(
    conn: sqlite3.Connection,
    execution_id: str,
    body: UploadExecutionArtifactRequest,
) -> Artifact:
    execution = get_execution_or_404(conn, None, execution_id)
    artifact_id = next_artifact_id(conn, execution.project_id)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO artifacts (
            id, project_id, produced_by_execution_id, type, uri, path,
            content_hash, summary, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            execution.project_id,
            execution.id,
            body.type,
            body.uri,
            body.path,
            body.content_hash,
            body.summary,
            dumps_json(body.metadata),
            now,
        ),
    )
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    assert row is not None
    return artifact_row_to_model(row)


def append_execution_events(
    conn: sqlite3.Connection,
    execution_id: str,
    body: AppendExecutionEventsRequest,
) -> list[ExecutionEvent]:
    execution = get_execution_or_404(conn, None, execution_id)
    written: list[ExecutionEvent] = []
    for event in body.events:
        if event.event_key is not None:
            existing = conn.execute(
                "SELECT * FROM execution_events WHERE execution_id = ? AND event_key = ?",
                (execution_id, event.event_key),
            ).fetchone()
            if existing is not None:
                _ensure_canonical_event_matches(existing, event)
                written.append(execution_event_row_to_model(existing))
                continue
        seq = _next_scoped_value(conn, execution.project_id, f"execution_event_seq:{execution_id}")
        project_seq = _next_scoped_value(conn, execution.project_id, "execution_event")
        now = utcnow()
        event_id = f"{execution.project_id}_ee{project_seq:06d}"
        cursor = f"evt_{project_seq}"
        conn.execute(
            """
            INSERT INTO execution_events (
                id, execution_id, project_id, seq, project_seq, cursor, ts,
                event_type, role, payload_json, event_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                execution_id,
                execution.project_id,
                seq,
                project_seq,
                cursor,
                event.ts or now,
                event.event_type,
                event.role,
                dumps_json(event.payload) or "{}",
                event.event_key,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM execution_events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None
        written.append(execution_event_row_to_model(row))
    return written


def finish_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    body: FinishExecutionRequest,
) -> ExecutionRun:
    current = get_execution_or_404(conn, None, execution_id)
    if current.leased_by is not None and current.leased_by != body.dispatcher_id:
        raise HTTPException(403, "Execution is leased by another dispatcher")
    if current.status in TERMINAL_EXECUTION_STATUSES:
        _ensure_duplicate_finish(conn, current, body)
        return current
    if current.status not in {"leased", "running"}:
        raise HTTPException(409, "Execution is not leased or running")

    if body.events:
        append_execution_events(
            conn,
            execution_id,
            AppendExecutionEventsRequest(dispatcher_id=body.dispatcher_id, events=body.events),
        )
    patch_fields = body.patch.model_dump(exclude_unset=True)
    patch_fields["dispatcher_id"] = body.dispatcher_id
    updated = patch_execution(conn, execution_id, PatchExecutionRequest(**patch_fields))
    release_execution_session_lock(conn, updated.id)
    return updated


def list_execution_events(
    conn: sqlite3.Connection,
    project_id: str,
    execution_id: str,
    *,
    after_cursor: str | None = None,
    limit: int = 200,
) -> list[ExecutionEvent]:
    execution = get_execution_or_404(conn, project_id, execution_id)
    after_project_seq = 0
    if after_cursor:
        row = conn.execute(
            "SELECT execution_id, project_seq FROM execution_events WHERE project_id = ? AND cursor = ?",
            (project_id, after_cursor),
        ).fetchone()
        if row is None:
            any_cursor = conn.execute(
                "SELECT 1 FROM execution_events WHERE cursor = ? LIMIT 1",
                (after_cursor,),
            ).fetchone()
            detail = "Foreign cursor" if any_cursor is not None else "Invalid cursor"
            raise HTTPException(400, detail)
        if row["execution_id"] != execution_id:
            raise HTTPException(400, "Cursor does not belong to execution")
        after_project_seq = row["project_seq"]
    rows = conn.execute(
        """
        SELECT *
        FROM execution_events
        WHERE execution_id = ?
          AND project_id = ?
          AND project_seq > ?
        ORDER BY project_seq
        LIMIT ?
        """,
        (execution.id, project_id, after_project_seq, max(1, min(limit, 1000))),
    ).fetchall()
    return [execution_event_row_to_model(row) for row in rows]


def _canonical_event_tuple(event_type: str, role: str | None, payload_json: str | None, ts: str | None) -> tuple:
    payload = loads_json_object(payload_json) or {}
    return (event_type, role, dumps_json(payload) or "{}", ts)


def _canonical_append_tuple(event) -> tuple:
    return (event.event_type, event.role, dumps_json(event.payload) or "{}", event.ts)


def _ensure_canonical_event_matches(row: sqlite3.Row, event) -> None:
    append_ts = event.ts if event.ts is not None else row["ts"]
    if _canonical_event_tuple(row["event_type"], row["role"], row["payload_json"], row["ts"]) != (
        event.event_type,
        event.role,
        dumps_json(event.payload) or "{}",
        append_ts,
    ):
        raise HTTPException(409, "Event key conflict")


def _ensure_duplicate_finish(conn: sqlite3.Connection, current: ExecutionRun, body: FinishExecutionRequest) -> None:
    patch = body.patch
    if patch.status != current.status:
        raise HTTPException(409, "Execution already finished with different status")
    for field, current_value in (
        ("returncode", current.returncode),
        ("error_code", current.error_code),
        ("error_detail", current.error_detail),
        ("remote_session_out_kind", current.remote_session_out_kind),
        ("remote_session_out_id", current.remote_session_out_id),
        ("remote_session_out_status", current.remote_session_out_status),
    ):
        value = getattr(patch, field)
        if value is not None and value != current_value:
            raise HTTPException(409, "Execution already finished with different patch")
    if patch.metadata is not None:
        merged_metadata = {**(current.metadata or {}), **patch.metadata}
        if merged_metadata != (current.metadata or {}):
            raise HTTPException(409, "Execution already finished with different patch")
    for event in body.events:
        if event.event_key is None:
            raise HTTPException(409, "Duplicate finish requires event keys")
        row = conn.execute(
            "SELECT * FROM execution_events WHERE execution_id = ? AND event_key = ?",
            (current.id, event.event_key),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Execution already finished; new terminal events are not allowed")
        _ensure_canonical_event_matches(row, event)


def release_execution_session_lock(conn: sqlite3.Connection, execution_id: str) -> None:
    conn.execute("DELETE FROM execution_session_locks WHERE execution_id = ?", (execution_id,))


def _add_seconds(iso_ts: str, seconds: int) -> str:
    base = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        UPDATE execution_runs
        SET status = 'cancelled',
            finished_at = COALESCE(finished_at, ?),
            error_code = COALESCE(error_code, 'reason_cancelled'),
            updated_at = ?
        WHERE project_id = ?
          AND task_type = 'reason'
          AND status IN ('pending','leased','running')
        """,
        (utcnow(), utcnow(), project_id),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE execution_runs
        SET status = 'failed',
            finished_at = COALESCE(finished_at, ?),
            error_code = COALESCE(error_code, 'lease_expired'),
            updated_at = ?
        WHERE intent_id IS NOT NULL
          AND status IN ('pending','leased','running')
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, now, now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, now, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE execution_runs
        SET status = 'failed',
            finished_at = COALESCE(finished_at, ?),
            error_code = COALESCE(error_code, 'reason_lease_expired'),
            updated_at = ?
        WHERE task_type = 'reason'
          AND status IN ('pending','leased','running')
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, now, now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, now, now, timeout)
    conn.execute(query, params)
