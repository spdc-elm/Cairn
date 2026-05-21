from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.redaction import redact_text
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle, WorkEnvironment
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.event_sink import CompositeRunLogger, ExecutionEventSink
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.runtime.run_logs import RunLogWriter
from cairn.shared.worker_events import message_event, status_event

HEALTHCHECK_COMMUNICATE_GRACE_SECONDS = 10
PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class HealthcheckRun:
    result: ProcessResult
    duration_ms: int


@dataclass(slots=True)
class WorkerProcessRun:
    result: ProcessResult
    run_log_id: str | None = None
    run_log_path: str | None = None
    event_flush_failed: bool = False
    event_sink: ExecutionEventSink | None = None

    def __getattr__(self, name: str):
        return getattr(self.result, name)


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def _process_error_code(result: ProcessResult) -> str | None:
    if result.cancelled:
        return "cancelled"
    if did_timeout(result):
        return "timeout"
    if result.returncode != 0:
        return "worker_process_failed"
    return None


def _process_error_detail(result: ProcessResult) -> str | None:
    if result.cancel_reason:
        return result.cancel_reason
    if result.returncode == 0 and not result.timed_out:
        return None
    return preview(result.stderr) or preview(result.stdout) or None


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def worker_health_from_healthcheck(
    environment: WorkEnvironment,
    worker: WorkerConfig,
    healthcheck: HealthcheckRun,
    *,
    status: str,
    source: str,
    dispatcher_id: str = "dispatcher",
    command: str | None = None,
) -> dict:
    secrets = _worker_secrets(worker)
    return {
        "environment_id": environment.id,
        "worker_name": worker.name,
        "worker_type": worker.type,
        "endpoint_id": worker.endpoint,
        "model_profile_id": worker.model_profile,
        "status": status,
        "checked_at": utcnow(),
        "source": source,
        "dispatcher_id": dispatcher_id,
        "detail": {
            "returncode": healthcheck.result.returncode,
            "duration_ms": healthcheck.duration_ms,
            "response_preview": redact_text(preview(healthcheck.result.stdout), secrets),
            "stderr_preview": redact_text(preview(healthcheck.result.stderr), secrets),
            "command": redact_text(command or "", secrets),
        },
    }


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def write_graph_snapshot_reference(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{environment.graph_snapshot_path(handle, phase)}-{uuid.uuid4().hex[:12]}/graph.yaml"
    environment.write_text_file(handle, path, graph_yaml)
    return (
        "The graph YAML snapshot is stored in this file inside the current work environment:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def run_healthcheck(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    worker: WorkerConfig,
    command: list[str],
    *,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> HealthcheckRun:
    process = environment.build_process(
        handle,
        dict(worker.env),
        command,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    started = time.perf_counter()
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds, HEALTHCHECK_COMMUNICATE_GRACE_SECONDS))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return HealthcheckRun(result=result, duration_ms=duration_ms)


def run_worker_process(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    project_id: str | None = None,
    task_type: str | None = None,
    intent_id: str | None = None,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    extra_metadata: dict | None = None,
    event_sink: ExecutionEventSink | None = None,
    close_event_sink_on_finish: bool = True,
) -> WorkerProcessRun:
    LOG.info(
        "starting work environment process environment=%s backend=%s target=%s worker=%s phase=%s timeout=%ss",
        environment.id,
        environment.backend,
        handle.target_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    jsonl_logger = None
    if project_id is not None and task_type is not None:
        run_metadata = {
            "container": handle.target_name if environment.backend == "docker" else None,
            "environment_id": environment.id,
            "backend": environment.backend,
            "target": handle.target_name,
            "workspace": handle.workspace,
            "model_profile_id": worker.model_profile,
            "endpoint_id": worker.endpoint,
            "timeout_seconds": timeout_seconds,
            "argv0": argv[0] if argv else None,
        } | (extra_metadata or {})
        jsonl_logger = RunLogWriter(
            project_id=project_id,
            task_type=task_type,
            phase=phase,
            worker_name=worker.name,
            intent_id=intent_id,
            metadata=run_metadata,
            secrets=_worker_secrets(worker),
        )
        LOG.info(
            "run log opened project=%s intent=%s worker=%s phase=%s run_id=%s path=%s",
            project_id,
            intent_id,
            worker.name,
            phase,
            jsonl_logger.run_id,
            jsonl_logger.path,
        )
    if event_sink is not None:
        event_sink.write_status("running")
    run_logger = CompositeRunLogger(jsonl_logger, event_sink)
    process = environment.build_process(
        handle,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
        run_logger=run_logger,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds))
        event_flush_failed = False
        if jsonl_logger is not None or event_sink is not None:
            run_logger.finish(
                returncode=result.returncode,
                timed_out=result.timed_out,
                cancelled=result.cancelled,
                cancel_reason=result.cancel_reason,
            )
            if event_sink is not None and close_event_sink_on_finish:
                if lease is not None and lease.failure is not None:
                    event_sink.write_event(
                        message_event(
                            "system",
                            f"dispatcher diagnostic: heartbeat_cancelled status={lease.failure.status_code} detail={lease.failure.text}",
                            event_key=f"{event_sink.execution_id}:diagnostic:heartbeat-cancelled",
                        )
                    )
                terminal_status = "cancelled" if result.cancelled else ("succeeded" if result.returncode == 0 else "failed")
                event_flush_failed = not event_sink.close(
                    terminal_status=terminal_status,
                    returncode=result.returncode,
                    error_code=_process_error_code(result),
                    error_detail=_process_error_detail(result),
                )
        return WorkerProcessRun(
            result=result,
            run_log_id=jsonl_logger.run_id if jsonl_logger is not None else None,
            run_log_path=str(jsonl_logger.path) if jsonl_logger is not None else None,
            event_flush_failed=event_flush_failed,
            event_sink=event_sink,
        )
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def record_remote_session(
    client: CairnClient,
    project_id: str,
    run: WorkerProcessRun,
    driver,
    prepared_session: str | None,
    execution_id: str | None = None,
) -> str | None:
    session = driver.extract_session_provenance(prepared_session, run.stdout, run.stderr)
    if execution_id is not None:
        response = client.patch_execution(
            execution_id,
            {
                "remote_session_out_kind": session.kind,
                "remote_session_out_id": session.id,
                "remote_session_out_status": session.status,
            },
        )
        if not response.ok:
            LOG.warning(
                "execution session update failed project=%s execution=%s status=%s body=%s",
                project_id,
                execution_id,
                response.status_code,
                response.text,
            )
    return session.id


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    title: str | None = None,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    metadata: dict | None = None,
    execution_id: str | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        title=title,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        metadata=metadata,
        execution_id=execution_id,
    ).status


def _worker_secrets(worker: WorkerConfig) -> list[str]:
    return [
        value
        for key, value in worker.env.items()
        if value and ("KEY" in key or "TOKEN" in key or "SECRET" in key)
    ]


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    title: str | None = None,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    metadata: dict | None = None,
    execution_id: str | None = None,
) -> ConcludeWriteResult:
    if execution_id is not None:
        response = client.submit_execution_conclusion_report(
            execution_id,
            {
                "title": title,
                "description": description,
                "metadata": metadata,
            },
        )
    else:
        response = client.conclude(project_id, intent_id, worker_name, description, title=title, metadata=metadata)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def finish_deferred_worker_process(
    client: CairnClient,
    execution_id: str | None,
    run: WorkerProcessRun,
    terminal_status: str,
    *,
    returncode: int | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    if execution_id is None:
        return
    if run.event_sink is not None:
        ok = run.event_sink.close(
            terminal_status=terminal_status,
            returncode=run.returncode if returncode is None else returncode,
            error_code=error_code,
            error_detail=error_detail,
        )
        run.event_flush_failed = run.event_flush_failed or not ok
        return
    response = finish_execution_terminal(
        client,
        execution_id,
        events=[
            status_event(
                terminal_status,
                event_key=f"{execution_id}:status:deferred-terminal:{uuid.uuid4().hex[:8]}",
                error_code=error_code,
                error_detail=error_detail,
            ).to_api_payload()
        ],
        patch={
            "status": terminal_status,
            "returncode": run.returncode if returncode is None else returncode,
            "error_code": error_code,
            "error_detail": error_detail,
        },
    )
    if not response.ok:
        LOG.warning(
            "deferred execution finish failed execution=%s status=%s body=%s",
            execution_id,
            response.status_code,
            response.text,
        )


def flush_deferred_worker_process_events(run: WorkerProcessRun) -> None:
    if run.event_sink is None:
        return
    ok = run.event_sink.close()
    run.event_flush_failed = run.event_flush_failed or not ok


def best_effort_release_after_conclude_failure(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
) -> None:
    best_effort_release(client, project_id, intent_id, worker_name)


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )


def finish_execution_terminal(
    client: CairnClient,
    execution_id: str,
    *,
    events: list[dict[str, Any]],
    patch: dict[str, Any],
    dispatcher_id: str = "dispatcher",
):
    finish = getattr(client, "finish_execution", None)
    if callable(finish):
        return finish(
            execution_id,
            dispatcher_id=dispatcher_id,
            events=events,
            patch={key: value for key, value in patch.items() if key != "dispatcher_id"},
        )
    append = client.append_execution_events(execution_id, dispatcher_id=dispatcher_id, events=events)
    if not append.ok:
        return append
    patch_payload = {"dispatcher_id": dispatcher_id, **patch}
    return client.patch_execution(execution_id, patch_payload)
