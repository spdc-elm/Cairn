from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException

from cairn.dispatcher.config import CleanupPolicy, SshEnvironmentConfig, TerminalConfig
from cairn.dispatcher.runtime.environments.ssh import SshEnvironment
from cairn.server.db import get_conn
from cairn.server.models import ProviderEndpointPublic, ProviderEndpointSecret, ProviderEndpointUpsert, WorkEnvironmentPublic, WorkEnvironmentUpsert
from cairn.server.services import (
    environment_row_to_public,
    get_environment_provider_endpoint_or_404,
    get_environment_or_404,
    slugify_environment_id,
    upsert_environment_provider_endpoint,
    utcnow,
    validate_environment_body,
)

router = APIRouter(tags=["environments"])


@router.get("/environments", response_model=list[WorkEnvironmentPublic])
def list_environments(include_secrets: bool = False):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM work_environments ORDER BY backend, created_at, id").fetchall()
        return [environment_row_to_public(row, conn=conn, include_secret=False) for row in rows]


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
        for endpoint in body.provider_endpoints:
            upsert_environment_provider_endpoint(conn, environment_id, endpoint)
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
        if conn.total_changes == 0:
            get_environment_or_404(conn, environment_id)
        for endpoint in body.provider_endpoints:
            upsert_environment_provider_endpoint(conn, environment_id, endpoint)
        return get_environment_or_404(conn, environment_id)


@router.delete("/environments/{environment_id}", status_code=204)
def delete_environment(environment_id: str):
    if environment_id == "docker-default":
        raise HTTPException(400, "docker-default cannot be deleted")
    with get_conn() as conn:
        get_environment_or_404(conn, environment_id)
        conn.execute("DELETE FROM work_environments WHERE id = ?", (environment_id,))


@router.get("/environments/{environment_id}/endpoints", response_model=list[ProviderEndpointPublic])
def list_environment_endpoints(environment_id: str):
    with get_conn() as conn:
        environment = get_environment_or_404(conn, environment_id)
        return environment.provider_endpoints


@router.post("/environments/{environment_id}/endpoints", response_model=ProviderEndpointPublic, status_code=201)
def create_environment_endpoint(environment_id: str, body: ProviderEndpointUpsert):
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT 1
            FROM environment_provider_endpoints
            WHERE environment_id = ? AND endpoint_id = ?
            """,
            (environment_id, body.id),
        ).fetchone()
        if existing is not None:
            raise HTTPException(409, "Provider endpoint already exists")
        return upsert_environment_provider_endpoint(conn, environment_id, body)


@router.put("/environments/{environment_id}/endpoints/{endpoint_id}", response_model=ProviderEndpointPublic)
def update_environment_endpoint(environment_id: str, endpoint_id: str, body: ProviderEndpointUpsert):
    if body.id != endpoint_id:
        raise HTTPException(400, "Endpoint id cannot be changed")
    with get_conn() as conn:
        get_environment_provider_endpoint_or_404(conn, environment_id, endpoint_id)
        return upsert_environment_provider_endpoint(conn, environment_id, body)


@router.get("/environments/{environment_id}/endpoints/{endpoint_id}")
def get_environment_endpoint(
    environment_id: str,
    endpoint_id: str,
    include_secret: bool = False,
) -> ProviderEndpointPublic | ProviderEndpointSecret:
    with get_conn() as conn:
        return get_environment_provider_endpoint_or_404(
            conn,
            environment_id,
            endpoint_id,
            include_secret=include_secret,
        )


@router.delete("/environments/{environment_id}/endpoints/{endpoint_id}", status_code=204)
def delete_environment_endpoint(environment_id: str, endpoint_id: str):
    with get_conn() as conn:
        get_environment_provider_endpoint_or_404(conn, environment_id, endpoint_id)
        conn.execute(
            """
            DELETE FROM environment_provider_endpoints
            WHERE environment_id = ? AND endpoint_id = ?
            """,
            (environment_id, endpoint_id),
        )


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
        _append_endpoint_healthchecks(result, environment)
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


def _append_endpoint_healthchecks(result: dict, environment: WorkEnvironmentPublic) -> None:
    checks = result.setdefault("checks", [])
    endpoint_statuses: list[str] = []
    for endpoint in environment.provider_endpoints:
        missing = []
        if not endpoint.base_url:
            missing.append("base_url")
        if endpoint.type == "pi" and not endpoint.provider_api:
            missing.append("provider_api")
        if not endpoint.has_api_key:
            missing.append("api_key")
        status = "ok" if not missing else "fail"
        endpoint_statuses.append(status)
        checks.append(
            {
                "name": f"endpoint:{endpoint.id}",
                "status": status,
                "duration_ms": 0,
                "command": "-",
                "stdout": "",
                "stderr": "" if not missing else f"missing fields: {', '.join(missing)}",
            }
        )
    if endpoint_statuses and any(status != "ok" for status in endpoint_statuses):
        result["status"] = "fail"
