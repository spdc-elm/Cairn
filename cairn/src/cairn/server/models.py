from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


AGENT_CONTEXT_MAX_CONTENT_BYTES = 128 * 1024
AgentContextKind = Literal["agents_md"]


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    title: str
    description: str
    metadata: dict[str, Any] | None = None


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    active_execution_id: str | None = None
    latest_execution_id: str | None = None
    runtime_status: str | None = None
    active_worker_name: str | None = None
    latest_worker_name: str | None = None
    worker_name: str | None = None
    requested_worker: str | None = None
    timeout_override_seconds: int | None = Field(default=None, gt=0)
    conclude_timeout_override_seconds: int | None = Field(default=None, gt=0)
    control_state: Literal["normal", "conclude_requested", "abort_requested"] = "normal"
    control_requested_at: str | None = None
    control_requested_by: str | None = None
    control_reason: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


ProviderEndpointType = Literal["claudecode", "codex", "pi", "mock"]


class ProviderEndpointPublic(BaseModel):
    id: str
    type: ProviderEndpointType
    base_url: str
    provider_api: str | None = None
    has_api_key: bool = False
    api_key_preview: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ProviderEndpointSecret(ProviderEndpointPublic):
    api_key: str | None = None


class ProviderEndpointUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: ProviderEndpointType
    base_url: str
    provider_api: str | None = None
    api_key: str | None = None
    clear_api_key: bool = False

    @field_validator("id", "base_url")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("provider_api", "api_key")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text


class WorkEnvironmentPublic(BaseModel):
    id: str
    label: str
    backend: Literal["docker", "ssh"]
    ssh_command: str | None = None
    workspace_root: str | None = None
    cleanup: dict | None = None
    terminal: dict | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_health_status: str | None = None
    last_healthcheck: dict | None = None
    provider_endpoints: list[ProviderEndpointPublic] = Field(default_factory=list)


class WorkEnvironmentUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    label: str
    backend: Literal["ssh", "docker"] = "ssh"
    ssh_command: str | None = None
    workspace_root: str | None = "/home/kali/cairn-workspaces"
    harness: str | None = Field(default=None, exclude=True)
    cleanup: dict | None = None
    terminal: dict | None = None
    provider_endpoints: list[ProviderEndpointUpsert] = Field(default_factory=list)

    @field_validator("id", "label", "ssh_command", "workspace_root", "harness")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        return text


ExecutionTaskType = Literal["explore", "conclude", "reason", "question", "healthcheck"]
ExecutionPhase = Literal["bootstrap", "run", "followup", "healthcheck"]
ExecutionSessionAction = Literal["fresh_context", "fork_initial", "resume_continue", "branch_continue"]
ExecutionStatus = Literal["pending", "leased", "running", "succeeded", "failed", "cancelled"]
ExecutionControlState = Literal["normal", "conclude_requested", "abort_requested"]
ExecutionEventType = Literal["status", "stdout", "stderr", "message", "tool", "artifact", "fact_candidate", "session", "metric"]
ExecutionEventRole = Literal["user", "assistant", "system", "tool"]


class ExecutionRun(BaseModel):
    id: str
    project_id: str
    intent_id: str | None = None
    branch_id: str | None = None
    parent_execution_id: str | None = None
    task_type: ExecutionTaskType
    phase: ExecutionPhase
    session_action: ExecutionSessionAction | None = None
    worker_name: str | None = None
    worker_type: str | None = None
    environment_id: str | None = None
    endpoint_id: str | None = None
    model_profile_id: str | None = None
    workspace: str | None = None
    status: ExecutionStatus
    leased_by: str | None = None
    sink_token: str | None = None
    leased_at: str | None = None
    lease_expires_at: str | None = None
    last_heartbeat_at: str | None = None
    control_state: ExecutionControlState = "normal"
    control_requested_at: str | None = None
    control_reason: str | None = None
    remote_session_in_kind: str | None = None
    remote_session_in_id: str | None = None
    remote_session_in_status: str | None = None
    remote_session_out_kind: str | None = None
    remote_session_out_id: str | None = None
    remote_session_out_status: str | None = None
    input_snapshot: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class CreateExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str | None = None
    branch_id: str | None = None
    parent_execution_id: str | None = None
    task_type: ExecutionTaskType
    phase: ExecutionPhase
    session_action: ExecutionSessionAction | None = None
    remote_session_in_kind: str | None = None
    remote_session_in_id: str | None = None
    remote_session_in_status: str | None = None
    input_snapshot: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator(
        "intent_id",
        "branch_id",
        "parent_execution_id",
        "remote_session_in_kind",
        "remote_session_in_id",
        "remote_session_in_status",
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class LeaseExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatcher_id: str
    project_id: str | None = None
    execution_id: str | None = None
    worker_name: str
    worker_type: str | None = None
    environment_id: str | None = None
    endpoint_id: str | None = None
    model_profile_id: str | None = None
    workspace: str | None = None
    lease_seconds: int = Field(default=60, gt=0)
    task_type: ExecutionTaskType | None = None
    phase: ExecutionPhase | None = None
    allow_parallel: bool = False

    @field_validator("dispatcher_id", "worker_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("project_id", "execution_id", "worker_type", "environment_id", "endpoint_id", "model_profile_id", "workspace")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class ClaimHealthcheckExecutionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatcher_id: str
    worker_names: list[str] = Field(default_factory=list)
    environment_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=1, gt=0, le=32)
    lease_seconds: int = Field(default=60, gt=0)

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("worker_names", "environment_ids")
    @classmethod
    def validate_text_list(cls, value: list[str]) -> list[str]:
        result = []
        for item in value:
            text = item.strip()
            if text:
                result.append(text)
        return result


class ClaimQuestionExecutionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatcher_id: str
    worker_names: list[str] = Field(default_factory=list)
    environment_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=1, gt=0, le=32)
    lease_seconds: int = Field(default=60, gt=0)

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("worker_names", "environment_ids")
    @classmethod
    def validate_text_list(cls, value: list[str]) -> list[str]:
        result = []
        for item in value:
            text = item.strip()
            if text:
                result.append(text)
        return result


class PatchExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatcher_id: str | None = None
    sink_token: str | None = None
    status: ExecutionStatus | None = None
    last_heartbeat_at: str | None = None
    lease_seconds: int | None = Field(default=None, gt=0)
    control_state: ExecutionControlState | None = None
    control_reason: str | None = None
    remote_session_out_kind: str | None = None
    remote_session_out_id: str | None = None
    remote_session_out_status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    metadata: dict[str, Any] | None = None


class ExecutionEvent(BaseModel):
    id: str
    execution_id: str
    project_id: str
    seq: int
    project_seq: int
    cursor: str
    ts: str
    event_type: ExecutionEventType
    role: ExecutionEventRole | None = None
    payload: dict[str, Any]
    event_key: str | None = None
    created_at: str


class ExecutionEventAppend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: ExecutionEventType
    role: ExecutionEventRole | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    event_key: str | None = None
    ts: str | None = None


class AppendExecutionEventsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatcher_id: str | None = None
    sink_token: str | None = None
    events: list[ExecutionEventAppend] = Field(min_length=1, max_length=250)


class FinishExecutionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed", "cancelled"]
    returncode: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    remote_session_out_kind: str | None = None
    remote_session_out_id: str | None = None
    remote_session_out_status: str | None = None
    metadata: dict[str, Any] | None = None


class FinishExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatcher_id: str
    sink_token: str | None = None
    events: list[ExecutionEventAppend] = Field(default_factory=list, max_length=250)
    patch: FinishExecutionPatch

    @field_validator("dispatcher_id")
    @classmethod
    def validate_dispatcher_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ExecutionEventsResponse(BaseModel):
    events: list[ExecutionEvent]
    next_cursor: str | None = None


class Artifact(BaseModel):
    id: str
    project_id: str
    produced_by_execution_id: str | None = None
    type: Literal["report", "transcript", "scan", "file", "screenshot", "other"]
    uri: str | None = None
    path: str | None = None
    content_hash: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str


class AgentContextSummary(BaseModel):
    kind: AgentContextKind = "agents_md"
    enabled: bool = False
    source_template_id: str | None = None
    source_template_hash: str | None = None
    content_hash: str | None = None
    updated_at: str | None = None


class AgentContextTemplate(BaseModel):
    id: str
    name: str
    description: str | None = None
    kind: AgentContextKind = "agents_md"
    content: str
    content_hash: str
    created_at: str
    updated_at: str


class AgentContextTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    kind: AgentContextKind = "agents_md"
    content: str

    @field_validator("name", "content")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("description")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("content")
    @classmethod
    def validate_content_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > AGENT_CONTEXT_MAX_CONTENT_BYTES:
            raise ValueError("content exceeds 128 KiB")
        return value


class AgentContextTemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    content: str | None = None

    @field_validator("name")
    @classmethod
    def validate_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("description")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text.encode("utf-8")) > AGENT_CONTEXT_MAX_CONTENT_BYTES:
            raise ValueError("content exceeds 128 KiB")
        return text


class ProjectAgentContext(BaseModel):
    project_id: str
    kind: AgentContextKind = "agents_md"
    enabled: bool
    source_template_id: str | None = None
    source_template_hash: str | None = None
    content: str
    content_hash: str
    created_at: str
    updated_at: str


class ProjectAgentContextUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    kind: AgentContextKind = "agents_md"
    template_id: str | None = None
    content: str | None = None

    @field_validator("template_id")
    @classmethod
    def validate_template_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value.encode("utf-8")) > AGENT_CONTEXT_MAX_CONTENT_BYTES:
            raise ValueError("content exceeds 128 KiB")
        return value


class UploadExecutionArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["report", "transcript", "scan", "file", "screenshot", "other"]
    uri: str | None = None
    path: str | None = None
    content_hash: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("uri", "path", "content_hash", "summary")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class IntentRuntimeProjection(BaseModel):
    intent_id: str
    active_execution_id: str | None = None
    latest_execution_id: str | None = None
    runtime_status: str | None = None
    worker_name: str | None = None
    last_heartbeat_at: str | None = None


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    created_at: str
    reason: ProjectReason | None = None
    environment_id: str | None = None
    environment: WorkEnvironmentPublic | None = None
    planned_workspace: str | None = None
    auto_reason: bool = False
    allowed_auto_workers: list[str] | None = None
    default_timeout_seconds: int | None = Field(default=None, gt=0)
    default_conclude_timeout_seconds: int | None = Field(default=None, gt=0)
    agent_context_summary: AgentContextSummary | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    hints: list[CreateHintInline] | None = None
    environment_id: str | None = None
    auto_reason: bool = False
    allowed_auto_workers: list[str] | None = None
    default_timeout_seconds: int | None = Field(default=None, gt=0)
    default_conclude_timeout_seconds: int | None = Field(default=None, gt=0)
    agent_context: ProjectAgentContextUpsert | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("allowed_auto_workers")
    @classmethod
    def validate_allowed_auto_workers(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("worker names must not be empty")
            cleaned.append(text)
        return cleaned


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None
    requested_worker: str | None = None
    timeout_override_seconds: int | None = Field(default=None, gt=0)
    conclude_timeout_override_seconds: int | None = Field(default=None, gt=0)

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker", "requested_worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class UpdateIntentRequest(BaseModel):
    description: str | None = None
    requested_worker: str | None = None
    timeout_override_seconds: int | None = Field(default=None, gt=0)
    conclude_timeout_override_seconds: int | None = Field(default=None, gt=0)
    control_state: Literal["normal"] | None = None

    @field_validator("description", "requested_worker")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class RequestConcludeRequest(BaseModel):
    actor: str
    reason: str | None = None

    @field_validator("actor", "reason")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str
    title: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("worker", "description", "title")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ManualConcludePromptResponse(BaseModel):
    intent_id: str
    prompt: str
    source_execution_id: str | None = None
    source_session_available: bool = False
    remote_session_kind: str | None = None
    remote_session_id: str | None = None
    remote_session_status: str | None = None
    report_path: str | None = None
    expected_json_shape: dict[str, Any] = Field(
        default_factory=lambda: {"accepted": True, "data": {"title": "...", "description": "..."}}
    )


class ManualConcludeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    raw_json: str
    source_execution_id: str | None = None

    @field_validator("actor", "raw_json", "source_execution_id")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ManualConcludeParsedPayload(BaseModel):
    title: str | None = None
    description: str


class ExecutionConclusionReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: str | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("title", "description", "confidence")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text and value is not None:
            raise ValueError("must not be empty")
        return text


class UpdateFactRequest(BaseModel):
    title: str | None = None
    description: str | None = None

    @field_validator("title", "description")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class WorkerRuntimeHealth(BaseModel):
    environment_id: str
    worker_name: str
    worker_type: str
    endpoint_id: str | None = None
    model_profile_id: str | None = None
    status: Literal["ok", "unhealthy", "unknown"] = "unknown"
    checked_at: str | None = None
    stale_after: str | None = None
    disabled_until: str | None = None
    source: str | None = None
    dispatcher_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("environment_id", "worker_name", "worker_type")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("endpoint_id", "model_profile_id", "checked_at", "stale_after", "disabled_until", "source", "dispatcher_id")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class WorkerRuntimeHealthUpsertRequest(BaseModel):
    health: list[WorkerRuntimeHealth] = Field(default_factory=list)


class WorkerInventoryItem(BaseModel):
    name: str
    type: str
    model_profile: str | None = None
    model: str | None = None
    model_context_window: int | None = Field(default=None, gt=0)
    endpoint: str | None = None
    task_types: list[str] = Field(default_factory=list)
    max_running: int = Field(gt=0)
    priority: int = 0
    allowed_environments: list[str] | None = None
    question_capability: dict[str, Any] | None = None
    runtime_health: list[WorkerRuntimeHealth] = Field(default_factory=list)
    capability_updated_at: str | None = None
    capability_source: str | None = None
    updated_at: str | None = None

    @field_validator("name", "type")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("model_profile", "model", "endpoint")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class WorkerInventoryUpsertRequest(BaseModel):
    workers: list[WorkerInventoryItem] = Field(default_factory=list)


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class UpdateProjectRequest(BaseModel):
    auto_reason: bool | None = None
    allowed_auto_workers: list[str] | None = None
    default_timeout_seconds: int | None = Field(default=None, gt=0)
    default_conclude_timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("allowed_auto_workers")
    @classmethod
    def validate_allowed_auto_workers(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("worker names must not be empty")
            cleaned.append(text)
        return cleaned


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
