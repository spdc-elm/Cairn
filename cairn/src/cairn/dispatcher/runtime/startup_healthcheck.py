from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.redaction import redact_text
from cairn.dispatcher.runtime.environments.base import WorkEnvironment
from cairn.dispatcher.tasks.common import run_healthcheck
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProviderEndpointSecret, WorkEnvironmentPublic

LOG = logging.getLogger("runtime.startup")
STARTUP_HEALTHCHECK_PREVIEW_LIMIT = 50


@dataclass(slots=True)
class StartupHealthcheckResult:
    environment_id: str
    backend: str
    worker_name: str
    worker_type: str
    model_profile_id: str | None
    endpoint_id: str | None
    ok: bool
    returncode: int
    duration_ms: int
    http_status: str | None
    response_preview: str
    stderr_preview: str
    command: str


def run_startup_healthchecks(
    config: DispatchConfig,
    environments: dict[str, WorkEnvironment],
    *,
    environment_metadata: dict[str, WorkEnvironmentPublic] | None = None,
    endpoint_loader: Callable[[str, str], ProviderEndpointSecret] | None = None,
    show_commands: bool = False,
) -> list[StartupHealthcheckResult]:
    jobs: list[tuple[WorkEnvironment, WorkerConfig]] = []
    for environment in environments.values():
        for worker in config.workers:
            if worker.allowed_environments is not None and environment.id not in worker.allowed_environments:
                continue
            jobs.append((environment, worker))
    parallelism = max(1, min(len(jobs), config.runtime.max_workers, 8))
    LOG.info(
        "[*] Startup healthcheck: jobs=%s environments=%s parallelism=%s",
        len(jobs),
        len(environments),
        parallelism,
    )
    results: list[StartupHealthcheckResult] = []
    with ThreadPoolExecutor(max_workers=parallelism) as executor:
        future_map = {}
        for environment, worker in jobs:
            try:
                resolved_worker = _resolve_worker_for_healthcheck(
                    config,
                    worker,
                    environment,
                    environment_metadata or {},
                    endpoint_loader,
                )
            except Exception as exc:
                LOG.warning("startup healthcheck skipped environment=%s worker=%s error=%s", environment.id, worker.name, exc)
                results.append(
                    StartupHealthcheckResult(
                        environment_id=environment.id,
                        backend=environment.backend,
                        worker_name=worker.name,
                        worker_type=worker.type,
                        model_profile_id=worker.model_profile,
                        endpoint_id=worker.endpoint,
                        ok=False,
                        returncode=1,
                        duration_ms=0,
                        http_status=None,
                        response_preview="",
                        stderr_preview=str(exc),
                        command="-",
                    )
                )
                continue
            future_map[
                executor.submit(
                    _run_worker_healthcheck,
                    environment,
                    resolved_worker,
                    config.runtime.healthcheck_timeout,
                )
            ] = (environment, worker)
        for future in as_completed(future_map):
            environment, worker = future_map[future]
            try:
                result = future.result()
            except Exception:
                LOG.exception("startup healthcheck crashed environment=%s worker=%s", environment.id, worker.name)
                result = StartupHealthcheckResult(
                    environment_id=environment.id,
                    backend=environment.backend,
                    worker_name=worker.name,
                    worker_type=worker.type,
                    model_profile_id=worker.model_profile,
                    endpoint_id=worker.endpoint,
                    ok=False,
                    returncode=1,
                    duration_ms=0,
                    http_status=None,
                    response_preview="",
                    stderr_preview="startup healthcheck crashed",
                    command="-",
                )
            results.append(result)

    results.sort(key=lambda result: (result.environment_id, result.worker_name))
    _log_report(results, show_commands=show_commands)
    return results


def format_failure_summary(results: list[StartupHealthcheckResult]) -> str:
    failed = [result for result in results if not result.ok]
    if not failed:
        return "startup healthchecks failed for all workers"
    details = []
    for result in failed:
        preview = result.response_preview or result.stderr_preview or "-"
        details.append(
            f"{result.worker_name}(http={result.http_status or '-'}, code={result.returncode}, preview={preview})"
        )
    return f"startup healthchecks failed for all workers: {', '.join(details)}"


def _run_worker_healthcheck(
    environment: WorkEnvironment,
    worker: WorkerConfig,
    timeout_seconds: int,
) -> StartupHealthcheckResult:
    driver = get_driver(worker.type)
    handle = environment.prepare_startup()
    try:
        healthcheck = run_healthcheck(
            environment,
            handle,
            worker,
            driver.build_startup_healthcheck(worker),
            timeout_seconds=timeout_seconds,
        )
        result = healthcheck.result
        http_status, response_preview = _parse_stdout(result.stdout)
        return StartupHealthcheckResult(
            environment_id=environment.id,
            backend=environment.backend,
            worker_name=worker.name,
            worker_type=worker.type,
            model_profile_id=worker.model_profile,
            endpoint_id=worker.endpoint,
            ok=result.returncode == 0,
            returncode=result.returncode,
            duration_ms=healthcheck.duration_ms,
            http_status=http_status,
            response_preview=redact_text(response_preview, _worker_secrets(worker)),
            stderr_preview=redact_text(_preview(result.stderr), _worker_secrets(worker)),
            command=redact_text(driver.describe_startup_healthcheck(worker), _worker_secrets(worker)),
        )
    finally:
        environment.cleanup_startup(handle)


def _log_report(results: list[StartupHealthcheckResult], *, show_commands: bool) -> None:
    if not results:
        LOG.warning("[!] Startup healthcheck: no workers configured")
        return
    env_width = max(len("ENV"), *(len(result.environment_id) for result in results))
    worker_width = max(len("WORKER"), *(len(result.worker_name) for result in results))
    lines = ["[=] Startup healthcheck results"]
    header = f"{'CHK':<5} {'ENV':<{env_width}} {'BACKEND':<7} {'WORKER':<{worker_width}} {'HTTP':<6} {'CODE':<6} {'TIME_S':>8}  PREVIEW"
    lines.append(header)
    lines.append(f"{'-' * 5} {'-' * env_width} {'-' * 7} {'-' * worker_width} {'-' * 6} {'-' * 6} {'-' * 8}  {'-' * 50}")
    healthy_count = 0
    for result in results:
        if result.ok:
            healthy_count += 1
        marker = "[+]" if result.ok else "[-]"
        preview = result.response_preview or result.stderr_preview or "-"
        duration_seconds = f"{result.duration_ms / 1000:.2f}"
        lines.append(
            f"{marker:<5} "
            f"{result.environment_id:<{env_width}} "
            f"{result.backend:<7} "
            f"{result.worker_name:<{worker_width}} "
            f"{(result.http_status or '-'): <6} "
            f"{result.returncode:<6} "
            f"{duration_seconds:>8}  "
            f"{preview}"
        )
    lines.append(
        f"[=] Summary: total={len(results)} healthy={healthy_count} unhealthy={len(results) - healthy_count}"
    )
    if show_commands:
        lines.append("")
        lines.append("[=] Startup healthcheck commands")
        for result in results:
            lines.append(f"- {result.environment_id}/{result.worker_name}")
            lines.append(f"  {result.command}")
        lines.append("")
    LOG.info("\n%s\n", "\n".join(lines))


def _parse_stdout(stdout: str) -> tuple[str | None, str]:
    lines = stdout.splitlines()
    http_status: str | None = None
    body_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if http_status is None and stripped.startswith("http_status="):
            http_status = stripped.partition("=")[2] or None
            continue
        body_lines.append(line)
    return http_status, _preview("\n".join(body_lines))


def _preview(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= STARTUP_HEALTHCHECK_PREVIEW_LIMIT:
        return compact
    return compact[:STARTUP_HEALTHCHECK_PREVIEW_LIMIT] + "..."


def _worker_secrets(worker: WorkerConfig) -> list[str]:
    return [
        value
        for key, value in worker.env.items()
        if value and ("KEY" in key or "TOKEN" in key or "SECRET" in key)
    ]


def _resolve_worker_for_healthcheck(
    config: DispatchConfig,
    worker: WorkerConfig,
    environment: WorkEnvironment,
    environment_metadata: dict[str, WorkEnvironmentPublic],
    endpoint_loader: Callable[[str, str], ProviderEndpointSecret] | None,
) -> WorkerConfig:
    if worker.type == "mock":
        return config.worker_with_endpoint_env(worker, None)
    if not worker.endpoint:
        raise ValueError(f"worker {worker.name} requires endpoint")
    metadata = environment_metadata.get(environment.id)
    if metadata is None:
        raise ValueError(f"environment {environment.id} metadata unavailable")
    endpoint_meta = next(
        (
            endpoint
            for endpoint in metadata.provider_endpoints
            if endpoint.id == worker.endpoint and endpoint.type == worker.type
        ),
        None,
    )
    if endpoint_meta is None:
        raise ValueError(f"environment {environment.id} missing endpoint {worker.endpoint}")
    if endpoint_loader is None:
        raise ValueError("endpoint secret loader unavailable")
    endpoint = endpoint_loader(environment.id, worker.endpoint)
    return config.worker_with_endpoint_env(worker, endpoint)
