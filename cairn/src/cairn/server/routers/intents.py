from fastapi import APIRouter
from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    LeaseExecutionRequest,
    PatchExecutionRequest,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
    RequestConcludeRequest,
    UpdateIntentRequest,
)
from cairn.server.services import (
    check_project_active,
    check_project_intent_delete_writable,
    derive_fact_title,
    dumps_json,
    fact_to_model,
    get_claimable_open_intent_or_404,
    get_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    lease_execution,
    next_fact_id,
    next_intent_id,
    patch_execution,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
    validate_requested_worker,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)
        validate_requested_worker(body.requested_worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, requested_worker,
                timeout_override_seconds, conclude_timeout_override_seconds,
                created_at, concluded_at, concluded_fact_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.requested_worker,
                body.timeout_override_seconds,
                body.conclude_timeout_override_seconds,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        if body.worker is not None:
            lease_execution(
                conn,
                LeaseExecutionRequest(
                    project_id=project_id,
                    dispatcher_id=body.worker,
                    worker_name=body.worker,
                    task_type="explore",
                    phase="run",
                ),
                intent_id=iid,
            )
        row = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (iid, project_id),
        ).fetchone()
        return intent_to_model(conn, row, project_id)


@router.patch(
    "/projects/{project_id}/intents/{intent_id}",
    response_model=Intent,
)
def update_intent(project_id: str, intent_id: str, body: UpdateIntentRequest):
    with get_conn() as conn:
        check_project_intent_delete_writable(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["concluded_fact_id"] is not None:
            raise HTTPException(409, "Only open intents can be updated")
        active = _active_intent_execution(conn, project_id, intent_id)
        if active is not None and active["worker_name"] is not None and (
            body.requested_worker is not None
            or body.timeout_override_seconds is not None
            or body.conclude_timeout_override_seconds is not None
        ):
            raise HTTPException(409, "Cannot change scheduling fields while intent is running")

        updates: list[str] = []
        params: list[object] = []
        if body.description is not None:
            updates.append("description = ?")
            params.append(body.description)
        if body.requested_worker is not None:
            validate_requested_worker(body.requested_worker)
            updates.append("requested_worker = ?")
            params.append(body.requested_worker)
        if body.timeout_override_seconds is not None:
            updates.append("timeout_override_seconds = ?")
            params.append(body.timeout_override_seconds)
        if body.conclude_timeout_override_seconds is not None:
            updates.append("conclude_timeout_override_seconds = ?")
            params.append(body.conclude_timeout_override_seconds)
        if body.control_state is not None and active is not None:
            patch_execution(conn, active["id"], PatchExecutionRequest(control_state="normal", control_reason=None))
        if updates:
            params.extend([intent_id, project_id])
            conn.execute(
                f"UPDATE intents SET {', '.join(updates)} WHERE id = ? AND project_id = ?",
                tuple(params),
            )
        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        active = _active_intent_execution(conn, project_id, intent_id)
        if active is None:
            lease_execution(
                conn,
                LeaseExecutionRequest(
                    project_id=project_id,
                    dispatcher_id=body.worker,
                    worker_name=body.worker,
                    task_type="explore",
                    phase="run",
                ),
                intent_id=intent_id,
            )
        else:
            patch_execution(conn, active["id"], PatchExecutionRequest(last_heartbeat_at=now, lease_seconds=60))

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        active = _active_intent_execution(conn, project_id, intent_id)
        if active is not None and active["worker_name"] == body.worker:
            patch_execution(conn, active["id"], PatchExecutionRequest(status="cancelled", error_code="released"))
            row = conn.execute("SELECT * FROM intents WHERE id = ? AND project_id = ?", (intent_id, project_id)).fetchone()

        return intent_to_model(conn, row, project_id)


@router.delete(
    "/projects/{project_id}/intents/{intent_id}",
    status_code=204,
)
def delete_open_intent(project_id: str, intent_id: str):
    with get_conn() as conn:
        check_project_intent_delete_writable(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["concluded_fact_id"] is not None:
            raise HTTPException(409, "Only open intents can be deleted")
        if _active_intent_execution(conn, project_id, intent_id) is not None:
            raise HTTPException(409, "Running intents cannot be deleted")
        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/request-conclude",
    response_model=Intent,
)
def request_conclude(project_id: str, intent_id: str, body: RequestConcludeRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["concluded_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")
        now = utcnow()
        active = _active_intent_execution(conn, project_id, intent_id)
        if active is None:
            raise HTTPException(409, "Intent has no active execution to conclude")
        patch_execution(
            conn,
            active["id"],
            PatchExecutionRequest(
                control_state="conclude_requested",
                control_reason=body.reason or f"requested by {body.actor}",
                last_heartbeat_at=now,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        fid = next_fact_id(conn, project_id)
        title = body.title or derive_fact_title(body.description, fid)
        active = _active_intent_execution(conn, project_id, intent_id)

        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, kind, status, title, description, metadata_json,
                produced_by_execution_id, produced_by_intent_id, created_at, updated_at
            ) VALUES (?, ?, 'fact', 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (fid, project_id, title, body.description, dumps_json(body.metadata), active["id"] if active is not None else None, intent_id, now, now),
        )
        conn.execute(
            """
            UPDATE intents
            SET concluded_fact_id = ?,
                concluded_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (fid, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=fact_to_model(conn.execute(
                "SELECT * FROM facts WHERE id = ? AND project_id = ?",
                (fid, project_id),
            ).fetchone()),
            intent=intent_to_model(conn, updated, project_id),
        )


def _active_intent_execution(conn, project_id: str, intent_id: str):
    return conn.execute(
        """
        SELECT *
        FROM execution_runs
        WHERE project_id = ?
          AND intent_id = ?
          AND status IN ('pending', 'leased', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (project_id, intent_id),
    ).fetchone()
