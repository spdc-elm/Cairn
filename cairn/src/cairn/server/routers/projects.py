from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    CompleteRequest,
    CreateExecutionRequest,
    CreateProjectRequest,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    LeaseExecutionRequest,
    PatchExecutionRequest,
    ProjectDetail,
    ProjectMeta,
    ProjectAgentContext,
    ProjectAgentContextUpsert,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    UpdateFactRequest,
    ReasonClaimRequest,
    UpdateProjectRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
)
from cairn.server.services import (
    build_intents,
    check_project_completed,
    check_project_active,
    clear_project_reason,
    create_execution_run,
    default_environment,
    derive_fact_title,
    dumps_json,
    expire_reason_leases,
    expire_workers,
    fact_to_model,
    get_completion_intent_or_409,
    get_project_or_404,
    get_environment_or_404,
    intent_to_model,
    lease_execution,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    planned_workspace_for,
    project_meta_from_row,
    patch_execution,
    SYSTEM_HEALTHCHECK_PROJECT_ID,
    utcnow,
    get_project_agent_context as get_project_agent_context_service,
    upsert_project_agent_context,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        rows = conn.execute("""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (
                    SELECT COUNT(DISTINCT i.id)
                    FROM intents i
                    JOIN execution_runs er ON er.project_id = i.project_id AND er.intent_id = i.id
                    WHERE i.project_id = p.id
                      AND i.concluded_fact_id IS NULL
                      AND er.status IN ('pending','leased','running')
                ) AS working_intent_count,
                (
                    SELECT COUNT(*)
                    FROM intents i
                    WHERE i.project_id = p.id
                      AND i.concluded_fact_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM execution_runs er
                          WHERE er.project_id = i.project_id
                            AND er.intent_id = i.id
                            AND er.status IN ('pending','leased','running')
                      )
                ) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            WHERE p.id != ?
            ORDER BY p.created_at
        """, (SYSTEM_HEALTHCHECK_PROJECT_ID,)).fetchall()
        summaries = []
        for row in rows:
            environment = _project_environment_or_none(conn, row["environment_id"])
            meta = project_meta_from_row(row, environment=environment, conn=conn)
            summaries.append(ProjectSummary(
                **meta.model_dump(),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            ))
        return summaries


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()
        try:
            environment = get_environment_or_404(conn, body.environment_id) if body.environment_id else default_environment(conn)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(400, "Unknown environment") from exc
            raise

        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at, auto_reason, allowed_auto_workers_json,
                default_timeout_seconds, default_conclude_timeout_seconds, environment_id
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                body.title,
                now,
                1 if body.auto_reason else 0,
                dumps_json(body.allowed_auto_workers),
                body.default_timeout_seconds,
                body.default_conclude_timeout_seconds,
                environment.id,
            ),
        )
        conn.execute(
            """
            INSERT INTO facts (id, project_id, kind, status, title, description, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            ("origin", pid, "origin", "Origin", body.origin, now, now),
        )
        conn.execute(
            """
            INSERT INTO facts (id, project_id, kind, status, title, description, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            ("goal", pid, "goal", "Goal", body.goal, now, now),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        if body.agent_context is not None:
            upsert_project_agent_context(conn, pid, body.agent_context)

        return ProjectDetail(
            project=project_meta_from_row(get_project_or_404(conn, pid), environment=environment, conn=conn),
            facts=[
                Fact(id="origin", title="Origin", description=body.origin),
                Fact(id="goal", title="Goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        environment = _project_environment_or_none(conn, row["environment_id"])
        meta = project_meta_from_row(row, environment=environment, conn=conn)
        return ProjectDetail(
            project=meta,
            facts=[fact_to_model(f) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
        )


@router.get("/projects/{project_id}/graph")
def get_project_graph(project_id: str):
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id)
        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        intents = conn.execute(
            "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        intent_payloads = []
        for intent in intents:
            model = intent_to_model(conn, intent, project_id).model_dump(by_alias=True)
            active = conn.execute(
                """
                SELECT *
                FROM execution_runs
                WHERE project_id = ?
                  AND intent_id = ?
                  AND status IN ('pending', 'leased', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, intent["id"]),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT *
                FROM execution_runs
                WHERE project_id = ?
                  AND intent_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, intent["id"]),
            ).fetchone()
            source = active or latest
            model.update(
                {
                    "active_execution_id": active["id"] if active is not None else None,
                    "latest_execution_id": latest["id"] if latest is not None else None,
                    "runtime_status": source["status"] if source is not None else None,
                    "active_worker_name": active["worker_name"] if active is not None else None,
                    "latest_worker_name": latest["worker_name"] if latest is not None else None,
                    "worker_name": source["worker_name"] if source is not None else None,
                    "last_heartbeat_at": source["last_heartbeat_at"] if source is not None else None,
                }
            )
            intent_payloads.append(model)
        return {
            "project": project_meta_from_row(row, conn=conn).model_dump(),
            "facts": [
                {
                    **fact_to_model(fact).model_dump(),
                    "kind": fact["kind"] if "kind" in fact.keys() else "fact",
                    "status": fact["status"] if "status" in fact.keys() else "active",
                    "producing_execution_id": _fact_producing_execution_id(conn, project_id, fact),
                    "artifact_summaries": [],
                }
                for fact in facts
            ],
            "intents": intent_payloads,
            "hints": [
                dict(hint)
                for hint in conn.execute(
                    "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
                    (project_id,),
                ).fetchall()
            ],
        }


@router.get("/projects/{project_id}/agent-context", response_model=ProjectAgentContext | None)
def get_project_agent_context(project_id: str):
    with get_conn() as conn:
        return get_project_agent_context_service(conn, project_id)


@router.put("/projects/{project_id}/agent-context", response_model=ProjectAgentContext)
def put_project_agent_context(project_id: str, body: ProjectAgentContextUpsert):
    with get_conn() as conn:
        return upsert_project_agent_context(conn, project_id, body)


def _project_environment_or_none(conn, environment_id: str | None):
    if not environment_id:
        return default_environment(conn)
    try:
        return get_environment_or_404(conn, environment_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


def _fact_producing_execution_id(conn, project_id: str, fact) -> str | None:
    if "produced_by_execution_id" in fact.keys() and fact["produced_by_execution_id"]:
        return fact["produced_by_execution_id"]
    intent_id = fact["produced_by_intent_id"] if "produced_by_intent_id" in fact.keys() else None
    if not intent_id:
        return None
    row = conn.execute(
        """
        SELECT id
        FROM execution_runs
        WHERE project_id = ?
          AND intent_id = ?
        ORDER BY
          CASE status WHEN 'succeeded' THEN 0 WHEN 'running' THEN 1 WHEN 'leased' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,
          COALESCE(finished_at, updated_at, created_at) DESC,
          created_at DESC
        LIMIT 1
        """,
        (project_id, intent_id),
    ).fetchone()
    return row["id"] if row is not None else None


@router.patch("/projects/{project_id}/facts/{fact_id}", response_model=Fact)
def update_fact(project_id: str, fact_id: str, body: UpdateFactRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Fact not found")

        updates: list[str] = []
        params: list[object] = []
        if body.title is not None:
            updates.append("title = ?")
            params.append(body.title)
        if body.description is not None:
            updates.append("description = ?")
            params.append(body.description)
        if updates:
            params.extend([fact_id, project_id])
            conn.execute(
                f"UPDATE facts SET {', '.join(updates)} WHERE id = ? AND project_id = ?",
                tuple(params),
            )
        updated = conn.execute(
            "SELECT * FROM facts WHERE id = ? AND project_id = ?",
            (fact_id, project_id),
        ).fetchone()
        return fact_to_model(updated)




@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated, conn=conn)


@router.patch("/projects/{project_id}", response_model=ProjectMeta)
def update_project(project_id: str, body: UpdateProjectRequest):
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id)
        updates: list[str] = []
        params: list[object] = []
        fields = body.model_fields_set
        if "auto_reason" in fields:
            updates.append("auto_reason = ?")
            params.append(1 if body.auto_reason else 0)
        if "allowed_auto_workers" in fields:
            updates.append("allowed_auto_workers_json = ?")
            params.append(dumps_json(body.allowed_auto_workers))
        if "default_timeout_seconds" in fields:
            updates.append("default_timeout_seconds = ?")
            params.append(body.default_timeout_seconds)
        if "default_conclude_timeout_seconds" in fields:
            updates.append("default_conclude_timeout_seconds = ?")
            params.append(body.default_conclude_timeout_seconds)
        if not updates:
            return project_meta_from_row(row, conn=conn)
        params.append(project_id)
        conn.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        environment = _project_environment_or_none(conn, updated["environment_id"])
        return project_meta_from_row(updated, environment=environment, conn=conn)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row, conn=conn)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "stopped":
            conn.execute(
                """
                UPDATE execution_runs
                SET status = 'cancelled',
                    finished_at = ?,
                    error_code = COALESCE(error_code, 'project_stopped'),
                    updated_at = ?
                WHERE project_id = ?
                  AND status IN ('pending','leased','running')
                """,
                (utcnow(), utcnow(), project_id),
            )
            clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated, conn=conn)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        active = _active_reason_execution(conn, project_id)
        current_worker = active["worker_name"] if active is not None else None
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row, conn=conn)

        execution = create_execution_run(
            conn,
            project_id,
            CreateExecutionRequest(task_type="reason", phase="run", metadata={"trigger": body.trigger}),
        )
        lease_execution(
            conn,
            LeaseExecutionRequest(
                project_id=project_id,
                execution_id=execution.id,
                dispatcher_id=body.worker,
                worker_name=body.worker,
                task_type="reason",
                phase="run",
            ),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated, conn=conn)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        active = _active_reason_execution(conn, project_id)
        current_worker = active["worker_name"] if active is not None else None
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        patch_execution(conn, active["id"], PatchExecutionRequest(last_heartbeat_at=now, lease_seconds=60))
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated, conn=conn)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        active = _active_reason_execution(conn, project_id)
        current_worker = active["worker_name"] if active is not None else None
        if current_worker is None:
            return project_meta_from_row(row, conn=conn)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        patch_execution(conn, active["id"], PatchExecutionRequest(status="cancelled", error_code="released"))
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated, conn=conn)


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)

        now = utcnow()
        iid = next_intent_id(conn, project_id)

        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at, concluded_at, concluded_fact_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'goal')
            """,
            (iid, project_id, body.description, body.worker, now, now),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )
        conn.execute(
            """
            UPDATE projects
            SET status = 'completed'
            WHERE id = ?
            """,
            (project_id,),
        )
        clear_project_reason(conn, project_id)

        return Intent(
            id=iid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            creator=body.worker,
            worker=None,
            requested_worker=None,
            last_heartbeat_at=None,
            created_at=now,
            concluded_at=now,
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            """
            INSERT INTO facts (
                id, project_id, kind, status, title, description,
                produced_by_intent_id, created_at, updated_at
            ) VALUES (?, ?, 'fact', 'active', ?, ?, ?, ?, ?)
            """,
            (fact_id, project_id, derive_fact_title(description, fact_id), description, intent_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, description, creator, created_at, concluded_at, concluded_fact_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (intent_id, project_id, "external_feedback", creator, now, now, fact_id),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project, conn=conn),
            fact=Fact(id=fact_id, title=derive_fact_title(description, fact_id), description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )


def _active_reason_execution(conn, project_id: str):
    return conn.execute(
        """
        SELECT *
        FROM execution_runs
        WHERE project_id = ?
          AND task_type = 'reason'
          AND status IN ('pending','leased','running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
