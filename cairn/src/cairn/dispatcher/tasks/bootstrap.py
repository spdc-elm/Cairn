from __future__ import annotations

import logging
import time
import uuid

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
)
from cairn.dispatcher.models import TaskOutcome
from cairn.dispatcher.prompting import format_hints, load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    best_effort_release_after_conclude_failure,
    cancel_reason,
    did_timeout,
    finish_deferred_worker_process,
    finish_execution_terminal,
    flush_deferred_worker_process_events,
    project_allows_conclude_fallback,
    preview,
    record_remote_session,
    run_healthcheck,
    run_worker_process,
    worker_health_from_healthcheck,
    write_conclude_result,
    write_conclude_result_with_fact_id,
    _worker_secrets,
)
from cairn.dispatcher.tasks.reports import metadata_for_worker_fact
from cairn.dispatcher.workers.registry import get_driver
from cairn.shared.worker_events import message_event, status_event
from cairn.shared.api_models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
    execution_id: str | None = None,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = (
        HeartbeatLease.for_execution(client, execution_id, worker.name, config.runtime.interval)
        if execution_id is not None
        else HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    )
    lease.start()
    try:
        handle = environment.prepare_project(project.project.id)

        LOG.info(
            "starting work environment process project=%s intent=%s environment=%s backend=%s worker=%s phase=bootstrap_healthcheck timeout=%ss",
            project.project.id,
            intent.id,
            environment.id,
            environment.backend,
            worker.name,
            healthcheck_timeout,
        )
        healthcheck = run_healthcheck(
            environment,
            handle,
            worker,
            driver.build_healthcheck(worker),
            timeout_seconds=healthcheck_timeout,
            lease=lease,
            cancellation=cancellation,
        )
        cancelled = cancel_reason(healthcheck.result, cancellation)
        if cancelled is not None:
            LOG.info(
                "bootstrap cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
            )
            if cancelled == "conclude_requested":
                LOG.info(
                    "bootstrap conclude unavailable before worker session project=%s intent=%s worker=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                )
                best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
            else:
                best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap healthcheck project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if healthcheck.result.returncode != 0:
            LOG.warning(
                "worker unhealthy project=%s intent=%s worker=%s healthcheck_ms=%s stderr=%s",
                project.project.id,
                intent.id,
                worker.name,
                healthcheck.duration_ms,
                preview(healthcheck.result.stderr),
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return TaskOutcome(
                "unhealthy",
                worker_health=worker_health_from_healthcheck(
                    environment,
                    worker,
                    healthcheck,
                    status="unhealthy",
                    source="runtime_healthcheck",
                ),
            )

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project),
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = run_worker_process(
            environment,
            handle,
            worker,
            execute.argv,
            phase="bootstrap",
            timeout_seconds=config.tasks.bootstrap.timeout,
            project_id=project.project.id,
            task_type="bootstrap",
            intent_id=intent.id,
            lease=lease,
            cancellation=cancellation,
            event_sink=(
                ExecutionEventSink(
                    client,
                    execution_id,
                    secrets=_worker_secrets(worker),
                    event_projector=driver.stream_event_projector(execution_id),
                )
                if execution_id is not None
                else None
            ),
            close_event_sink_on_finish=False,
        )
        flush_deferred_worker_process_events(first)
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = record_remote_session(client, project.project.id, first, driver, session, execution_id=execution_id)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            if cancelled == "conclude_requested":
                LOG.info(
                    "bootstrap conclude requested project=%s intent=%s worker=%s execute_ms=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                )
                outcome = _try_conclude_fallback(
                    config,
                    client,
                    environment,
                    handle,
                    worker,
                    driver,
                    project,
                    intent,
                    session,
                    lease,
                    cancellation,
                    execution_id=execution_id,
                )
                _finish_after_bootstrap_fallback(client, execution_id, first, outcome)
                return outcome
            LOG.info(
                "bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            finish_deferred_worker_process(
                client,
                execution_id,
                first,
                "cancelled",
                error_code="cancelled",
                error_detail=cancelled,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            finish_deferred_worker_process(
                client,
                execution_id,
                first,
                "failed",
                error_code="heartbeat_lost",
                error_detail=f"heartbeat lost during bootstrap: status={lease.failure.status_code} {preview(lease.failure.text)}",
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, data = validate_bootstrap_execute_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "bootstrap parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                outcome = _try_conclude_fallback(
                    config,
                    client,
                    environment,
                    handle,
                    worker,
                    driver,
                    project,
                    intent,
                    session,
                    lease,
                    cancellation,
                    execution_id=execution_id,
                )
                if outcome == "success":
                    finish_deferred_worker_process(client, execution_id, first, "succeeded", returncode=first.returncode)
                elif outcome == "cancelled":
                    finish_deferred_worker_process(
                        client,
                        execution_id,
                        first,
                        "cancelled",
                        error_code="cancelled",
                        error_detail="worker execution was cancelled before bootstrap conclude fallback completed",
                    )
                return outcome
            if kind == "rejected":
                LOG.warning(
                    "bootstrap rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                finish_deferred_worker_process(
                    client,
                    execution_id,
                    first,
                    "failed",
                    error_code="bootstrap_rejected",
                    error_detail="worker returned rejected instead of an accepted bootstrap fact",
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            outcome = _write_bootstrap_complete_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                data["fact_title"],
                data["fact_description"],
                data["complete_description"],
                source="bootstrap",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
                producing_run_log_id=first.run_log_id,
                execution_id=execution_id,
            )
            if outcome == "success":
                finish_deferred_worker_process(client, execution_id, first, "succeeded", returncode=first.returncode)
            else:
                _mark_execution_postprocess_failed(client, execution_id, "bootstrap_write_failed", "bootstrap fact write failed")
            return outcome
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            outcome = _try_conclude_fallback(
                config,
                client,
                environment,
                handle,
                worker,
                driver,
                project,
                intent,
                session,
                lease,
                cancellation,
                execution_id=execution_id,
            )
            if outcome == "success":
                finish_deferred_worker_process(client, execution_id, first, "succeeded", returncode=first.returncode)
            elif outcome == "cancelled":
                finish_deferred_worker_process(
                    client,
                    execution_id,
                    first,
                    "cancelled",
                    error_code="cancelled",
                    error_detail="worker execution was cancelled before bootstrap conclude fallback completed",
                )
            return outcome
        LOG.warning(
            "bootstrap command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        finish_deferred_worker_process(
            client,
            execution_id,
            first,
            "failed",
            error_code="worker_process_failed",
            error_detail=preview(first.stderr) or preview(first.stdout) or "bootstrap worker process failed",
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("bootstrap task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    handle,
    worker: WorkerConfig,
    driver,
    project: ProjectDetail,
    intent: Intent,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    execution_id: str | None = None,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "bootstrap conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        _mark_execution_postprocess_failed(
            client,
            execution_id,
            "bootstrap_conclude_unavailable",
            "conclude fallback unavailable before a usable bootstrap result was produced",
        )
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning(
            "bootstrap conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
            project.project.id,
            intent.id,
            worker.name,
        )
        _mark_execution_postprocess_failed(client, execution_id, "heartbeat_lost", "heartbeat lost before bootstrap conclude fallback")
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled and cancellation.reason != "conclude_requested":
        LOG.info(
            "bootstrap conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project.project.id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        _mark_execution_postprocess_failed(client, execution_id, "project_not_active", "project became inactive before bootstrap conclude fallback")
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "failed"

    handle = environment.prepare_project(project.project.id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
        _bootstrap_prompt_replacements(project),
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting bootstrap conclude fallback project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = run_worker_process(
        environment,
        handle,
        worker,
        conclude_argv,
        phase="bootstrap_conclude",
        timeout_seconds=config.tasks.bootstrap.conclude_timeout,
        project_id=project.project.id,
        task_type="bootstrap",
        intent_id=intent.id,
        lease=lease,
        cancellation=None if cancellation.reason == "conclude_requested" else cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    session = record_remote_session(client, project.project.id, result, driver, session)
    conclude_cancellation = None if cancellation.reason == "conclude_requested" else cancellation
    cancelled = cancel_reason(result, conclude_cancellation)
    if cancelled is not None:
        LOG.info(
            "bootstrap conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        _mark_execution_postprocess_failed(client, execution_id, "heartbeat_lost", "heartbeat lost during bootstrap conclude fallback")
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "bootstrap conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        _mark_execution_postprocess_failed(
            client,
            execution_id,
            "bootstrap_conclude_process_failed",
            preview(result.stderr) or preview(result.stdout) or "bootstrap conclude process failed",
        )
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        conclude_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(conclude_data, dict) and isinstance(conclude_data.get("complete"), dict):
            LOG.warning(
                "bootstrap conclude returned unexpected complete payload project=%s intent=%s worker=%s complete_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                preview(str(conclude_data.get("complete"))),
            )
        kind, fact_data = validate_bootstrap_conclude_payload(payload)
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        _mark_execution_postprocess_failed(client, execution_id, "bootstrap_conclude_parse_failed", str(exc))
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "bootstrap conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        _mark_execution_postprocess_failed(client, execution_id, "bootstrap_conclude_rejected", "bootstrap conclude returned rejected")
        best_effort_release_after_conclude_failure(client, project.project.id, intent.id, worker.name)
        return "rejected"
    outcome = write_conclude_result(
        client,
        project.project.id,
        intent.id,
        worker.name,
        fact_data["description"],
        title=fact_data["title"],
        source="bootstrap_conclude",
        phase_ms=conclude_ms,
        metadata=metadata_for_worker_fact(worker.name, intent.id, producing_run_log_id=result.run_log_id),
        execution_id=execution_id,
    )
    if outcome != "success":
        _mark_execution_postprocess_failed(client, execution_id, "bootstrap_conclude_write_failed", "bootstrap conclude fact write failed")
    return outcome


def _append_execution_diagnostic(client: CairnClient, execution_id: str | None, message: str) -> None:
    if not execution_id:
        return
    response = client.append_execution_events(
        execution_id,
        dispatcher_id="dispatcher",
        events=[
            message_event(
                "system",
                message,
                event_key=f"{execution_id}:dispatcher-diagnostic:{uuid.uuid4().hex[:8]}",
            ).to_api_payload()
        ],
    )
    if not response.ok:
        LOG.warning(
            "bootstrap execution diagnostic append failed execution=%s status=%s body=%s",
            execution_id,
            response.status_code,
            response.text,
        )


def _mark_execution_postprocess_failed(
    client: CairnClient,
    execution_id: str | None,
    error_code: str,
    error_detail: str,
) -> None:
    if not execution_id:
        return
    _append_execution_diagnostic(client, execution_id, f"dispatcher postprocess failed: {error_code}: {error_detail}")
    response = finish_execution_terminal(
        client,
        execution_id,
        events=[
            status_event(
                "failed",
                event_key=f"{execution_id}:status:postprocess-failed:{uuid.uuid4().hex[:8]}",
                error_code=error_code,
                error_detail=error_detail,
            ).to_api_payload()
        ],
        patch={
            "status": "failed",
            "error_code": error_code,
            "error_detail": error_detail,
        },
    )
    if not response.ok:
        LOG.warning(
            "bootstrap execution postprocess finish failed execution=%s status=%s body=%s",
            execution_id,
            response.status_code,
            response.text,
        )


def _bootstrap_prompt_replacements(project: ProjectDetail) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints),
    }


def _write_bootstrap_complete_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    fact_title: str | None,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    producing_run_log_id: str | None = None,
    execution_id: str | None = None,
) -> str:
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        fact_description,
        title=fact_title,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        metadata=metadata_for_worker_fact(worker_name, intent_id, producing_run_log_id=producing_run_log_id),
        execution_id=execution_id,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s intent=%s worker=%s source=%s",
            project_id,
            intent_id,
            worker_name,
            source,
        )
        return "success"

    response = client.complete(project_id, [conclude.fact_id], complete_description, worker_name)
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        return "success"
    if total_ms is None:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
        )
    else:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"


def _finish_after_bootstrap_fallback(
    client: CairnClient,
    execution_id: str | None,
    run,
    outcome: str,
) -> None:
    if outcome == "success":
        finish_deferred_worker_process(client, execution_id, run, "succeeded", returncode=run.returncode)
    elif outcome == "cancelled":
        finish_deferred_worker_process(
            client,
            execution_id,
            run,
            "cancelled",
            error_code="cancelled",
            error_detail="worker execution was cancelled before bootstrap conclude fallback completed",
        )
