from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class WorkerInventoryItem(BaseModel):
    name: str
    type: str
    model_profile: str | None = None
    endpoint: str | None = None
    task_types: list[str] = Field(default_factory=list)
    max_running: int = Field(gt=0)
    priority: int = 0
    allowed_environments: list[str] | None = None
    updated_at: str | None = None

    @field_validator("name", "type")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("model_profile", "endpoint")
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
