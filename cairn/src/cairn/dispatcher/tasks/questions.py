from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.tasks.common import HttpRunProvenanceRecorder, run_worker_process, _worker_secrets
from cairn.dispatcher.workers.registry import get_driver
from cairn.shared.api_models import ProjectDetail
from cairn.server.transcripts import build_transcript_from_streams

LOG = logging.getLogger(__name__)


def run_question_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    project: ProjectDetail,
    worker: WorkerConfig,
    job: dict[str, Any],
    cancellation: TaskCancellation,
) -> str:
    if job.get("task_type") == "question" and job.get("id"):
        return _run_question_execution_task(config, client, environment, project, worker, job, cancellation)

    dispatcher_id = "dispatcher"
    job_id = job["id"]
    started = client.start_question_job(job_id, dispatcher_id)
    if not started.ok:
        LOG.warning("question start failed job=%s status=%s body=%s", job_id, started.status_code, started.text)
        return "rejected"
    driver = get_driver(worker.type)
    mode = job["mode"]
    execution_id = job.get("execution_id")
    source_session = (job.get("source_session") or {}).get("id")
    prompt = _prompt_from_job(job)
    try:
        driver_result = driver.build_question(worker, mode=mode, prompt=prompt, source_session=source_session)
    except Exception as exc:
        client.fail_question_job(job_id, dispatcher_id, "worker_not_available", str(exc))
        return "rejected"

    try:
        handle = environment.prepare_project(project.project.id)
        run = run_worker_process(
            environment,
            handle,
            worker,
            driver_result.argv,
            phase=f"question_{mode}",
            timeout_seconds=300,
            project_id=project.project.id if mode == "resume" else None,
            task_type="question" if mode == "resume" else None,
            intent_id=None,
            cancellation=cancellation,
            provenance_recorder=HttpRunProvenanceRecorder(client) if mode == "resume" else None,
            event_sink=ExecutionEventSink(client, execution_id) if execution_id else None,
            extra_metadata={
                "question_mode": mode,
                "question_anchor_type": (job.get("prompt_context") or {}).get("anchor", {}).get("type"),
                "question_anchor_id": (job.get("prompt_context") or {}).get("anchor", {}).get("id"),
                "source_run_log_id": (job.get("prompt_context") or {}).get("source_run_log_id"),
                "source_remote_session_id": source_session,
                "session_effect": "continued" if mode == "resume" else ("forked" if mode == "fork" else "fresh"),
            },
        )
    except Exception as exc:
        client.fail_question_job(job_id, dispatcher_id, "worker_process_failed", str(exc))
        return "error"

    session = driver.extract_session_provenance(driver_result.session, run.stdout, run.stderr)
    if execution_id:
        client.patch_execution(
            execution_id,
            {
                "remote_session_out_kind": session.kind,
                "remote_session_out_id": session.id,
                "remote_session_out_status": session.status,
            },
        )
    transcript = build_transcript_from_streams(
        run.stdout,
        run.stderr,
        project_id=project.project.id,
        run_log_id=run.run_log_id or job_id,
        worker_type=worker.type,
        limit_events=200,
    )
    events = [
        {
            "event_key": event.id or f"{run.run_log_id or job_id}:{idx}",
            "event": event.model_dump(mode="json"),
        }
        for idx, event in enumerate(transcript.events)
    ]
    if events:
        client.append_question_events(job_id, dispatcher_id, f"batch_{uuid.uuid4().hex}", events)
    answer = driver.extract_response_text(run.stdout, run.stderr).strip() or run.stderr.strip() or run.stdout.strip()
    if run.returncode != 0:
        client.fail_question_job(job_id, dispatcher_id, "worker_process_failed", answer or f"worker exited {run.returncode}")
        return "error"
    if execution_id and answer:
        client.append_execution_events(
            execution_id,
            dispatcher_id=dispatcher_id,
            events=[
                {
                    "event_type": "message",
                    "role": "assistant",
                    "payload": {"text": answer},
                    "event_key": f"{execution_id}:assistant:final",
                }
            ],
        )
    finish = client.finish_question_job(
        job_id,
        dispatcher_id,
        result_text=answer,
        run_log_id=run.run_log_id,
        question_remote_session={
            "id": session.id,
            "kind": session.kind,
            "status": session.status,
            "capture_method": session.capture_method,
        },
    )
    if not finish.ok:
        LOG.warning("question finish failed job=%s status=%s body=%s", job_id, finish.status_code, finish.text)
        return "error"
    return "success"


def _run_question_execution_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    project: ProjectDetail,
    worker: WorkerConfig,
    execution: dict[str, Any],
    cancellation: TaskCancellation,
) -> str:
    dispatcher_id = "dispatcher"
    execution_id = execution["id"]
    session_action = execution.get("session_action") or "fresh_context"
    mode = _mode_from_session_action(session_action)
    source_session = execution.get("remote_session_in_id")
    prompt = _prompt_from_execution(execution)
    driver = get_driver(worker.type)
    try:
        driver_result = driver.build_question(worker, mode=mode, prompt=prompt, source_session=source_session)
    except Exception as exc:
        client.patch_execution(
            execution_id,
            {
                "dispatcher_id": dispatcher_id,
                "status": "failed",
                "error_code": "worker_not_available",
                "error_detail": str(exc),
            },
        )
        return "rejected"

    sink = ExecutionEventSink(client, execution_id, secrets=_worker_secrets(worker))
    client.patch_execution(execution_id, {"dispatcher_id": dispatcher_id, "status": "running"})
    try:
        handle = environment.prepare_project(project.project.id)
        run = run_worker_process(
            environment,
            handle,
            worker,
            driver_result.argv,
            phase=f"question_{mode}",
            timeout_seconds=300,
            project_id=project.project.id,
            task_type="question",
            intent_id=None,
            cancellation=cancellation,
            event_sink=sink,
            close_event_sink_on_finish=False,
            extra_metadata={
                "branch_id": execution.get("branch_id"),
                "session_action": session_action,
                "source_remote_session_id": source_session,
                "session_effect": "continued" if mode == "resume" else ("forked" if mode == "fork" else "fresh"),
            },
        )
    except Exception as exc:
        client.patch_execution(
            execution_id,
            {
                "dispatcher_id": dispatcher_id,
                "status": "failed",
                "error_code": "worker_process_failed",
                "error_detail": str(exc),
            },
        )
        return "error"

    session = driver.extract_session_provenance(driver_result.session, run.stdout, run.stderr)
    client.patch_execution(
        execution_id,
        {
            "dispatcher_id": dispatcher_id,
            "remote_session_out_kind": session.kind,
            "remote_session_out_id": session.id,
            "remote_session_out_status": session.status,
        },
    )
    answer = driver.extract_response_text(run.stdout, run.stderr).strip() or run.stderr.strip() or run.stdout.strip()
    if answer:
        client.append_execution_events(
            execution_id,
            dispatcher_id=dispatcher_id,
            events=[
                {
                    "event_type": "message",
                    "role": "assistant",
                    "payload": {"text": answer},
                    "event_key": f"{execution_id}:assistant:final",
                }
            ],
        )
    terminal_status = "cancelled" if run.cancelled else ("succeeded" if run.returncode == 0 else "failed")
    sink.close(
        terminal_status=terminal_status,
        returncode=run.returncode,
        error_code="timeout" if run.timed_out else None,
        error_detail=run.cancel_reason,
    )
    if run.returncode != 0:
        return "error"
    return "success"


def _mode_from_session_action(session_action: str) -> str:
    if session_action == "fork_initial":
        return "fork"
    if session_action in {"resume_continue", "branch_continue"}:
        return "resume"
    return "fresh_context"


def _prompt_from_execution(execution: dict[str, Any]) -> str:
    snapshot = execution.get("input_snapshot") or {}
    return "\n".join(
        [
            "You are answering a short interactive Cairn follow-up question.",
            "Do not modify Cairn unless the user's question explicitly asks for writes.",
            "",
            "Execution JSON:",
            json.dumps(execution, ensure_ascii=False, sort_keys=True, indent=2),
            "",
            "User question:",
            snapshot.get("message") or "",
        ]
    )


def _prompt_from_job(job: dict[str, Any]) -> str:
    context = job.get("prompt_context") or {}
    return "\n".join(
        [
            "You are answering a short interactive Cairn follow-up question.",
            "Do not modify Cairn unless the user's question explicitly asks for writes.",
            "",
            "Context JSON:",
            json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2),
            "",
            "User question:",
            job.get("message") or context.get("user_message") or "",
        ]
    )
