from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.tasks.common import HttpRunProvenanceRecorder, run_worker_process
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail
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
    dispatcher_id = "dispatcher"
    job_id = job["id"]
    started = client.start_question_job(job_id, dispatcher_id)
    if not started.ok:
        LOG.warning("question start failed job=%s status=%s body=%s", job_id, started.status_code, started.text)
        return "rejected"
    driver = get_driver(worker.type)
    mode = job["mode"]
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
