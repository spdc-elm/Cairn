from __future__ import annotations

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import RemoteSessionProvenance
from cairn.server.questions.manager import (
    QuestionGoneError,
    ResumeLockError,
    append_question_events,
    append_question_message_job,
    cancel_question_job,
    claim_question_job,
    close_question_thread,
    create_question_thread,
    fail_question_job,
    finish_question_job,
    get_question_thread_detail,
    heartbeat_question_job,
    recover_question_runtime,
    requeue_question_job,
    reset_runtime_for_tests,
    start_question_job,
)
from cairn.server.questions.models import (
    QuestionClaimRequest,
    QuestionClaimResponse,
    QuestionCloseResponse,
    QuestionCreateRequest,
    QuestionEvent,
    QuestionJob,
    QuestionJobEventsRequest,
    QuestionJobHeartbeatRequest,
    QuestionJobTerminalRequest,
    QuestionMessageRequest,
    QuestionPromotionRequest,
    QuestionPromotionResponse,
    QuestionThread,
)
from cairn.server.services import (
    check_project_active,
    derive_fact_title,
    dumps_json,
    get_project_or_404,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    question_modes_from_inventory,
    resolve_anchor,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["questions"])


def reset_question_state_for_tests() -> None:
    with get_conn() as conn:
        reset_runtime_for_tests(conn)


def set_question_executor_for_tests(_executor) -> None:
    raise RuntimeError("server question execution has been removed; use dispatcher question jobs")


def recover_question_state() -> None:
    with get_conn() as conn:
        recover_question_runtime(conn)


@router.post("/projects/{project_id}/questions", response_model=QuestionThread, status_code=201)
def create_question(project_id: str, body: QuestionCreateRequest):
    with get_conn() as conn:
        project_row = get_project_or_404(conn, project_id)
        _reject_running_or_open_intent(conn, project_id, body.anchor_type, body.anchor_id)
        resolution = resolve_anchor(conn, project_id, body.anchor_type, body.anchor_id)
        worker_name = body.worker_name or (resolution.provenance.worker_name if resolution.provenance else None)
        mode = _choose_mode(conn, resolution, body, worker_name)
        execution = _question_execution_identity(conn, project_row, resolution, worker_name, mode)
        try:
            thread = create_question_thread(
                conn,
                project_id=project_id,
                anchor_type=body.anchor_type,
                anchor_id=body.anchor_id,
                worker_name=worker_name,
                execution_environment_id=execution["environment_id"],
                execution_worker_type=execution["worker_type"],
                execution_endpoint_id=execution["endpoint_id"],
                execution_model_profile_id=execution["model_profile_id"],
                anchor_resolution=resolution,
                mode=mode,
                session_effect=_session_effect(mode),
                notice=_notice_for_mode(mode),
            )
        except ResumeLockError as exc:
            raise HTTPException(409, str(exc)) from exc
        if body.message:
            append_question_message_job(conn, thread.id, body.message)
            thread = get_question_thread_detail(conn, project_id, thread.id)
        return thread


@router.get("/projects/{project_id}/questions/{question_id}", response_model=QuestionThread)
def get_question(project_id: str, question_id: str):
    with get_conn() as conn:
        return _get_thread_or_http(conn, project_id, question_id)


@router.post("/projects/{project_id}/questions/{question_id}/messages", response_model=QuestionThread)
def post_question_message(project_id: str, question_id: str, body: QuestionMessageRequest):
    with get_conn() as conn:
        _get_thread_or_http(conn, project_id, question_id)
        append_question_message_job(conn, question_id, body.message)
        return get_question_thread_detail(conn, project_id, question_id)


@router.post("/projects/{project_id}/questions/{question_id}/close", response_model=QuestionCloseResponse)
def close_question(project_id: str, question_id: str):
    with get_conn() as conn:
        closed = close_question_thread(conn, project_id, question_id)
        return QuestionCloseResponse(id=closed.id, status=closed.status)


@router.post("/projects/{project_id}/questions/{question_id}/promote", response_model=QuestionPromotionResponse)
def promote_question(project_id: str, question_id: str, body: QuestionPromotionRequest):
    with get_conn() as conn:
        thread = _get_thread_or_http(conn, project_id, question_id)
        check_project_active(conn, project_id)
        metadata = _promotion_metadata(thread, body.answer_summary or body.content)
        if body.kind == "hint":
            hint_id = next_hint_id(conn, project_id)
            conn.execute(
                "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                (hint_id, project_id, body.content, "question", utcnow()),
            )
            return QuestionPromotionResponse(kind="hint", object_id=hint_id)
        if body.kind == "fact":
            fact_id = next_fact_id(conn, project_id)
            conn.execute(
                "INSERT INTO facts (id, project_id, title, description, metadata_json) VALUES (?, ?, ?, ?, ?)",
                (fact_id, project_id, body.title or derive_fact_title(body.content, fact_id), body.content, dumps_json(metadata)),
            )
            return QuestionPromotionResponse(kind="fact", object_id=fact_id)
        source_ids = _promotion_sources(conn, thread, body.from_fact_ids)
        validate_facts_exist(conn, project_id, source_ids)
        validate_goal_not_in_sources(source_ids)
        intent_id = next_intent_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, worker, requested_worker,
                timeout_override_seconds, conclude_timeout_override_seconds, control_state,
                last_heartbeat_at, created_at, concluded_at
            ) VALUES (?, ?, NULL, ?, 'question', NULL, NULL, NULL, NULL, 'normal', NULL, ?, NULL)
            """,
            (intent_id, project_id, body.content, now),
        )
        for fact_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, fact_id),
            )
        return QuestionPromotionResponse(kind="intent", object_id=intent_id)


@router.post("/dispatcher/question-jobs/claim", response_model=QuestionClaimResponse)
def dispatcher_claim_question_job(body: QuestionClaimRequest):
    with get_conn() as conn:
        return claim_question_job(
            conn,
            dispatcher_id=body.dispatcher_id,
            worker_names=body.worker_names,
            environment_ids=body.environment_ids,
            limit=body.limit,
        )


@router.post("/dispatcher/question-jobs/{job_id}/start", response_model=QuestionJob)
def dispatcher_start_question_job(job_id: str, body: QuestionJobHeartbeatRequest):
    with get_conn() as conn:
        return start_question_job(conn, job_id, body.dispatcher_id)


@router.post("/dispatcher/question-jobs/{job_id}/heartbeat", response_model=QuestionJob)
def dispatcher_heartbeat_question_job(job_id: str, body: QuestionJobHeartbeatRequest):
    with get_conn() as conn:
        return heartbeat_question_job(conn, job_id, body.dispatcher_id)


@router.post("/dispatcher/question-jobs/{job_id}/events", response_model=list[QuestionEvent])
def dispatcher_append_question_events(job_id: str, body: QuestionJobEventsRequest):
    with get_conn() as conn:
        return append_question_events(conn, job_id=job_id, dispatcher_id=body.dispatcher_id, events=body.events)


@router.post("/dispatcher/question-jobs/{job_id}/finish", response_model=QuestionJob)
def dispatcher_finish_question_job(job_id: str, body: QuestionJobTerminalRequest):
    with get_conn() as conn:
        return finish_question_job(
            conn,
            job_id=job_id,
            dispatcher_id=body.dispatcher_id,
            result_text=body.result_text,
            run_log_id=body.run_log_id,
            question_session=body.question_remote_session,
        )


@router.post("/dispatcher/question-jobs/{job_id}/fail", response_model=QuestionJob)
def dispatcher_fail_question_job(job_id: str, body: QuestionJobTerminalRequest):
    with get_conn() as conn:
        return fail_question_job(
            conn,
            job_id=job_id,
            dispatcher_id=body.dispatcher_id,
            error_code=body.error_code or "worker_process_failed",
            error_detail=body.error_detail or "worker process failed",
        )


@router.post("/dispatcher/question-jobs/{job_id}/requeue", response_model=QuestionJob)
def dispatcher_requeue_question_job(job_id: str, body: QuestionJobTerminalRequest):
    with get_conn() as conn:
        return requeue_question_job(conn, job_id=job_id, dispatcher_id=body.dispatcher_id, error_detail=body.error_detail)


@router.post("/dispatcher/question-jobs/{job_id}/cancelled", response_model=QuestionJob)
def dispatcher_cancel_question_job(job_id: str, body: QuestionJobTerminalRequest):
    with get_conn() as conn:
        return cancel_question_job(conn, job_id=job_id, dispatcher_id=body.dispatcher_id, error_detail=body.error_detail)


def _choose_mode(conn, resolution, body: QuestionCreateRequest, worker_name: str | None):
    if body.mode == "resume" and not body.confirm_resume:
        raise HTTPException(409, "resume_confirmation_required")
    available, reasons = _available_modes_from_inventory(conn, resolution, worker_name)
    if body.mode == "auto":
        if "fork" in available:
            return "fork"
        if "resume" in available and body.allow_resume_without_fork:
            if not body.confirm_resume:
                raise HTTPException(409, "resume_confirmation_required")
            return "resume"
        return "fresh_context"
    if body.mode == "fresh_context":
        return "fresh_context"
    if body.mode == "resume":
        if "resume" not in available:
            raise HTTPException(409, reasons.get("resume") or "source_session_missing")
        return "resume"
    if body.mode == "fork":
        if "fork" not in available:
            raise HTTPException(409, reasons.get("fork") or "fork_unavailable")
        return "fork"
    raise HTTPException(400, "unsupported_question_mode")


def _available_modes_from_inventory(conn, resolution, worker_name: str | None) -> tuple[list[str], dict[str, str]]:
    modes = ["fresh_context"]
    reasons: dict[str, str] = {}
    provenance = resolution.provenance
    if resolution.status != "exact" or provenance is None:
        reasons["resume"] = "source_session_missing"
        reasons["fork"] = "source_session_missing"
        return modes, reasons
    return question_modes_from_inventory(conn, provenance, worker_name)


def _question_execution_identity(conn, project_row, resolution, worker_name: str | None, mode: str) -> dict[str, str | None]:
    if mode in {"fork", "resume"} and resolution.provenance is not None:
        provenance = resolution.provenance
        return {
            "environment_id": provenance.environment_id or (project_row["environment_id"] if "environment_id" in project_row.keys() else None),
            "worker_type": provenance.worker_type,
            "endpoint_id": provenance.endpoint_id,
            "model_profile_id": provenance.model_profile_id,
        }
    worker_row = conn.execute("SELECT * FROM worker_inventory WHERE name = ?", (worker_name,)).fetchone() if worker_name else None
    return {
        "environment_id": project_row["environment_id"] if "environment_id" in project_row.keys() else None,
        "worker_type": worker_row["type"] if worker_row is not None else None,
        "endpoint_id": worker_row["endpoint"] if worker_row is not None else None,
        "model_profile_id": worker_row["model_profile"] if worker_row is not None else None,
    }


def _reject_running_or_open_intent(conn, project_id: str, anchor_type: str, anchor_id: str) -> None:
    if anchor_type != "intent":
        return
    row = conn.execute(
        "SELECT to_fact_id, worker FROM intents WHERE project_id = ? AND id = ?",
        (project_id, anchor_id),
    ).fetchone()
    if row is None:
        return
    if row["to_fact_id"] is None:
        if row["worker"] is not None:
            raise HTTPException(409, "running_intent_question_out_of_scope")
        raise HTTPException(409, "open_intent_question_out_of_scope")


def _get_thread_or_http(conn, project_id: str, question_id: str) -> QuestionThread:
    try:
        return get_question_thread_detail(conn, project_id, question_id)
    except QuestionGoneError as exc:
        raise HTTPException(410, "Question thread is gone") from exc
    except KeyError as exc:
        raise HTTPException(404, "Question thread not found") from exc


def _session_effect(mode: str):
    if mode == "resume":
        return "continued"
    if mode == "fork":
        return "forked"
    return "fresh"


def _notice_for_mode(mode: str) -> str:
    if mode == "resume":
        return "Resume continues the source remote session and is recorded in Cairn run logs."
    if mode == "fork":
        return "Fork asks in a separate remote session when the dispatcher/backend supports it."
    return "Fresh-context Q&A uses Cairn summaries and prior thread messages; it is not connected to the source worker session."


def _promotion_metadata(thread: QuestionThread, answer_summary: str) -> dict:
    source_session = thread.source_session
    return {
        "source": {
            "kind": "question_thread",
            "question_thread_id": thread.id,
            "anchor_type": thread.anchor_type,
            "anchor_id": thread.anchor_id,
            "source_run_log_id": thread.source_run_log_id,
            "mode": thread.mode,
            "session_effect": thread.session_effect,
            "source_remote_session": {
                "kind": source_session.kind,
                "id": source_session.id,
                "status": source_session.status,
            },
            "answer_summary": answer_summary[:500],
        }
    }


def _promotion_sources(conn, thread: QuestionThread, provided: list[str] | None) -> list[str]:
    if provided:
        return provided
    if thread.anchor_type == "fact":
        return [thread.anchor_id]
    if thread.anchor_type == "intent":
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE project_id = ? AND intent_id = ? ORDER BY fact_id",
            (thread.project_id, thread.anchor_id),
        ).fetchall()
        return [row["fact_id"] for row in rows] or ["origin"]
    return ["origin"]
