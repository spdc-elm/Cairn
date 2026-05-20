from __future__ import annotations

import logging
import time
import uuid

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_explore_payload
from cairn.dispatcher.models import TaskOutcome
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle, WorkEnvironment
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    best_effort_release_after_conclude_failure,
    cancel_reason,
    did_timeout,
    HttpRunProvenanceRecorder,
    project_allows_conclude_fallback,
    preview,
    record_remote_session,
    run_healthcheck,
    run_worker_process,
    worker_health_from_healthcheck,
    write_conclude_result,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.tasks.reports import (
    build_report_path,
    metadata_for_report,
    validate_report_written,
    write_failure_report,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_explore_task(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    project: ProjectDetail,
    export_yaml: str,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation | None,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        handle = environment.prepare_project(project.project.id)
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        report_path = build_report_path(handle, intent.id, run_id)

        LOG.info(
            "starting work environment process project=%s intent=%s environment=%s backend=%s worker=%s phase=explore_healthcheck timeout=%ss",
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
                "explore cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
            )
            if cancelled == "conclude_requested":
                _write_failure_and_release(
                    environment,
                    handle,
                    client,
                    project.project.id,
                    intent.id,
                    worker.name,
                    report_path,
                    run_id,
                    "conclude_unavailable",
                    "conclude requested before worker session was established",
                )
            else:
                best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during explore healthcheck project=%s intent=%s worker=%s status=%s",
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
            load_prompt(config.runtime.prompt_group, "explore.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    environment,
                    handle,
                    export_yaml.strip(),
                    phase="explore_execute",
                ),
                "intent_id": intent.id,
                "intent_description": intent.description,
                "report_path": report_path,
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = _run_process(
            environment,
            handle,
            worker,
            execute.argv,
            phase="explore_execute",
            timeout=_effective_timeout(config, project, intent),
            project_id=project.project.id,
            intent_id=intent.id,
            report_path=report_path,
            run_id=run_id,
            control_state=intent.control_state,
            lease=lease,
            cancellation=cancellation,
            client=client,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = record_remote_session(client, project.project.id, first, driver, session)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            if cancelled == "conclude_requested":
                LOG.info(
                    "explore conclude requested project=%s intent=%s worker=%s execute_ms=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                )
                return _try_conclude_fallback(
                    config,
                    client,
                    environment,
                    handle,
                    worker,
                    driver,
                    project.project.id,
                    intent,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                    report_path,
                    run_id,
                )
            LOG.info(
                "explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, fact_data = validate_explore_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "explore parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                return _try_conclude_fallback(
                    config,
                    client,
                    environment,
                    handle,
                    worker,
                    driver,
                    project.project.id,
                    intent,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                    report_path,
                    run_id,
                )
            if kind == "rejected":
                LOG.warning(
                    "explore rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            if not validate_report_written(environment, handle, report_path):
                LOG.warning(
                    "explore report missing project=%s intent=%s worker=%s report_path=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    report_path,
                )
                return _try_conclude_fallback(
                    config,
                    client,
                    environment,
                    handle,
                    worker,
                    driver,
                    project.project.id,
                    intent,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                    report_path,
                    run_id,
                )
            return write_conclude_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                fact_data["description"],
                title=fact_data["title"],
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
                metadata=metadata_for_report(
                    report_path,
                    run_id,
                    worker.name,
                    intent.id,
                    producing_run_log_id=first.run_log_id,
                ),
            )
        if did_timeout(first):
            LOG.warning(
                "explore timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            return _try_conclude_fallback(
                config,
                client,
                environment,
                handle,
                worker,
                driver,
                project.project.id,
                intent,
                export_yaml,
                session,
                lease,
                cancellation,
                report_path,
                run_id,
            )
        LOG.warning(
            "explore command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("explore task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    worker: WorkerConfig,
    driver,
    project_id: str,
    intent: Intent,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    report_path: str,
    run_id: str | None,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project_id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        _write_failure_and_release(environment, handle, client, project_id, intent.id, worker.name, report_path, run_id, "conclude_unavailable")
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
        _write_failure_and_release(environment, handle, client, project_id, intent.id, worker.name, report_path, run_id, "heartbeat_lost")
        return "failed"
    if cancellation.is_cancelled and cancellation.reason != "conclude_requested":
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project_id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project_id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        _write_failure_and_release(
            environment,
            handle,
            client,
            project_id,
            intent.id,
            worker.name,
            report_path,
            run_id,
            "project_not_active",
        )
        return "failed"

    handle = environment.prepare_project(project_id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "explore_conclude.md"),
        {
            "graph_yaml": write_graph_snapshot_reference(
                environment,
                handle,
                export_yaml.strip(),
                phase="explore_conclude",
            ),
            "intent_id": intent.id,
            "intent_description": intent.description,
            "report_path": report_path,
        },
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting conclude fallback project=%s intent=%s worker=%s", project_id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = _run_process(
        environment,
        handle,
        worker,
        conclude_argv,
        phase="explore_conclude",
        timeout=_effective_conclude_timeout(config, project_id, intent, client),
        project_id=project_id,
        intent_id=intent.id,
        report_path=report_path,
        run_id=run_id,
        control_state=intent.control_state,
        lease=lease,
        cancellation=None if cancellation.reason == "conclude_requested" else cancellation,
        client=client,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    session = record_remote_session(client, project_id, result, driver, session)
    conclude_cancellation = None if cancellation.reason == "conclude_requested" else cancellation
    cancelled = cancel_reason(result, conclude_cancellation)
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project_id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        _write_failure_and_release(environment, handle, client, project_id, intent.id, worker.name, report_path, run_id, "conclude_process_failed")
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        _write_failure_and_release(
            environment,
            handle,
            client,
            project_id,
            intent.id,
            worker.name,
            report_path,
            run_id,
            "conclude_process_failed",
            preview(result.stderr) or preview(result.stdout),
        )
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, fact_data = validate_explore_payload(payload)
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        _write_failure_and_release(
            environment,
            handle,
            client,
            project_id,
            intent.id,
            worker.name,
            report_path,
            run_id,
            "conclude_parse_failed",
            str(exc),
        )
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project_id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release_after_conclude_failure(client, project_id, intent.id, worker.name)
        return "rejected"
    if not validate_report_written(environment, handle, report_path):
        LOG.warning(
            "conclude report missing project=%s intent=%s worker=%s report_path=%s",
            project_id,
            intent.id,
            worker.name,
            report_path,
        )
        _write_failure_and_release(environment, handle, client, project_id, intent.id, worker.name, report_path, run_id, "missing_report_after_conclude")
        return "failed"
    return write_conclude_result(
        client,
        project_id,
        intent.id,
        worker.name,
        fact_data["description"],
        title=fact_data["title"],
        source="explore_conclude",
        phase_ms=conclude_ms,
        metadata=metadata_for_report(
            report_path,
            run_id,
            worker.name,
            intent.id,
            producing_run_log_id=result.run_log_id,
        ),
    )


def _run_process(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout: int,
    project_id: str,
    intent_id: str,
    report_path: str | None = None,
    run_id: str | None = None,
    control_state: str | None = None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
    client: CairnClient,
):
    return run_worker_process(
        environment,
        handle,
        worker,
        argv,
        phase=phase,
        timeout_seconds=timeout,
        project_id=project_id,
        task_type="explore",
        intent_id=intent_id,
        lease=lease,
        cancellation=cancellation,
        provenance_recorder=HttpRunProvenanceRecorder(client),
        extra_metadata={
            "report_path": report_path,
            "report_run_id": run_id,
            "control_state_at_start": control_state,
        },
    )


def _effective_timeout(config: DispatchConfig, project: ProjectDetail, intent: Intent) -> int:
    return intent.timeout_override_seconds or project.project.default_timeout_seconds or config.tasks.explore.timeout


def _effective_conclude_timeout(config: DispatchConfig, project_id: str, intent: Intent, client: CairnClient) -> int:
    try:
        project = client.get_project(project_id)
        project_default = project.project.default_conclude_timeout_seconds
    except Exception:
        project_default = None
    return intent.conclude_timeout_override_seconds or project_default or config.tasks.explore.conclude_timeout


def _write_failure_and_release(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    report_path: str,
    run_id: str | None,
    reason: str,
    details: str = "",
) -> None:
    try:
        write_failure_report(
            environment,
            handle,
            report_path,
            intent_id=intent_id,
            worker=worker_name,
            run_id=run_id,
            reason=reason,
            details=details,
        )
    except Exception:
        LOG.exception("failed to write failure report project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    best_effort_release_after_conclude_failure(client, project_id, intent_id, worker_name)
