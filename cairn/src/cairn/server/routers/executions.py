from __future__ import annotations

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import (
    AppendExecutionEventsRequest,
    Artifact,
    ClaimHealthcheckExecutionsRequest,
    ClaimQuestionExecutionsRequest,
    ConcludeResponse,
    CreateExecutionRequest,
    ExecutionConclusionReportRequest,
    ExecutionEventsResponse,
    ExecutionRun,
    FinishExecutionRequest,
    LeaseExecutionRequest,
    PatchExecutionRequest,
    UploadExecutionArtifactRequest,
)
from cairn.server.services import (
    append_execution_events,
    claim_pending_healthcheck_executions,
    claim_pending_question_executions,
    create_execution_run,
    finish_execution,
    get_execution_or_404,
    lease_execution,
    list_execution_events,
    patch_execution,
    submit_execution_conclusion_report,
    upload_execution_artifact,
)

router = APIRouter(tags=["executions"])


@router.post("/projects/{project_id}/executions", response_model=ExecutionRun, status_code=201)
def create_project_execution(project_id: str, body: CreateExecutionRequest):
    with get_conn() as conn:
        return create_execution_run(conn, project_id, body)


@router.get("/projects/{project_id}/executions/{execution_id}", response_model=ExecutionRun)
def get_project_execution(project_id: str, execution_id: str):
    with get_conn() as conn:
        return get_execution_or_404(conn, project_id, execution_id)


@router.get("/projects/{project_id}/executions/{execution_id}/events", response_model=ExecutionEventsResponse)
def get_project_execution_events(
    project_id: str,
    execution_id: str,
    after_cursor: str | None = None,
    limit: int = 200,
):
    with get_conn() as conn:
        events = list_execution_events(
            conn,
            project_id,
            execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )
    return ExecutionEventsResponse(events=events, next_cursor=events[-1].cursor if events else after_cursor)


@router.post("/dispatcher/executions/lease", response_model=ExecutionRun)
def dispatcher_lease_pending_execution(body: LeaseExecutionRequest):
    with get_conn() as conn:
        return lease_execution(conn, body)


@router.post("/dispatcher/healthcheck-executions/claim", response_model=list[ExecutionRun])
def dispatcher_claim_healthcheck_executions(body: ClaimHealthcheckExecutionsRequest):
    with get_conn() as conn:
        return claim_pending_healthcheck_executions(
            conn,
            dispatcher_id=body.dispatcher_id,
            worker_names=body.worker_names,
            environment_ids=body.environment_ids,
            limit=body.limit,
            lease_seconds=body.lease_seconds,
        )


@router.post("/dispatcher/question-executions/claim", response_model=list[ExecutionRun])
def dispatcher_claim_question_executions(body: ClaimQuestionExecutionsRequest):
    with get_conn() as conn:
        return claim_pending_question_executions(
            conn,
            dispatcher_id=body.dispatcher_id,
            worker_names=body.worker_names,
            environment_ids=body.environment_ids,
            limit=body.limit,
            lease_seconds=body.lease_seconds,
        )


@router.post("/dispatcher/intents/{intent_id}/lease-execution", response_model=ExecutionRun, status_code=201)
def dispatcher_lease_intent_execution(intent_id: str, body: LeaseExecutionRequest):
    with get_conn() as conn:
        return lease_execution(conn, body, intent_id=intent_id)


@router.patch("/dispatcher/executions/{execution_id}", response_model=ExecutionRun)
def dispatcher_patch_execution(execution_id: str, body: PatchExecutionRequest):
    with get_conn() as conn:
        return patch_execution(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/events")
def dispatcher_append_execution_events(execution_id: str, body: AppendExecutionEventsRequest):
    with get_conn() as conn:
        return append_execution_events(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/finish", response_model=ExecutionRun)
def dispatcher_finish_execution(execution_id: str, body: FinishExecutionRequest):
    with get_conn() as conn:
        return finish_execution(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/conclusion-report", response_model=ConcludeResponse)
def dispatcher_submit_execution_conclusion_report(execution_id: str, body: ExecutionConclusionReportRequest):
    with get_conn() as conn:
        return submit_execution_conclusion_report(conn, execution_id, body)


@router.post("/dispatcher/executions/{execution_id}/artifacts", response_model=Artifact, status_code=201)
def dispatcher_upload_execution_artifact(execution_id: str, body: UploadExecutionArtifactRequest):
    with get_conn() as conn:
        return upload_execution_artifact(conn, execution_id, body)
