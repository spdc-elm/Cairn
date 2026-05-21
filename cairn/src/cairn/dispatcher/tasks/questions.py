from __future__ import annotations

import json
import logging
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.tasks.common import run_worker_process, _worker_secrets
from cairn.dispatcher.workers.registry import get_driver
from cairn.shared.api_models import ProjectDetail

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

    LOG.warning("legacy question job ignored; v3.2 uses execution question jobs")
    return "rejected"


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

    sink = ExecutionEventSink(
        client,
        execution_id,
        secrets=_worker_secrets(worker),
        event_projector=driver.stream_event_projector(execution_id),
    )
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
