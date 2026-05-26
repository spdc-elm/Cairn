from __future__ import annotations

import json
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from cairn.dispatcher.config import CleanupPolicy, DispatchConfig, SshEnvironmentConfig, TerminalConfig, WorkerConfig
from cairn.dispatcher.models import ReasonCheckpoint, RunningTask, TaskOutcome
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.environments import WorkEnvironment, build_environment
from cairn.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.dispatcher.tasks.bootstrap import run_bootstrap_task
from cairn.dispatcher.tasks.common import finish_execution_terminal
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.dispatcher.tasks.healthcheck import run_healthcheck_task
from cairn.dispatcher.tasks.questions import run_question_task
from cairn.dispatcher.tasks.reason import run_reason_task
from cairn.dispatcher.workers.registry import get_driver
from cairn.shared.api_models import Intent, ProjectDetail, ProjectSummary
from cairn.shared.api_models import WorkEnvironmentPublic
from cairn.shared.worker_events import status_event

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
ACTIVE_EXECUTION_STATUSES = {"pending", "leased", "running"}


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]
    blocked_environment: list[str]
    blocked_endpoint: list[str]
    blocked_requested_worker: list[str]
    blocked_auto_worker_scope: list[str]
    blocked_missing_worker: list[str]


class DispatcherLoop:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = DispatchConfig.load(config_path)
        self.client = CairnClient(self.config.server)
        self._environment_hashes: dict[str, str] = {}
        self.environment_metadata: dict[str, WorkEnvironmentPublic] = {}
        self.environments = self._build_environment_registry()
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self.cleanup_executor = ThreadPoolExecutor(max_workers=max(1, min(8, self.config.runtime.max_workers)))
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[tuple[str, str, str, str, str], float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self.project_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
        self.executor.shutdown(wait=True)
        self.cleanup_executor.shutdown(wait=True)
        for environment in self.environments.values():
            environment.close()
        self.client.close()

    def _build_environment_registry(self) -> dict[str, WorkEnvironment]:
        registry: dict[str, WorkEnvironment] = {}
        for environment in self.config.environments:
            registry[environment.id] = build_environment(environment)
            self._environment_hashes[environment.id] = _environment_config_hash(environment)
            self.environment_metadata[environment.id] = _environment_public_from_config(environment)
        try:
            server_environments = self.client.list_environments()
        except Exception as exc:
            LOG.info("server environments unavailable during dispatcher startup; using config only error=%s", exc)
            return registry
        for environment in server_environments:
            self.environment_metadata[environment.id] = environment
            config = _server_environment_config(environment)
            if config is None or environment.id in registry:
                continue
            registry[environment.id] = build_environment(config)
            self._environment_hashes[environment.id] = _server_environment_hash(environment)
        return registry

    def _refresh_environment_registry(self) -> None:
        try:
            server_environments = self.client.list_environments()
        except Exception as exc:
            self._log_changed("environment-refresh", logging.INFO, "server environment refresh skipped error=%s", exc)
            return
        config_ids = {environment.id for environment in self.config.environments}
        seen_server_ids: set[str] = set()
        for environment in server_environments:
            self.environment_metadata[environment.id] = environment
            config = _server_environment_config(environment)
            if config is None or environment.id in config_ids:
                continue
            seen_server_ids.add(environment.id)
            digest = _server_environment_hash(environment)
            if self._environment_hashes.get(environment.id) == digest and environment.id in self.environments:
                continue
            old = self.environments.get(environment.id)
            if old is not None:
                old.close()
            self.environments[environment.id] = build_environment(config)
            self._environment_hashes[environment.id] = digest
            LOG.info("environment registry refreshed environment=%s backend=%s", environment.id, environment.backend)
        for environment_id in list(self.environments):
            if environment_id in config_ids or environment_id in seen_server_ids:
                continue
            self.environments.pop(environment_id).close()
            self._environment_hashes.pop(environment_id, None)
            self.environment_metadata.pop(environment_id, None)
            LOG.info("environment registry removed environment=%s", environment_id)

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks(fail_on_all=False)
            while True:
                try:
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._publish_worker_inventory()
                        self._settings_checked = True
                    self._refresh_environment_registry()
                    self._reap_futures()
                    self._reap_cleanup_futures()
                    self._try_dispatch_healthcheck_execution()
                    self._try_dispatch_question_job()
                    summaries = self.client.list_projects()
                    self._initialize_reason_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    self._queue_environment_cleanups(summaries)
                    self._dispatch_available(summaries)
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True, fail_on_all=True)
        finally:
            self.close()

    def run_environment_healthchecks_only(self) -> None:
        try:
            for environment in self.environments.values():
                result = environment.run_healthcheck(self._worker_types_for_environment(environment.id))
                LOG.info(
                    "environment healthcheck environment=%s backend=%s status=%s result=%s",
                    environment.id,
                    environment.backend,
                    result.get("status"),
                    result,
                )
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False, fail_on_all: bool = True) -> None:
        if self._startup_healthchecks_checked:
            return
        self._run_startup_healthchecks(show_commands=show_commands, fail_on_all=fail_on_all)
        self._startup_healthchecks_checked = True

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch/global", logging.INFO, "skip dispatch because no active projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                return
            for summary in idle_projects:
                if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                    self._log_changed(
                        "dispatch/idle-limit",
                        logging.INFO,
                        "stop idle project dispatch because max_running_projects reached running_projects=%s",
                        self._running_project_count(active),
                    )
                    return
                if self._try_dispatch_project(summary):
                    dispatched = True
                    break

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        environment = self._environment_for_summary(summary)
        if environment is None:
            self._log_changed(
                f"{skip_scope}:environment",
                logging.INFO,
                "skip project=%s because environment=%s is not configured",
                summary.id,
                summary.environment_id,
            )
            return False
        cleanup_key = environment.cleanup_key(summary.id)
        if cleanup_key in self._cleanup_pending:
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because environment cleanup is still pending key=%s",
                summary.id,
                cleanup_key,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        environment = self._environment_for_project(project)
        if environment is None:
            self._log_changed(
                f"{skip_scope}:environment",
                logging.INFO,
                "skip project=%s because environment=%s is not configured",
                summary.id,
                project.project.environment_id,
            )
            return False
        if project.project.status != "active":
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        self._cancel_control_requested_tasks(project)
        if self._is_initial_project(project):
            if project.project.reason is not None:
                return False
            return self._dispatch_initial_project(project, environment)
        running_intent_ids = self._project_running_explore_intents(summary.id)
        unclaimed_intents = [
            intent
            for intent in project.intents
            if intent.to is None
            and not self._intent_has_active_execution(intent)
            and intent.id not in running_intent_ids
            and intent.control_state == "normal"
            and not self._is_bootstrap_intent(intent)
        ]
        if running_intent_ids and not unclaimed_intents:
            self._log_changed(
                f"{skip_scope}:explore_running",
                logging.DEBUG,
                "skip explore project=%s because all unclaimed intents are already running locally intents=%s",
                summary.id,
                sorted(running_intent_ids),
            )
        if unclaimed_intents:
            export_yaml = self.client.export_project(summary.id)
            for intent in sorted(unclaimed_intents, key=lambda i: i.created_at, reverse=True):
                if self._dispatch_explore(project, export_yaml, intent, environment):
                    return True
            return False
        if project.project.reason is not None:
            self._log_changed(
                f"{skip_scope}:reason_claimed",
                logging.DEBUG,
                "skip reason project=%s because reason is already claimed by %s",
                summary.id,
                project.project.reason.worker,
            )
            return False
        if not project.project.auto_reason:
            self._log_changed(
                f"{skip_scope}:auto_reason_disabled",
                logging.DEBUG,
                "skip reason project=%s because auto_reason is disabled",
                summary.id,
            )
            return False
        reason_trigger = self._reason_trigger(project)
        if reason_trigger is None:
            self._log_changed(
                f"{skip_scope}:graph_unchanged",
                logging.DEBUG,
                "skip reason project=%s because reason state unchanged facts=%s hints=%s open_intents=%s intents=%s",
                summary.id,
                len(project.facts),
                len(project.hints),
                self._project_open_intent_count(project),
                len(project.intents),
            )
            return False
        export_yaml = self.client.export_project(summary.id)
        return self._dispatch_reason(project, export_yaml, reason_trigger, environment)

    def _dispatch_initial_project(self, project: ProjectDetail, environment: WorkEnvironment) -> bool:
        intent = self._get_bootstrap_intent(project)
        if intent is None:
            if not project.project.auto_reason:
                self._log_changed(
                    f"project:{project.project.id}:skip:bootstrap_auto_disabled",
                    logging.DEBUG,
                    "skip automatic bootstrap project=%s because auto_reason is disabled",
                    project.project.id,
                )
                return False
            intent = self._create_bootstrap_intent(project.project.id)
            if intent is None:
                return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if self._intent_has_active_execution(intent):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap intent=%s has active execution worker=%s",
                project.project.id,
                intent.id,
                self._intent_active_worker_name(intent),
            )
            return False
        if intent.control_state != "normal":
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_control",
                logging.INFO,
                "skip bootstrap project=%s intent=%s because control_state=%s",
                project.project.id,
                intent.id,
                intent.control_state,
            )
            return False
        return self._dispatch_bootstrap(project, intent, environment)

    def _dispatch_reason(self, project: ProjectDetail, export_yaml: str, trigger: str, environment: WorkEnvironment) -> bool:
        selection = self._select_worker(
            project.project.id,
            "reason",
            environment.id,
            allowed_auto_workers=project.project.allowed_auto_workers,
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:reason",
                logging.INFO,
                "no worker available for reason project=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        worker = self._worker_with_environment_endpoint(worker, environment.id)
        if worker is None:
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:reason")
        created = self.client.create_execution(
            project.project.id,
            {
                "task_type": "reason",
                "phase": "run",
                "input_snapshot": {"trigger": trigger},
            },
        )
        if created.status_code in (403, 409):
            level = logging.INFO if created.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason execution create failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                created.status_code,
            )
            return False
        if not created.ok:
            LOG.warning(
                "reason execution create failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                created.status_code,
            )
            return False
        execution_id = _execution_id_from_response(created.data)
        if execution_id is None:
            LOG.warning("reason execution create returned no id project=%s worker=%s", project.project.id, worker.name)
            return False
        claim = self.client.lease_pending_execution(
            execution_id,
            dispatcher_id="dispatcher",
            worker_name=worker.name,
            worker_type=worker.type,
            environment_id=environment.id,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            workspace=project.project.planned_workspace,
        )
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_reason_task,
                self.config,
                self.client,
                environment,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
                execution_id,
            )
        except Exception:
            LOG.exception("failed to submit reason task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_reason(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "reason",
            worker.name,
            cancellation,
            environment_id=environment.id,
            worker_type=worker.type,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            intent_id=None,
            execution_id=execution_id,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_intent_count=self._project_open_intent_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched reason project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, intent: Intent, environment: WorkEnvironment) -> bool:
        selection = self._select_worker(
            project.project.id,
            "bootstrap",
            environment.id,
            requested_worker=intent.requested_worker,
            allowed_auto_workers=(
                project.project.allowed_auto_workers
                if project.project.auto_reason and not intent.requested_worker
                else None
            ),
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        worker = self._worker_with_environment_endpoint(worker, environment.id)
        if worker is None:
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        claim = self.client.lease_intent_execution(
            project.project.id,
            intent.id,
            dispatcher_id="dispatcher",
            worker_name=worker.name,
            worker_type=worker.type,
            environment_id=environment.id,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            workspace=project.project.planned_workspace,
            task_type="explore",
            phase="bootstrap",
        )
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        execution_id = _execution_id_from_response(claim.data)
        if execution_id is None:
            LOG.warning("bootstrap claim returned no execution id project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                environment,
                project,
                intent,
                worker,
                cancellation := TaskCancellation(),
                execution_id,
            )
        except Exception:
            LOG.exception("failed to submit bootstrap task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "bootstrap",
            worker.name,
            cancellation,
            environment_id=environment.id,
            worker_type=worker.type,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            intent_id=intent.id,
            execution_id=execution_id,
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched bootstrap project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _dispatch_explore(self, project: ProjectDetail, export_yaml: str, intent: Intent, environment: WorkEnvironment) -> bool:
        selection = self._select_worker(
            project.project.id,
            "explore",
            environment.id,
            requested_worker=intent.requested_worker,
            allowed_auto_workers=None if intent.requested_worker else project.project.allowed_auto_workers,
        )
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:explore",
                logging.INFO,
                "no worker available for explore project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        worker = self._worker_with_environment_endpoint(worker, environment.id)
        if worker is None:
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:explore")
        claim = self.client.lease_intent_execution(
            project.project.id,
            intent.id,
            dispatcher_id="dispatcher",
            worker_name=worker.name,
            worker_type=worker.type,
            environment_id=environment.id,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            workspace=project.project.planned_workspace,
            task_type="explore",
            phase="run",
        )
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        execution_id = _execution_id_from_response(claim.data)
        if execution_id is None:
            LOG.warning("explore claim returned no execution id project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            return False
        try:
            future = self.executor.submit(
                run_explore_task,
                self.config,
                self.client,
                environment,
                project,
                export_yaml,
                intent,
                worker,
                cancellation := TaskCancellation(),
                execution_id,
            )
        except Exception:
            LOG.exception("failed to submit explore task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "explore",
            worker.name,
            cancellation,
            environment_id=environment.id,
            worker_type=worker.type,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            intent_id=intent.id,
            execution_id=execution_id,
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched explore project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _select_worker(
        self,
        project_id: str,
        task_type: str,
        environment_id: str,
        *,
        requested_worker: str | None = None,
        allowed_auto_workers: list[str] | None = None,
    ) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        blocked_environment: list[str] = []
        blocked_endpoint: list[str] = []
        blocked_requested_worker: list[str] = []
        blocked_auto_worker_scope: list[str] = []
        blocked_missing_worker: list[str] = []
        running_counts = self._worker_counts()
        worker_names = {worker.name for worker in self.config.workers}
        if requested_worker is not None and requested_worker not in worker_names:
            blocked_missing_worker.append(requested_worker)
        for worker in self.config.workers:
            if requested_worker is not None and worker.name != requested_worker:
                blocked_requested_worker.append(worker.name)
                continue
            if requested_worker is None and allowed_auto_workers is not None and worker.name not in allowed_auto_workers:
                blocked_auto_worker_scope.append(worker.name)
                continue
            if worker.allowed_environments is not None and environment_id not in worker.allowed_environments:
                blocked_environment.append(worker.name)
                continue
            if not self._worker_endpoint_available(worker, environment_id):
                blocked_endpoint.append(worker.name)
                continue
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(self._worker_health_key(environment_id, worker), 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s blocked_endpoint=%s",
                project_id,
                task_type,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
                blocked_endpoint,
            )
            return WorkerSelection(
                worker=None,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
                blocked_environment=blocked_environment,
                blocked_endpoint=blocked_endpoint,
                blocked_requested_worker=blocked_requested_worker,
                blocked_auto_worker_scope=blocked_auto_worker_scope,
                blocked_missing_worker=blocked_missing_worker,
            )
        ordered = choose_worker(candidates, running_counts)
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s blocked_endpoint=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            blocked_endpoint,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
            blocked_environment=blocked_environment,
            blocked_endpoint=blocked_endpoint,
            blocked_requested_worker=blocked_requested_worker,
            blocked_auto_worker_scope=blocked_auto_worker_scope,
            blocked_missing_worker=blocked_missing_worker,
        )

    def _worker_endpoint_available(self, worker: WorkerConfig, environment_id: str) -> bool:
        if worker.type == "mock":
            return True
        if not worker.endpoint:
            return False
        metadata = self.environment_metadata.get(environment_id)
        if metadata is None:
            return False
        return any(endpoint.id == worker.endpoint and endpoint.type == worker.type for endpoint in metadata.provider_endpoints)

    def _worker_with_environment_endpoint(self, worker: WorkerConfig, environment_id: str) -> WorkerConfig | None:
        if worker.type == "mock":
            return self.config.worker_with_endpoint_env(worker, None)
        assert worker.endpoint is not None
        try:
            endpoint = self.client.get_environment_endpoint(
                environment_id,
                worker.endpoint,
                include_secret=True,
            )
            return self.config.worker_with_endpoint_env(worker, endpoint)
        except Exception as exc:
            LOG.warning(
                "worker endpoint resolution failed environment=%s worker=%s endpoint=%s error=%s",
                environment_id,
                worker.name,
                worker.endpoint,
                exc,
            )
            return None

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _worker_health_key(self, environment_id: str, worker: WorkerConfig) -> tuple[str, str, str, str, str]:
        return (
            environment_id,
            worker.name,
            worker.type,
            worker.endpoint or "",
            worker.model_profile or "",
        )

    def _task_health_key(self, task: RunningTask) -> tuple[str, str, str, str, str]:
        return (
            task.environment_id or "",
            task.worker_name,
            task.worker_type or "",
            task.endpoint_id or "",
            task.model_profile_id or "",
        )

    def _publish_worker_health(self, health: list[dict]) -> None:
        health = [item for item in health if item.get("environment_id") and item.get("worker_name") and item.get("worker_type")]
        if not health:
            return
        if not hasattr(self.client, "upsert_worker_health"):
            LOG.debug("worker health publish skipped because client lacks endpoint")
            return
        response = self.client.upsert_worker_health(health)
        if not response.ok:
            LOG.warning("worker health publish failed status=%s body=%s", response.status_code, response.text)

    def _with_health_retry_window(self, health: dict) -> dict:
        payload = dict(health)
        payload.setdefault("disabled_until", _future_iso(UNHEALTHY_RETRY_AFTER_SECONDS))
        payload.setdefault("stale_after", _future_iso(max(UNHEALTHY_RETRY_AFTER_SECONDS * 6, 30)))
        return payload

    def _ok_health_for_task(self, task: RunningTask) -> dict:
        return {
            "environment_id": task.environment_id,
            "worker_name": task.worker_name,
            "worker_type": task.worker_type,
            "endpoint_id": task.endpoint_id,
            "model_profile_id": task.model_profile_id,
            "status": "ok",
            "checked_at": _now_iso(),
            "stale_after": _future_iso(300),
            "source": "task_success",
            "dispatcher_id": "dispatcher",
            "detail": {"task_type": task.task_type, "project_id": task.project_id},
        }

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.intent_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.intent_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_explore_intents(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "explore" and task.intent_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if intent.to is None)

    def _intent_has_active_execution(self, intent: Intent) -> bool:
        return bool(
            getattr(intent, "active_execution_id", None)
            and getattr(intent, "runtime_status", None) in ACTIVE_EXECUTION_STATUSES
        )

    def _intent_active_worker_name(self, intent: Intent) -> str | None:
        if not self._intent_has_active_execution(intent):
            return None
        return (
            getattr(intent, "active_worker_name", None)
            or getattr(intent, "worker_name", None)
            or getattr(intent, "worker", None)
        )

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [intent for intent in project.intents if self._is_bootstrap_intent(intent)]
        if not intents:
            return None
        if len(intents) > 1:
            LOG.warning("project has multiple bootstrap intents project=%s intents=%s", project.project.id, [intent.id for intent in intents])
        intents.sort(key=lambda intent: (self._intent_has_active_execution(intent), intent.created_at, intent.id))
        return intents[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _create_bootstrap_intent(self, project_id: str) -> Intent | None:
        response = self.client.create_intent(
            project_id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap intent create project=%s", project_id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap intent write failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned empty body project=%s", project_id)
            return None
        intent = Intent.model_validate(response.data)
        LOG.info("created bootstrap intent project=%s intent=%s", project_id, intent.id)
        return intent

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        open_intent_count = self._project_open_intent_count(project)
        checkpoint = self.reason_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        if not changes:
            return None
        return ",".join(changes)

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            try:
                raw_outcome = future.result()
                outcome = raw_outcome.status if isinstance(raw_outcome, TaskOutcome) else raw_outcome
                if isinstance(raw_outcome, TaskOutcome) and raw_outcome.worker_health:
                    self._publish_worker_health([self._with_health_retry_window(raw_outcome.worker_health)])
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                health_key = self._task_health_key(task)
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[health_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy identity=%s retry_after=%.0fs",
                        health_key,
                        retry_after_seconds,
                    )
                else:
                    self.worker_unhealthy_until.pop(health_key, None)
                    if outcome == "success" and task.task_type != "healthcheck":
                        self._publish_worker_health([self._ok_health_for_task(task)])
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if outcome == "success" and task.task_type == "reason":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_intent_count is not None
                    self.reason_checkpoints[task.project_id] = ReasonCheckpoint(
                        fact_count=task.fact_count,
                        hint_count=task.hint_count,
                        open_intent_count=task.open_intent_count,
                    )
                    LOG.debug(
                        "reason checkpoint updated project=%s facts=%s hints=%s open_intents=%s",
                        task.project_id,
                        task.fact_count,
                        task.hint_count,
                        task.open_intent_count,
                    )
            except Exception:
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)

    def _environment_for_summary(self, summary: ProjectSummary) -> WorkEnvironment | None:
        environment_id = summary.environment_id or self.config.default_environment_id
        return self.environments.get(environment_id)

    def _environment_for_project(self, project: ProjectDetail) -> WorkEnvironment | None:
        environment_id = project.project.environment_id or self.config.default_environment_id
        return self.environments.get(environment_id)

    def _cleanup_completed_environments(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            environment = self._environment_for_summary(summary)
            if environment is None:
                continue
            cleanup_key = environment.cleanup_key(summary.id)
            if cleanup_key in self._cleanup_pending:
                continue
            if not environment.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(environment.cleanup_completed, summary.id)
            self.cleanup_futures[future] = (cleanup_key, summary.id, summary.status)
            self._cleanup_pending.add(cleanup_key)

    def _cleanup_stopped_environments(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            environment = self._environment_for_summary(summary)
            if environment is None:
                continue
            cleanup_key = environment.cleanup_key(summary.id)
            if cleanup_key in self._cleanup_pending:
                continue
            if not environment.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(environment.cleanup_stopped, summary.id)
            self.cleanup_futures[future] = (cleanup_key, summary.id, summary.status)
            self._cleanup_pending.add(cleanup_key)

    def _queue_environment_cleanups(self, summaries: list[ProjectSummary]) -> None:
        self._cleanup_completed_environments(summaries)
        self._cleanup_stopped_environments(summaries)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success and project_id is not None and target_status in ("completed", "stopped"):
                    self._inactive_cleanup_done[project_id] = target_status
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("environment cleanup failed key=%s", name)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        self.runtime_project_ids.intersection_update(active_ids)
        inactive_status_by_id = {summary.id: summary.status for summary in summaries if summary.status != "active"}
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = inactive_status_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            if task.task_type == "healthcheck":
                continue
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _cancel_control_requested_tasks(self, project: ProjectDetail) -> None:
        intents = {intent.id: intent for intent in project.intents}
        for task in self.futures.values():
            if task.project_id != project.project.id or task.intent_id is None:
                continue
            intent = intents.get(task.intent_id)
            if intent is None or intent.control_state != "conclude_requested":
                continue
            if task.cancellation.cancel("conclude_requested"):
                LOG.info(
                    "cancelling running task for requested conclude project=%s intent=%s worker=%s",
                    task.project_id,
                    task.intent_id,
                    task.worker_name,
                )

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )
            LOG.debug(
                "reason checkpoint initialized project=%s facts=%s hints=%s open_intents=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_intent_count,
            )

    def _best_effort_release(self, project_id: str, intent_id: str, worker_name: str) -> None:
        response = self.client.release(project_id, intent_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s intent=%s worker=%s status=%s", project_id, intent_id, worker_name, response.status_code)

    def _best_effort_release_reason(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_reason(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("reason release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= interval:
                raise RuntimeError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _publish_worker_inventory(self) -> None:
        workers = [
            {
                "name": worker.name,
                "type": worker.type,
                "model_profile": worker.model_profile,
                "model": (profile.model if (profile := self.config.model_profile_config(worker)) is not None else None),
                "model_context_window": profile.context_window if profile is not None else None,
                "endpoint": worker.endpoint,
                "task_types": list(worker.task_types),
                "max_running": worker.max_running,
                "priority": worker.priority,
                "allowed_environments": worker.allowed_environments,
                "question_capability": asdict(get_driver(worker.type).question_capability(worker)),
                "capability_source": "config",
            }
            for worker in self.config.workers
        ]
        response = self.client.upsert_workers(workers)
        if response.ok:
            LOG.info("published worker inventory workers=%s", [worker["name"] for worker in workers])
        else:
            LOG.info("worker inventory publish skipped status=%s body=%s", response.status_code, response.text)

    def _try_dispatch_healthcheck_execution(self) -> bool:
        if len(self.futures) >= self.config.runtime.max_workers:
            return False
        running_counts = self._worker_counts()
        available_workers = [
            worker.name
            for worker in self.config.workers
            if running_counts.get(worker.name, 0) < worker.max_running
        ]
        if not available_workers:
            return False
        limit = max(1, min(self.config.runtime.max_workers - len(self.futures), len(available_workers)))
        claim = self.client.claim_healthcheck_executions(
            "dispatcher",
            available_workers,
            list(self.environments),
            limit=limit,
            lease_seconds=max(60, self.config.runtime.healthcheck_timeout + 20),
        )
        if not claim.ok or not isinstance(claim.data, list) or not claim.data:
            return False
        dispatched = False
        for execution in claim.data:
            worker_name = execution.get("worker_name") if isinstance(execution, dict) else None
            environment_id = execution.get("environment_id") if isinstance(execution, dict) else None
            execution_id = execution.get("id") if isinstance(execution, dict) else None
            project_id = execution.get("project_id") if isinstance(execution, dict) else None
            worker = next((candidate for candidate in self.config.workers if candidate.name == worker_name), None)
            environment = self.environments.get(environment_id or "")
            if worker is None or environment is None or not execution_id or not project_id:
                if execution_id:
                    finish_execution_terminal(
                        self.client,
                        execution_id,
                        events=[
                            status_event(
                                "failed",
                                event_key=f"{execution_id}:status:healthcheck-target-unavailable",
                                error_code="healthcheck_target_unavailable",
                                error_detail="worker or environment is not configured",
                            ).to_api_payload()
                        ],
                        patch={
                            "status": "failed",
                            "error_code": "healthcheck_target_unavailable",
                            "error_detail": "worker or environment is not configured",
                        },
                    )
                continue
            resolved_worker = self._worker_with_environment_endpoint(worker, environment.id)
            if resolved_worker is None:
                finish_execution_terminal(
                    self.client,
                    execution_id,
                    events=[
                        status_event(
                            "failed",
                            event_key=f"{execution_id}:status:worker-endpoint-unavailable",
                            error_code="worker_endpoint_unavailable",
                            error_detail="worker endpoint unavailable",
                        ).to_api_payload()
                    ],
                    patch={
                        "status": "failed",
                        "error_code": "worker_endpoint_unavailable",
                        "error_detail": "worker endpoint unavailable",
                    },
                )
                continue
            try:
                future = self.executor.submit(
                    run_healthcheck_task,
                    self.config,
                    self.client,
                    environment,
                    resolved_worker,
                    cancellation := TaskCancellation(),
                    execution_id,
                )
            except Exception:
                LOG.exception("failed to submit healthcheck execution=%s worker=%s", execution_id, worker.name)
                finish_execution_terminal(
                    self.client,
                    execution_id,
                    events=[
                        status_event(
                            "failed",
                            event_key=f"{execution_id}:status:healthcheck-submit-failed",
                            error_code="healthcheck_submit_failed",
                            error_detail="failed to submit healthcheck task",
                        ).to_api_payload()
                    ],
                    patch={
                        "status": "failed",
                        "error_code": "healthcheck_submit_failed",
                        "error_detail": "failed to submit healthcheck task",
                    },
                )
                continue
            self.futures[future] = RunningTask(
                project_id,
                "healthcheck",
                resolved_worker.name,
                cancellation,
                environment_id=environment.id,
                worker_type=resolved_worker.type,
                endpoint_id=resolved_worker.endpoint,
                model_profile_id=resolved_worker.model_profile,
                execution_id=execution_id,
            )
            LOG.info("dispatched healthcheck execution=%s environment=%s worker=%s", execution_id, environment.id, resolved_worker.name)
            dispatched = True
        return dispatched

    def _try_dispatch_question_job(self) -> bool:
        if len(self.futures) >= self.config.runtime.max_workers:
            return False
        running_counts = self._worker_counts()
        available_workers = [worker.name for worker in self.config.workers if running_counts.get(worker.name, 0) < worker.max_running]
        if not available_workers:
            return False
        claim = self.client.claim_question_executions("dispatcher", available_workers, list(self.environments), limit=1)
        if not claim.ok or not isinstance(claim.data, list) or not claim.data:
            return False
        job = claim.data[0]
        worker_name = job.get("worker_name")
        worker = next((candidate for candidate in self.config.workers if candidate.name == worker_name), None)
        if worker is None:
            finish_execution_terminal(
                self.client,
                job["id"],
                events=[
                    status_event(
                        "failed",
                        event_key=f"{job['id']}:status:worker-not-available",
                        error_code="worker_not_available",
                        error_detail=f"worker not configured: {worker_name}",
                    ).to_api_payload()
                ],
                patch={
                    "status": "failed",
                    "error_code": "worker_not_available",
                    "error_detail": f"worker not configured: {worker_name}",
                },
            )
            return False
        project = self.client.get_project(job["project_id"])
        execution_environment_id = job.get("environment_id")
        environment = self.environments.get(execution_environment_id) if execution_environment_id else self._environment_for_project(project)
        if environment is None:
            finish_execution_terminal(
                self.client,
                job["id"],
                events=[
                    status_event(
                        "failed",
                        event_key=f"{job['id']}:status:environment-not-available",
                        error_code="environment_not_available",
                        error_detail="project environment is not configured",
                    ).to_api_payload()
                ],
                patch={
                    "status": "failed",
                    "error_code": "environment_not_available",
                    "error_detail": "project environment is not configured",
                },
            )
            return False
        worker = self._worker_with_environment_endpoint(worker, environment.id)
        if worker is None:
            finish_execution_terminal(
                self.client,
                job["id"],
                events=[
                    status_event(
                        "failed",
                        event_key=f"{job['id']}:status:worker-endpoint-unavailable",
                        error_code="worker_not_available",
                        error_detail="worker endpoint unavailable",
                    ).to_api_payload()
                ],
                patch={
                    "status": "failed",
                    "error_code": "worker_not_available",
                    "error_detail": "worker endpoint unavailable",
                },
            )
            return False
        try:
            future = self.executor.submit(
                run_question_task,
                self.config,
                self.client,
                environment,
                project,
                worker,
                job,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit question execution=%s worker=%s", job["id"], worker.name)
            finish_execution_terminal(
                self.client,
                job["id"],
                events=[
                    status_event(
                        "failed",
                        event_key=f"{job['id']}:status:question-submit-failed",
                        error_code="worker_process_failed",
                        error_detail="failed to submit question task",
                    ).to_api_payload()
                ],
                patch={
                    "status": "failed",
                    "error_code": "worker_process_failed",
                    "error_detail": "failed to submit question task",
                },
            )
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "question",
            worker.name,
            cancellation,
            environment_id=environment.id,
            worker_type=worker.type,
            endpoint_id=worker.endpoint,
            model_profile_id=worker.model_profile,
            execution_id=job["id"],
        )
        self.runtime_project_ids.add(project.project.id)
        LOG.info("dispatched question project=%s execution=%s worker=%s", project.project.id, job["id"], worker.name)
        return True

    def _run_startup_healthchecks(self, *, show_commands: bool, fail_on_all: bool) -> None:
        results = run_startup_healthchecks(
            self.config,
            self.environments,
            environment_metadata=self.environment_metadata,
            endpoint_loader=self.client.get_environment_endpoint,
            show_commands=show_commands,
        )
        health_payloads = [self._health_from_startup_result(result) for result in results]
        self._publish_worker_health(health_payloads)
        if not hasattr(self, "worker_unhealthy_until"):
            self.worker_unhealthy_until = {}
        for result in results:
            key = (
                result.environment_id,
                result.worker_name,
                result.worker_type,
                result.endpoint_id or "",
                result.model_profile_id or "",
            )
            if result.ok:
                self.worker_unhealthy_until.pop(key, None)
            else:
                self.worker_unhealthy_until[key] = time.time() + UNHEALTHY_RETRY_AFTER_SECONDS
        if any(result.ok for result in results):
            return
        if not fail_on_all:
            LOG.warning(format_failure_summary(results))
            return
        raise RuntimeError(format_failure_summary(results))

    def _worker_types_for_environment(self, environment_id: str) -> list:
        metadata = self.environment_metadata.get(environment_id)
        if metadata is None:
            return []
        return sorted({endpoint.type for endpoint in metadata.provider_endpoints})

    def _health_from_startup_result(self, result) -> dict:
        status = "ok" if result.ok else "unhealthy"
        payload = {
            "environment_id": result.environment_id,
            "worker_name": result.worker_name,
            "worker_type": result.worker_type,
            "endpoint_id": result.endpoint_id,
            "model_profile_id": result.model_profile_id,
            "status": status,
            "checked_at": _now_iso(),
            "stale_after": _future_iso(300 if result.ok else max(UNHEALTHY_RETRY_AFTER_SECONDS * 6, 30)),
            "source": "startup_healthcheck",
            "dispatcher_id": "dispatcher",
            "detail": {
                "returncode": result.returncode,
                "http_status": result.http_status,
                "duration_ms": result.duration_ms,
                "response_preview": result.response_preview,
                "stderr_preview": result.stderr_preview,
                "command": result.command,
            },
        }
        if not result.ok:
            payload["disabled_until"] = _future_iso(UNHEALTHY_RETRY_AFTER_SECONDS)
        return payload


def _server_environment_config(environment: WorkEnvironmentPublic) -> SshEnvironmentConfig | None:
    if environment.backend != "ssh" or not environment.ssh_command:
        return None
    return SshEnvironmentConfig(
        id=environment.id,
        label=environment.label,
        backend="ssh",
        ssh_command=environment.ssh_command,
        workspace_root=environment.workspace_root or "/home/kali/cairn-workspaces",
        cleanup=CleanupPolicy.model_validate(environment.cleanup or {"completed_action": "stop"}),
        terminal=TerminalConfig.model_validate(environment.terminal or {"mode": "none"}),
    )


def _server_environment_hash(environment: WorkEnvironmentPublic) -> str:
    payload = {
        "id": environment.id,
        "label": environment.label,
        "backend": environment.backend,
        "ssh_command": environment.ssh_command,
        "workspace_root": environment.workspace_root,
        "cleanup": environment.cleanup,
        "terminal": environment.terminal,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _environment_public_from_config(environment) -> WorkEnvironmentPublic:
    if environment.backend == "ssh":
        return WorkEnvironmentPublic(
            id=environment.id,
            label=environment.label,
            backend="ssh",
            ssh_command=environment.ssh_command,
            workspace_root=environment.workspace_root,
            cleanup=environment.cleanup.model_dump(mode="json"),
            terminal=environment.terminal.model_dump(mode="json"),
            provider_endpoints=[],
        )
    return WorkEnvironmentPublic(
        id=environment.id,
        label=environment.label,
        backend="docker",
        cleanup=environment.cleanup.model_dump(mode="json"),
        provider_endpoints=[],
    )


def _environment_config_hash(environment) -> str:
    return json.dumps(environment.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _future_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _execution_id_from_response(data) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get("id")
    return value if isinstance(value, str) and value else None
