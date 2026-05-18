from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException

from cairn.dispatcher.config import CleanupPolicy, SshEnvironmentConfig, TerminalConfig
from cairn.dispatcher.runtime.environments.ssh import SshEnvironment
from cairn.server.db import get_conn
from cairn.server.models import WorkEnvironmentPublic, WorkEnvironmentUpsert
from cairn.server.services import (
    environment_row_to_public,
    get_environment_or_404,
    slugify_environment_id,
    utcnow,
    validate_environment_body,
)

router = APIRouter(tags=["environments"])


@router.get("/environments", response_model=list[WorkEnvironmentPublic])
def list_environments(include_secrets: bool = False):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM work_environments ORDER BY backend, created_at, id").fetchall()
        return [environment_row_to_public(row) for row in rows]


@router.post("/environments", response_model=WorkEnvironmentPublic, status_code=201)
def create_environment(body: WorkEnvironmentUpsert):
    validate_environment_body(body)
    with get_conn() as conn:
        environment_id = _choose_environment_id(conn, body.id or slugify_environment_id(body.label))
        now = utcnow()
        conn.execute(
            """
            INSERT INTO work_environments (
                id, label, backend, ssh_command, workspace_root, harness, cleanup_json, terminal_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                environment_id,
                body.label,
                body.backend,
                body.ssh_command,
                body.workspace_root,
                body.harness,
                json.dumps(body.cleanup or {"completed_action": "stop"}, ensure_ascii=True, sort_keys=True),
                json.dumps(body.terminal or {"mode": "none"}, ensure_ascii=True, sort_keys=True),
                now,
                now,
            ),
        )
        return get_environment_or_404(conn, environment_id)


@router.put("/environments/{environment_id}", response_model=WorkEnvironmentPublic)
def update_environment(environment_id: str, body: WorkEnvironmentUpsert):
    if environment_id == "docker-default" and body.backend != "docker":
        raise HTTPException(400, "docker-default cannot be changed to SSH")
    validate_environment_body(body)
    with get_conn() as conn:
        now = utcnow()
        conn.execute(
            """
            UPDATE work_environments
            SET label = ?,
                backend = ?,
                ssh_command = ?,
                workspace_root = ?,
                harness = ?,
                cleanup_json = ?,
                terminal_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                body.label,
                body.backend,
                body.ssh_command,
                body.workspace_root,
                body.harness,
                json.dumps(body.cleanup or {"completed_action": "stop"}, ensure_ascii=True, sort_keys=True),
                json.dumps(body.terminal or {"mode": "none"}, ensure_ascii=True, sort_keys=True),
                now,
                environment_id,
            ),
        )
        return get_environment_or_404(conn, environment_id)


@router.delete("/environments/{environment_id}", status_code=204)
def delete_environment(environment_id: str):
    if environment_id == "docker-default":
        raise HTTPException(400, "docker-default cannot be deleted")
    with get_conn() as conn:
        get_environment_or_404(conn, environment_id)
        conn.execute("DELETE FROM work_environments WHERE id = ?", (environment_id,))


@router.post("/environments/{environment_id}/healthcheck")
def healthcheck_environment(environment_id: str):
    with get_conn() as conn:
        environment = get_environment_or_404(conn, environment_id)
        if environment.backend != "ssh":
            result = {
                "environment_id": environment.id,
                "backend": environment.backend,
                "status": "skipped",
                "checks": [
                    {
                        "name": "docker",
                        "status": "skipped",
                        "duration_ms": 0,
                        "command": "-",
                        "stdout": "",
                        "stderr": "Docker healthchecks run in the dispatcher startup matrix.",
                    }
                ],
            }
        else:
            ssh_environment = SshEnvironment(_ssh_config_from_public(environment))
            try:
                result = ssh_environment.run_healthcheck()
            finally:
                ssh_environment.close()
        conn.execute(
            "UPDATE work_environments SET last_health_status = ?, last_healthcheck_json = ?, updated_at = ? WHERE id = ?",
            (result.get("status"), json.dumps(result, ensure_ascii=True), utcnow(), environment_id),
        )
        return result


def _choose_environment_id(conn: sqlite3.Connection, base: str) -> str:
    candidate = base
    index = 2
    while conn.execute("SELECT 1 FROM work_environments WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _ssh_config_from_public(environment: WorkEnvironmentPublic) -> SshEnvironmentConfig:
    return SshEnvironmentConfig(
        id=environment.id,
        label=environment.label,
        backend="ssh",
        ssh_command=environment.ssh_command,
        workspace_root=environment.workspace_root or "/home/kali/cairn-workspaces",
        harness="pi",
        cleanup=CleanupPolicy.model_validate(environment.cleanup or {"completed_action": "stop"}),
        terminal=TerminalConfig.model_validate(environment.terminal or {"mode": "none"}),
    )
