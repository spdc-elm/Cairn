from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from cairn.server.models import AnchorResolution, RemoteSessionProvenance
from cairn.server.questions.context import build_question_context
from cairn.server.questions.models import (
    QuestionClaimJob,
    QuestionClaimResponse,
    QuestionEvent,
    QuestionJob,
    QuestionJobEventPayload,
    QuestionMessage,
    QuestionThread,
    ResolvedQuestionMode,
    SessionEffect,
    SourceSession,
)
from cairn.server.services import dumps_json, loads_json_object, utcnow

ACTIVE_TTL_HOURS = 24
TOMBSTONE_TTL_HOURS = 1
CLAIM_TTL_SECONDS = 60


class QuestionGoneError(KeyError):
    pass


class ResumeLockError(RuntimeError):
    pass


def create_question_thread(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    anchor_type: str,
    anchor_id: str,
    worker_name: str | None,
    execution_environment_id: str | None,
    execution_worker_type: str | None,
    execution_endpoint_id: str | None,
    execution_model_profile_id: str | None,
    anchor_resolution: AnchorResolution,
    mode: ResolvedQuestionMode,
    session_effect: SessionEffect,
    notice: str | None,
) -> QuestionThread:
    now = utcnow()
    expires_at = _future(hours=ACTIVE_TTL_HOURS)
    source_session = anchor_resolution.provenance.remote_session if anchor_resolution.provenance else RemoteSessionProvenance(status="missing")
    thread_id = f"q_{uuid.uuid4().hex}"
    if mode == "resume":
        _acquire_resume_lock(
            conn,
            project_id=project_id,
            thread_id=thread_id,
            remote_session_kind=source_session.kind,
            remote_session_id=source_session.id,
            expires_at=expires_at,
        )
    conn.execute(
        """
        INSERT INTO question_threads (
            id, project_id, anchor_type, anchor_id, source_run_log_id,
            source_remote_session_kind, source_remote_session_id, source_remote_session_status,
            worker_name, execution_environment_id, execution_worker_type, execution_endpoint_id,
            execution_model_profile_id, mode, session_effect, status, notice, created_at, updated_at, expires_at,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            project_id,
            anchor_type,
            anchor_id,
            anchor_resolution.source_run_log_id,
            source_session.kind,
            source_session.id,
            source_session.status,
            worker_name,
            execution_environment_id,
            execution_worker_type,
            execution_endpoint_id,
            execution_model_profile_id,
            mode,
            session_effect,
            notice,
            now,
            now,
            expires_at,
            dumps_json({"anchor_resolution": anchor_resolution.model_dump(mode="json")}),
        ),
    )
    return get_question_thread_detail(conn, project_id, thread_id)


def append_question_message_job(conn: sqlite3.Connection, thread_id: str, message: str) -> QuestionJob:
    thread = _thread_row_or_raise(conn, thread_id)
    if thread["status"] != "active":
        raise HTTPException(409, "question_thread_not_active")
    seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM question_jobs WHERE thread_id = ?", (thread_id,)).fetchone()
    seq = int(seq_row["seq"])
    now = utcnow()
    prompt_context = build_question_context(conn, _thread_from_row(conn, thread, include_children=False), message)
    job = QuestionJob(
        id=f"qjob_{uuid.uuid4().hex}",
        thread_id=thread_id,
        project_id=thread["project_id"],
        seq=seq,
        mode=thread["mode"],
        message=message,
        prompt_context=prompt_context,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    conn.execute(
        """
        INSERT INTO question_jobs (
            id, thread_id, project_id, seq, mode, message, prompt_context_json,
            status, execution_environment_id, execution_worker_type, execution_endpoint_id,
            execution_model_profile_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
        """,
        (
            job.id,
            job.thread_id,
            job.project_id,
            job.seq,
            job.mode,
            job.message,
            dumps_json(prompt_context),
            thread["execution_environment_id"],
            thread["execution_worker_type"],
            thread["execution_endpoint_id"],
            thread["execution_model_profile_id"],
            now,
            now,
        ),
    )
    conn.execute("UPDATE question_threads SET updated_at = ? WHERE id = ?", (now, thread_id))
    return job


def get_question_thread_detail(conn: sqlite3.Connection, project_id: str, thread_id: str) -> QuestionThread:
    row = conn.execute("SELECT * FROM question_threads WHERE project_id = ? AND id = ?", (project_id, thread_id)).fetchone()
    if row is None:
        raise KeyError(thread_id)
    if row["status"] in ("closed", "expired"):
        raise QuestionGoneError(thread_id)
    return _thread_from_row(conn, row, include_children=True)


def close_question_thread(conn: sqlite3.Connection, project_id: str, thread_id: str) -> QuestionThread:
    row = conn.execute("SELECT * FROM question_threads WHERE project_id = ? AND id = ?", (project_id, thread_id)).fetchone()
    if row is None:
        raise KeyError(thread_id)
    if row["status"] in ("closed", "expired"):
        raise QuestionGoneError(thread_id)
    thread = _thread_from_row(conn, row, include_children=True)
    now = utcnow()
    conn.execute(
        "UPDATE question_jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE thread_id = ? AND status IN ('pending','claimed','running')",
        (now, now, thread_id),
    )
    conn.execute("DELETE FROM question_events WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM question_jobs WHERE thread_id = ?", (thread_id,))
    conn.execute(
        "UPDATE question_threads SET status = 'closed', closed_at = ?, expires_at = ?, updated_at = ? WHERE id = ?",
        (now, _future(hours=TOMBSTONE_TTL_HOURS), now, thread_id),
    )
    _release_resume_lock(conn, project_id=project_id, thread_id=thread_id)
    return thread.model_copy(update={"status": "closed", "jobs": [], "events": [], "messages": [], "active_job": None, "updated_at": now}, deep=True)


def claim_question_job(
    conn: sqlite3.Connection,
    *,
    dispatcher_id: str,
    worker_names: list[str],
    environment_ids: list[str] | None = None,
    limit: int = 1,
) -> QuestionClaimResponse:
    _recover_stale_runtime(conn)
    placeholders = ",".join("?" for _ in worker_names)
    params: list[Any] = []
    worker_filter = ""
    if worker_names:
        worker_filter = f"AND (t.worker_name IS NULL OR t.worker_name IN ({placeholders}))"
        params.extend(worker_names)
    env_filter = ""
    if environment_ids:
        env_placeholders = ",".join("?" for _ in environment_ids)
        env_filter = f"AND (j.execution_environment_id IS NULL OR j.execution_environment_id IN ({env_placeholders}))"
        params.extend(environment_ids)
    rows = conn.execute(
        f"""
        SELECT j.*, t.worker_name, t.source_remote_session_kind, t.source_remote_session_id, t.source_remote_session_status
        FROM question_jobs j
        JOIN question_threads t ON t.id = j.thread_id
        WHERE j.status = 'pending'
          AND t.status = 'active'
          {worker_filter}
          {env_filter}
        ORDER BY j.created_at, j.id
        LIMIT ?
        """,
        (*params, max(limit, 50)),
    ).fetchall()
    if not rows:
        return QuestionClaimResponse(job=None)
    selected: tuple[sqlite3.Row, str | None, dict[str, str | None]] | None = None
    for row in rows:
        selected_worker = row["worker_name"] or (worker_names[0] if worker_names else None)
        execution = _resolve_claim_execution(conn, row, selected_worker)
        if not _claim_execution_available(conn, execution):
            continue
        selected = (row, selected_worker, execution)
        break
    if selected is None:
        return QuestionClaimResponse(job=None)
    row, selected_worker, execution = selected
    now = utcnow()
    expires_at = _future(seconds=CLAIM_TTL_SECONDS)
    conn.execute(
        """
        UPDATE question_jobs
        SET status = 'claimed', claimed_by = ?, claimed_at = ?, claim_expires_at = ?, updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (dispatcher_id, now, expires_at, now, row["id"]),
    )
    if selected_worker and row["worker_name"] is None:
        conn.execute("UPDATE question_threads SET worker_name = ?, updated_at = ? WHERE id = ?", (selected_worker, now, row["thread_id"]))
    conn.execute(
        """
        UPDATE question_jobs
        SET execution_environment_id = ?, execution_worker_type = ?, execution_endpoint_id = ?,
            execution_model_profile_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            execution["environment_id"],
            execution["worker_type"],
            execution["endpoint_id"],
            execution["model_profile_id"],
            now,
            row["id"],
        ),
    )
    return QuestionClaimResponse(
        job=QuestionClaimJob(
            id=row["id"],
            thread_id=row["thread_id"],
            project_id=row["project_id"],
            mode=row["mode"],
            worker_name=selected_worker,
            execution_environment_id=execution["environment_id"],
            execution_worker_type=execution["worker_type"],
            execution_endpoint_id=execution["endpoint_id"],
            execution_model_profile_id=execution["model_profile_id"],
            source_session=SourceSession(
                kind=row["source_remote_session_kind"],
                id=row["source_remote_session_id"],
                status=row["source_remote_session_status"],
            ),
            prompt_context=loads_json_object(row["prompt_context_json"]),
            message=row["message"],
        )
    )


def start_question_job(conn: sqlite3.Connection, job_id: str, dispatcher_id: str) -> QuestionJob:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] == "running":
        return _job_from_row(job)
    if job["status"] != "claimed":
        raise HTTPException(409, "job_terminal_state_conflict")
    now = utcnow()
    conn.execute("UPDATE question_jobs SET status = 'running', started_at = ?, updated_at = ? WHERE id = ?", (now, now, job_id))
    return _job_from_row(_job_row_or_raise(conn, job_id))


def heartbeat_question_job(conn: sqlite3.Connection, job_id: str, dispatcher_id: str) -> QuestionJob:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] not in ("claimed", "running"):
        return _job_from_row(job)
    now = utcnow()
    conn.execute(
        "UPDATE question_jobs SET claim_expires_at = ?, updated_at = ? WHERE id = ?",
        (_future(seconds=CLAIM_TTL_SECONDS), now, job_id),
    )
    return _job_from_row(_job_row_or_raise(conn, job_id))


def append_question_events(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    dispatcher_id: str,
    events: list[QuestionJobEventPayload],
) -> list[QuestionEvent]:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] not in ("claimed", "running", "succeeded", "failed"):
        raise HTTPException(409, "job_terminal_state_conflict")
    stored: list[QuestionEvent] = []
    for payload in events:
        existing = conn.execute(
            "SELECT * FROM question_events WHERE job_id = ? AND event_key = ?",
            (job_id, payload.event_key),
        ).fetchone()
        if existing is not None:
            stored.append(_event_from_row(existing))
            continue
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM question_events WHERE thread_id = ?", (job["thread_id"],)).fetchone()
        seq = int(seq_row["seq"])
        now = utcnow()
        event_id = f"qevt_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO question_events (id, thread_id, job_id, seq, event_key, event_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, job["thread_id"], job_id, seq, payload.event_key, dumps_json(payload.event), now),
        )
        stored.append(
            QuestionEvent(
                id=event_id,
                thread_id=job["thread_id"],
                job_id=job_id,
                seq=seq,
                event_key=payload.event_key,
                event=payload.event,
                created_at=now,
            )
        )
    return stored


def finish_question_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    dispatcher_id: str,
    result_text: str | None,
    run_log_id: str | None = None,
    question_session: RemoteSessionProvenance | None = None,
) -> QuestionJob:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] == "succeeded":
        return _job_from_row(job)
    if job["status"] in ("failed", "cancelled"):
        raise HTTPException(409, "job_terminal_state_conflict")
    now = utcnow()
    conn.execute(
        """
        UPDATE question_jobs
        SET status = 'succeeded', finished_at = ?, result_text = ?, run_log_id = ?,
            question_remote_session_kind = ?, question_remote_session_id = ?,
            question_remote_session_status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            now,
            result_text,
            run_log_id,
            question_session.kind if question_session else None,
            question_session.id if question_session else None,
            question_session.status if question_session else None,
            now,
            job_id,
        ),
    )
    _release_resume_lock(conn, project_id=job["project_id"], thread_id=job["thread_id"])
    return _job_from_row(_job_row_or_raise(conn, job_id))


def fail_question_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    dispatcher_id: str,
    error_code: str,
    error_detail: str,
) -> QuestionJob:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] == "failed":
        return _job_from_row(job)
    if job["status"] in ("succeeded", "cancelled"):
        raise HTTPException(409, "job_terminal_state_conflict")
    now = utcnow()
    conn.execute(
        "UPDATE question_jobs SET status = 'failed', finished_at = ?, error_code = ?, error_detail = ?, updated_at = ? WHERE id = ?",
        (now, error_code, error_detail, now, job_id),
    )
    _release_resume_lock(conn, project_id=job["project_id"], thread_id=job["thread_id"])
    return _job_from_row(_job_row_or_raise(conn, job_id))


def requeue_question_job(conn: sqlite3.Connection, *, job_id: str, dispatcher_id: str, error_detail: str | None = None) -> QuestionJob:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] not in ("claimed", "running"):
        return _job_from_row(job)
    now = utcnow()
    conn.execute(
        """
        UPDATE question_jobs
        SET status = 'pending', claimed_by = NULL, claimed_at = NULL, claim_expires_at = NULL,
            error_code = 'job_requeued', error_detail = ?, updated_at = ?
        WHERE id = ?
        """,
        (error_detail, now, job_id),
    )
    return _job_from_row(_job_row_or_raise(conn, job_id))


def cancel_question_job(conn: sqlite3.Connection, *, job_id: str, dispatcher_id: str, error_detail: str | None = None) -> QuestionJob:
    job = _job_row_or_raise(conn, job_id)
    _assert_claim_owner(job, dispatcher_id)
    if job["status"] == "cancelled":
        return _job_from_row(job)
    now = utcnow()
    conn.execute(
        "UPDATE question_jobs SET status = 'cancelled', finished_at = ?, error_code = 'worker_cancelled', error_detail = ?, updated_at = ? WHERE id = ?",
        (now, error_detail, now, job_id),
    )
    _release_resume_lock(conn, project_id=job["project_id"], thread_id=job["thread_id"])
    return _job_from_row(_job_row_or_raise(conn, job_id))


def recover_question_runtime(conn: sqlite3.Connection) -> None:
    _recover_stale_runtime(conn)


def reset_runtime_for_tests(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM question_events")
    conn.execute("DELETE FROM question_jobs")
    conn.execute("DELETE FROM question_resume_locks")
    conn.execute("DELETE FROM question_threads")


def _recover_stale_runtime(conn: sqlite3.Connection) -> None:
    now = utcnow()
    expired = conn.execute(
        "SELECT id, project_id FROM question_threads WHERE status = 'active' AND expires_at <= ?",
        (now,),
    ).fetchall()
    for row in expired:
        conn.execute("UPDATE question_threads SET status = 'expired', updated_at = ? WHERE id = ?", (now, row["id"]))
        _release_resume_lock(conn, project_id=row["project_id"], thread_id=row["id"])
    conn.execute("DELETE FROM question_resume_locks WHERE expires_at <= ?", (now,))
    stale_jobs = conn.execute(
        """
        SELECT j.*, t.mode
        FROM question_jobs j
        JOIN question_threads t ON t.id = j.thread_id
        WHERE j.status IN ('claimed', 'running') AND j.claim_expires_at <= ?
        """,
        (now,),
    ).fetchall()
    for job in stale_jobs:
        if job["mode"] == "resume":
            conn.execute(
                "UPDATE question_jobs SET status = 'failed', error_code = 'failed_requires_manual_retry', error_detail = 'resume claim expired', finished_at = ?, updated_at = ? WHERE id = ?",
                (now, now, job["id"]),
            )
            _release_resume_lock(conn, project_id=job["project_id"], thread_id=job["thread_id"])
        else:
            conn.execute(
                "UPDATE question_jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, claim_expires_at = NULL, updated_at = ? WHERE id = ?",
                (now, job["id"]),
            )


def _thread_from_row(conn: sqlite3.Connection, row: sqlite3.Row, *, include_children: bool) -> QuestionThread:
    metadata = loads_json_object(row["metadata_json"]) or {}
    resolution_payload = metadata.get("anchor_resolution")
    anchor_resolution = AnchorResolution.model_validate(resolution_payload) if resolution_payload else AnchorResolution(
        anchor_type=row["anchor_type"],
        anchor_id=row["anchor_id"],
        source_run_log_id=row["source_run_log_id"],
        status="missing",
    )
    source_session = RemoteSessionProvenance(
        kind=row["source_remote_session_kind"],
        id=row["source_remote_session_id"],
        status=row["source_remote_session_status"],
    )
    jobs: list[QuestionJob] = []
    events: list[QuestionEvent] = []
    messages: list[QuestionMessage] = []
    active_job = None
    if include_children:
        jobs = [_job_from_row(job) for job in conn.execute("SELECT * FROM question_jobs WHERE thread_id = ? ORDER BY seq", (row["id"],)).fetchall()]
        events = [_event_from_row(event) for event in conn.execute("SELECT * FROM question_events WHERE thread_id = ? ORDER BY seq", (row["id"],)).fetchall()]
        messages = _messages_from_jobs(jobs)
        active_job = next((job for job in jobs if job.status in ("pending", "claimed", "running")), None)
    question_session = next((job.question_session for job in reversed(jobs) if job.question_session is not None), None)
    return QuestionThread(
        id=row["id"],
        project_id=row["project_id"],
        anchor_type=row["anchor_type"],
        anchor_id=row["anchor_id"],
        worker_name=row["worker_name"],
        source_run_log_id=row["source_run_log_id"],
        execution_environment_id=row["execution_environment_id"],
        execution_worker_type=row["execution_worker_type"],
        execution_endpoint_id=row["execution_endpoint_id"],
        execution_model_profile_id=row["execution_model_profile_id"],
        anchor_resolution=anchor_resolution,
        source_session=source_session,
        question_session=question_session,
        mode=row["mode"],
        session_effect=row["session_effect"],
        status=row["status"],
        notice=row["notice"],
        messages=messages,
        jobs=jobs,
        events=events,
        active_job=active_job,
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _messages_from_jobs(jobs: list[QuestionJob]) -> list[QuestionMessage]:
    messages: list[QuestionMessage] = []
    for job in jobs:
        messages.append(QuestionMessage(id=f"msg_user_{job.id}", role="user", text=job.message, created_at=job.created_at))
        if job.status == "succeeded":
            messages.append(QuestionMessage(id=f"msg_assistant_{job.id}", role="assistant", text=job.result_text, created_at=job.finished_at or job.updated_at))
        elif job.status == "failed":
            messages.append(QuestionMessage(id=f"msg_system_{job.id}", role="system", text=job.error_detail or job.error_code, created_at=job.finished_at or job.updated_at))
    return messages


def _job_from_row(row: sqlite3.Row) -> QuestionJob:
    session = None
    if row["question_remote_session_status"] or row["question_remote_session_id"] or row["question_remote_session_kind"]:
        session = RemoteSessionProvenance(
            id=row["question_remote_session_id"],
            kind=row["question_remote_session_kind"],
            status=row["question_remote_session_status"] or "unresolved",
        )
    return QuestionJob(
        id=row["id"],
        thread_id=row["thread_id"],
        project_id=row["project_id"],
        seq=row["seq"],
        mode=row["mode"],
        message=row["message"],
        prompt_context=loads_json_object(row["prompt_context_json"]),
        status=row["status"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        claim_expires_at=row["claim_expires_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result_text=row["result_text"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        run_log_id=row["run_log_id"],
        execution_environment_id=row["execution_environment_id"],
        execution_worker_type=row["execution_worker_type"],
        execution_endpoint_id=row["execution_endpoint_id"],
        execution_model_profile_id=row["execution_model_profile_id"],
        question_session=session,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> QuestionEvent:
    return QuestionEvent(
        id=row["id"],
        thread_id=row["thread_id"],
        job_id=row["job_id"],
        seq=row["seq"],
        event_key=row["event_key"],
        event=loads_json_object(row["event_json"]) or {"kind": "raw", "text": row["event_json"]},
        created_at=row["created_at"],
    )


def _thread_row_or_raise(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM question_threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None:
        raise KeyError(thread_id)
    if row["status"] in ("closed", "expired"):
        raise QuestionGoneError(thread_id)
    return row


def _resolve_claim_execution(conn: sqlite3.Connection, row: sqlite3.Row, worker_name: str | None) -> dict[str, str | None]:
    worker_row = conn.execute("SELECT * FROM worker_inventory WHERE name = ?", (worker_name,)).fetchone() if worker_name else None
    return {
        "worker_name": worker_name,
        "environment_id": row["execution_environment_id"],
        "worker_type": row["execution_worker_type"] or (worker_row["type"] if worker_row is not None else None),
        "endpoint_id": row["execution_endpoint_id"] or (worker_row["endpoint"] if worker_row is not None else None),
        "model_profile_id": row["execution_model_profile_id"] or (worker_row["model_profile"] if worker_row is not None else None),
    }


def _claim_execution_available(conn: sqlite3.Connection, execution: dict[str, str | None]) -> bool:
    if not execution["environment_id"] or not execution["worker_type"]:
        return True
    row = conn.execute(
        """
        SELECT status, disabled_until, stale_after FROM worker_runtime_health
        WHERE environment_id = ? AND worker_name = ? AND worker_type = ?
          AND endpoint_id = ? AND model_profile_id = ?
        """,
        (
            execution["environment_id"],
            execution.get("worker_name") or "",
            execution["worker_type"],
            _identity_text(execution["endpoint_id"]),
            _identity_text(execution["model_profile_id"]),
        ),
    ).fetchone()
    if row is None:
        return True
    now = utcnow()
    if row["stale_after"] and row["stale_after"] <= now:
        return True
    return not (row["status"] == "unhealthy" and (row["disabled_until"] is None or row["disabled_until"] > now))


def _identity_text(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _job_row_or_raise(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM question_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return row


def _assert_claim_owner(job: sqlite3.Row, dispatcher_id: str) -> None:
    if job["claimed_by"] != dispatcher_id:
        raise HTTPException(409, "job_claim_expired")


def _acquire_resume_lock(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    thread_id: str,
    remote_session_kind: str | None,
    remote_session_id: str | None,
    expires_at: str,
) -> None:
    if not remote_session_kind or not remote_session_id:
        raise ResumeLockError("source_session_missing")
    now = utcnow()
    conn.execute("DELETE FROM question_resume_locks WHERE expires_at <= ?", (now,))
    existing = conn.execute(
        """
        SELECT thread_id FROM question_resume_locks
        WHERE project_id = ? AND remote_session_kind = ? AND remote_session_id = ?
        """,
        (project_id, remote_session_kind, remote_session_id),
    ).fetchone()
    if existing is not None:
        raise ResumeLockError("resume_lock_conflict")
    conn.execute(
        """
        INSERT INTO question_resume_locks (
            project_id, remote_session_kind, remote_session_id, thread_id, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, remote_session_kind, remote_session_id, thread_id, expires_at, now),
    )


def _release_resume_lock(conn: sqlite3.Connection, *, project_id: str, thread_id: str) -> None:
    conn.execute("DELETE FROM question_resume_locks WHERE project_id = ? AND thread_id = ?", (project_id, thread_id))


def _future(*, hours: int = 0, seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def terminal_digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
