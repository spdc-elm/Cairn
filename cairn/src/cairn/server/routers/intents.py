from fastapi import APIRouter
from fastapi import HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
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
    dumps_json,
    fact_to_model,
    get_claimable_open_intent_or_404,
    get_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
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
        claimed = body.worker is not None
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, worker, requested_worker,
                timeout_override_seconds, conclude_timeout_override_seconds, control_state,
                last_heartbeat_at, created_at, concluded_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'normal', ?, ?, NULL)
            """,
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                body.requested_worker,
                body.timeout_override_seconds,
                body.conclude_timeout_override_seconds,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            requested_worker=body.requested_worker,
            timeout_override_seconds=body.timeout_override_seconds,
            conclude_timeout_override_seconds=body.conclude_timeout_override_seconds,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
        )


@router.patch(
    "/projects/{project_id}/intents/{intent_id}",
    response_model=Intent,
)
def update_intent(project_id: str, intent_id: str, body: UpdateIntentRequest):
    with get_conn() as conn:
        check_project_intent_delete_writable(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Only open intents can be updated")
        if row["worker"] is not None and (
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
        if body.control_state is not None:
            updates.extend([
                "control_state = 'normal'",
                "control_requested_at = NULL",
                "control_requested_by = NULL",
                "control_reason = NULL",
            ])
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
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

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

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.delete(
    "/projects/{project_id}/intents/{intent_id}",
    status_code=204,
)
def delete_open_intent(project_id: str, intent_id: str):
    with get_conn() as conn:
        check_project_intent_delete_writable(conn, project_id)
        row = get_intent_or_404(conn, project_id, intent_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Only open intents can be deleted")
        if row["worker"] is not None:
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
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")
        now = utcnow()
        conn.execute(
            """
            UPDATE intents
            SET control_state = 'conclude_requested',
                control_requested_at = ?,
                control_requested_by = ?,
                control_reason = ?
            WHERE id = ? AND project_id = ?
            """,
            (now, body.actor, body.reason, intent_id, project_id),
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

        conn.execute(
            "INSERT INTO facts (id, project_id, description, metadata_json) VALUES (?, ?, ?, ?)",
            (fid, project_id, body.description, dumps_json(body.metadata)),
        )
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, intent_id, project_id),
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
