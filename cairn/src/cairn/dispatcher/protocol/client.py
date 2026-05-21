from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.shared.api_models import Intent, ProjectDetail, ProjectSummary, ProviderEndpointSecret, Settings, WorkEnvironmentPublic

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._environment_adapter = TypeAdapter(list[WorkEnvironmentPublic])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def list_environments(self, *, include_secrets: bool = False) -> list[WorkEnvironmentPublic]:
        response = self._session().get(
            self._url("/environments"),
            params={"include_secrets": str(include_secrets).lower()},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return self._environment_adapter.validate_python(response.json())

    def get_environment_endpoint(
        self,
        environment_id: str,
        endpoint_id: str,
        *,
        include_secret: bool = True,
    ) -> ProviderEndpointSecret:
        response = self._session().get(
            self._url(f"/environments/{environment_id}/endpoints/{endpoint_id}"),
            params={"include_secret": str(include_secret).lower()},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return ProviderEndpointSecret.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        description: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApiResult:
        payload = {"worker": worker, "description": description}
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json=payload,
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        *,
        requested_worker: str | None = None,
        timeout_override_seconds: int | None = None,
        conclude_timeout_override_seconds: int | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {"from": from_ids, "description": description, "creator": creator, "worker": None}
        if requested_worker is not None:
            payload["requested_worker"] = requested_worker
        if timeout_override_seconds is not None:
            payload["timeout_override_seconds"] = timeout_override_seconds
        if conclude_timeout_override_seconds is not None:
            payload["conclude_timeout_override_seconds"] = conclude_timeout_override_seconds
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json=payload,
        )

    def patch_intent(self, project_id: str, intent_id: str, **fields: Any) -> ApiResult:
        return self._request_json(
            "PATCH",
            f"/projects/{project_id}/intents/{intent_id}",
            json=fields,
        )

    def request_conclude(self, project_id: str, intent_id: str, actor: str, reason: str | None = None) -> ApiResult:
        payload: dict[str, Any] = {"actor": actor}
        if reason is not None:
            payload["reason"] = reason
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/request-conclude",
            json=payload,
        )

    def upsert_workers(self, workers: list[dict[str, Any]]) -> ApiResult:
        return self._request_json("PUT", "/workers", json={"workers": workers})

    def upsert_worker_health(self, health: list[dict[str, Any]]) -> ApiResult:
        return self._request_json("PUT", "/dispatcher/workers/health", json={"health": health})

    def create_execution(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/executions",
            json=payload,
        )

    def lease_pending_execution(
        self,
        execution_id: str,
        *,
        dispatcher_id: str,
        worker_name: str,
        worker_type: str | None = None,
        environment_id: str | None = None,
        endpoint_id: str | None = None,
        model_profile_id: str | None = None,
        workspace: str | None = None,
        lease_seconds: int = 60,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            "/dispatcher/executions/lease",
            json={
                "execution_id": execution_id,
                "dispatcher_id": dispatcher_id,
                "worker_name": worker_name,
                "worker_type": worker_type,
                "environment_id": environment_id,
                "endpoint_id": endpoint_id,
                "model_profile_id": model_profile_id,
                "workspace": workspace,
                "lease_seconds": lease_seconds,
            },
        )

    def claim_healthcheck_executions(
        self,
        dispatcher_id: str,
        worker_names: list[str],
        environment_ids: list[str],
        *,
        limit: int = 1,
        lease_seconds: int = 60,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            "/dispatcher/healthcheck-executions/claim",
            json={
                "dispatcher_id": dispatcher_id,
                "worker_names": worker_names,
                "environment_ids": environment_ids,
                "limit": limit,
                "lease_seconds": lease_seconds,
            },
        )

    def claim_question_executions(
        self,
        dispatcher_id: str,
        worker_names: list[str],
        environment_ids: list[str],
        *,
        limit: int = 1,
        lease_seconds: int = 60,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            "/dispatcher/question-executions/claim",
            json={
                "dispatcher_id": dispatcher_id,
                "worker_names": worker_names,
                "environment_ids": environment_ids,
                "limit": limit,
                "lease_seconds": lease_seconds,
            },
        )

    def lease_intent_execution(
        self,
        project_id: str,
        intent_id: str,
        *,
        dispatcher_id: str,
        worker_name: str,
        worker_type: str | None = None,
        environment_id: str | None = None,
        endpoint_id: str | None = None,
        model_profile_id: str | None = None,
        workspace: str | None = None,
        task_type: str = "explore",
        phase: str = "run",
        lease_seconds: int = 60,
        allow_parallel: bool = False,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/dispatcher/intents/{intent_id}/lease-execution",
            json={
                "project_id": project_id,
                "dispatcher_id": dispatcher_id,
                "worker_name": worker_name,
                "worker_type": worker_type,
                "environment_id": environment_id,
                "endpoint_id": endpoint_id,
                "model_profile_id": model_profile_id,
                "workspace": workspace,
                "task_type": task_type,
                "phase": phase,
                "lease_seconds": lease_seconds,
                "allow_parallel": allow_parallel,
            },
        )

    def heartbeat_execution(self, execution_id: str, *, dispatcher_id: str, lease_seconds: int = 60) -> ApiResult:
        return self.patch_execution(
            execution_id,
            {
                "dispatcher_id": dispatcher_id,
                "last_heartbeat_at": None,
                "lease_seconds": lease_seconds,
            },
        )

    def patch_execution(self, execution_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "PATCH",
            f"/dispatcher/executions/{execution_id}",
            json=payload,
        )

    def append_execution_events(
        self,
        execution_id: str,
        *,
        dispatcher_id: str | None = None,
        events: list[dict[str, Any]],
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/dispatcher/executions/{execution_id}/events",
            json={"dispatcher_id": dispatcher_id, "events": events},
        )

    def upload_execution_artifact(self, execution_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/dispatcher/executions/{execution_id}/artifacts",
            json=payload,
        )

    def submit_execution_conclusion_report(self, execution_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            f"/dispatcher/executions/{execution_id}/conclusion-report",
            json=payload,
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
