from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from importlib import resources
import os
from pathlib import Path
import shlex
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.shared.api_models import ProviderEndpointPublic


TaskType = Literal["reason", "explore", "bootstrap"]
WorkerType = Literal["claudecode", "codex", "pi", "mock"]
CompletedAction = Literal["remove", "stop"]
EnvironmentBackend = Literal["docker", "ssh"]
CredentialsMode = Literal["remote", "inject", "merge"]
TerminalMode = Literal["none", "tmux", "zellij"]

WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "claudecode": (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    ),
    "codex": (
        "CODEX_MODEL",
        "CODEX_BASE_URL",
        "OPENAI_API_KEY",
    ),
    "pi": (
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
    "mock": (),
}

DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": ("{graph_yaml}", "{fact_ids}", "{open_intents}", "{max_intents}"),
    "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "explore_conclude.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
}

PROMPT_REQUIRED_TOKENS_BY_GROUP: dict[str, dict[str, tuple[str, ...]]] = {
    "mock": {
        "reason.md": ("{fact_ids}", "{open_intents}", "{max_intents}"),
        "explore.md": ("{intent_id}",),
        "explore_conclude.md": ("{intent_id}",),
        "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
        "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
    }
}

MOCK_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "healthcheck": frozenset({"ok", "fail"}),
    "reason": frozenset({"complete", "intent", "noop", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_execute": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "explore_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap": frozenset({"complete", "fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
    "bootstrap_conclude": frozenset({"fact", "rejected", "invalid_json", "invalid_payload", "command_fail"}),
}

MOCK_DEFAULT_BEHAVIOR: dict[str, dict[str, Any]] = {
    "healthcheck": {
        "delay": [0.05, 0.15],
        "outcomes": {"ok": "1.0", "fail": "0.0"},
    },
    "reason": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "0.0",
            "intent": "1.0",
            "noop": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_execute": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "explore_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "complete": "1.0",
            "fact": "0.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
    "bootstrap_conclude": {
        "delay": [0.05, 0.3],
        "outcomes": {
            "fact": "1.0",
            "rejected": "0.0",
            "invalid_json": "0.0",
            "invalid_payload": "0.0",
            "command_fail": "0.0",
        },
    },
}

MOCK_ALLOWED_ENV_KEYS = frozenset(
    {f"MOCK_{phase.upper()}" for phase in MOCK_ALLOWED_OUTCOMES}
)


class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig


class ContainerConfig(BaseModel):
    image: str
    network_mode: str
    completed_action: CompletedAction
    platform: str | None = None
    cap_add: list[str] = Field(default_factory=list)


class CredentialsConfig(BaseModel):
    mode: CredentialsMode = "inject"


class CleanupPolicy(BaseModel):
    completed_action: CompletedAction = "stop"


class TerminalConfig(BaseModel):
    mode: TerminalMode = "none"


class ModelProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    type: WorkerType
    model: str
    context_window: int | None = Field(default=None, gt=0)

    @field_validator("id", "model")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class DockerEnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    label: str
    backend: Literal["docker"] = "docker"
    container: ContainerConfig
    cleanup: CleanupPolicy = Field(default_factory=CleanupPolicy)


class SshEnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    label: str
    backend: Literal["ssh"] = "ssh"
    workspace_root: str
    ssh_command: str | None = None
    host: str | None = None
    user: str | None = None
    port: int | None = Field(default=None, gt=0, le=65535)
    ssh_config: str | None = None
    identity_file: str | None = None
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)
    cleanup: CleanupPolicy = Field(default_factory=CleanupPolicy)
    terminal: TerminalConfig = Field(default_factory=TerminalConfig)
    runner_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_harness(cls, data: Any) -> Any:
        if isinstance(data, dict) and "harness" in data:
            cleaned = dict(data)
            cleaned.pop("harness", None)
            return cleaned
        return data

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: str) -> str:
        text = value.rstrip("/") or "/"
        if text in {"/", "/home", "/home/kali", "/home/kali/ctf"}:
            raise ValueError(f"unsafe SSH workspace_root: {text}")
        if text.startswith("/home/kali/ctf/"):
            raise ValueError("SSH workspace_root must not be inside /home/kali/ctf")
        return text

    @model_validator(mode="after")
    def validate_ssh_target(self) -> "SshEnvironmentConfig":
        if self.ssh_command:
            argv = shlex.split(self.ssh_command)
            if not argv:
                raise ValueError("ssh_command must not be empty")
            if argv[0] != "ssh":
                raise ValueError("ssh_command must start with ssh")
            return self
        if not self.host:
            raise ValueError("SSH environment requires ssh_command or host")
        return self

    def ssh_argv(self) -> list[str]:
        if self.ssh_command:
            return shlex.split(self.ssh_command)
        argv = ["ssh"]
        if self.ssh_config:
            argv.extend(["-F", self.ssh_config])
        if self.identity_file:
            argv.extend(["-i", self.identity_file])
        if self.port:
            argv.extend(["-p", str(self.port)])
        target = self.host if not self.user else f"{self.user}@{self.host}"
        assert target is not None
        argv.append(target)
        return argv


EnvironmentConfig = DockerEnvironmentConfig | SshEnvironmentConfig


class RuntimeConfig(BaseModel):
    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    prompt_group: str = Field(min_length=1)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str
    type: WorkerType
    model_profile: str | None = None
    endpoint: str | None = None
    task_types: list[TaskType]
    max_running: int = Field(gt=0)
    priority: int = Field(ge=0)
    env: dict[str, str] = Field(default_factory=dict)
    allowed_environments: list[str] | None = None

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        if self.type == "pi":
            _validate_optional_positive_int_env(self.name, self.env, "PI_MODEL_CONTEXT_WINDOW")
        if self.type == "mock":
            resolve_mock_behavior(self.name, self.env)
        return self


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    server: str
    runtime: RuntimeConfig
    tasks: TasksConfig
    container: ContainerConfig | None = None
    environments: list[EnvironmentConfig]
    model_profiles: list[ModelProfileConfig] = Field(default_factory=list)
    common_env: dict[str, str] = Field(default_factory=dict)
    workers: list[WorkerConfig]

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = cls._normalize_environments(data)
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            worker_copy["env"] = {
                key: _expand_env_vars(value)
                for key, value in {**common_env, **worker_env}.items()
            }
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @staticmethod
    def _normalize_environments(data: dict[str, Any]) -> dict[str, Any]:
        merged = dict(data)
        environments = merged.get("environments")
        container = merged.get("container")
        if environments is None:
            if container is None:
                return merged
            merged["environments"] = [
                {
                    "id": "docker-default",
                    "label": "Docker Default",
                    "backend": "docker",
                    "container": container,
                    "cleanup": {"completed_action": container.get("completed_action", "stop") if isinstance(container, dict) else "stop"},
                }
            ]
            return merged
        if container is not None and isinstance(environments, list):
            has_docker_default = any(isinstance(env, dict) and env.get("id") == "docker-default" for env in environments)
            if not has_docker_default:
                merged["environments"] = [
                    {
                        "id": "docker-default",
                        "label": "Docker Default",
                        "backend": "docker",
                        "container": container,
                        "cleanup": {"completed_action": container.get("completed_action", "stop") if isinstance(container, dict) else "stop"},
                    },
                    *environments,
                ]
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        if not self.workers:
            raise ValueError("workers must not be empty")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        environment_ids = [environment.id for environment in self.environments]
        if len(set(environment_ids)) != len(environment_ids):
            raise ValueError("environment ids must be unique")
        model_profile_ids = [profile.id for profile in self.model_profiles]
        if len(set(model_profile_ids)) != len(model_profile_ids):
            raise ValueError("model_profile ids must be unique")
        profiles_by_id = {profile.id: profile for profile in self.model_profiles}
        for worker in self.workers:
            profile = profiles_by_id.get(worker.model_profile or "")
            if worker.type == "mock":
                if worker.model_profile is not None and profile is None:
                    raise ValueError(f"worker {worker.name} references unknown model_profile: {worker.model_profile}")
                if profile is not None and profile.type != worker.type:
                    raise ValueError(f"worker {worker.name} model_profile {profile.id} type {profile.type} does not match worker type {worker.type}")
                continue
            if not worker.model_profile:
                raise ValueError(f"worker {worker.name} requires model_profile")
            if not worker.endpoint:
                raise ValueError(f"worker {worker.name} requires endpoint")
            if profile is None:
                raise ValueError(f"worker {worker.name} references unknown model_profile: {worker.model_profile}")
            if profile.type != worker.type:
                raise ValueError(f"worker {worker.name} model_profile {profile.id} type {profile.type} does not match worker type {worker.type}")
        return self

    @property
    def default_environment_id(self) -> str:
        return self.environments[0].id

    def environment_config(self, environment_id: str | None) -> EnvironmentConfig:
        target = environment_id or self.default_environment_id
        for environment in self.environments:
            if environment.id == target:
                return environment
        raise KeyError(target)

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        validate_prompt_resources(config.runtime.prompt_group)
        return config

    def model_profile_config(self, worker: WorkerConfig) -> ModelProfileConfig | None:
        if worker.model_profile is None:
            return None
        for profile in self.model_profiles:
            if profile.id == worker.model_profile:
                return profile
        raise KeyError(worker.model_profile)

    def worker_with_endpoint_env(
        self,
        worker: WorkerConfig,
        endpoint: ProviderEndpointPublic | None,
    ) -> WorkerConfig:
        profile = self.model_profile_config(worker)
        if profile is None:
            return worker
        if endpoint is None:
            raise ValueError(f"worker {worker.name} requires endpoint")
        return worker.model_copy(update={"env": resolve_worker_env(worker, profile, endpoint)})


def resolve_worker_env(
    worker: WorkerConfig,
    profile: ModelProfileConfig,
    endpoint: ProviderEndpointPublic,
) -> dict[str, str]:
    if profile.type != worker.type or endpoint.type != worker.type:
        raise ValueError(
            f"worker {worker.name} type={worker.type} requires matching model_profile and endpoint types"
        )
    profile_env: dict[str, str] = {}
    if profile.type == "pi":
        profile_env["PI_MODEL"] = profile.model
        profile_env["PI_BASE_URL"] = _required_endpoint_field(endpoint, "base_url")
        profile_env["PI_PROVIDER_API"] = _required_endpoint_field(endpoint, "provider_api")
        profile_env["PI_API_KEY"] = _required_endpoint_field(endpoint, "api_key")
        if profile.context_window is not None:
            profile_env["PI_MODEL_CONTEXT_WINDOW"] = str(profile.context_window)
    elif profile.type == "codex":
        profile_env["CODEX_MODEL"] = profile.model
        profile_env["CODEX_BASE_URL"] = _required_endpoint_field(endpoint, "base_url")
        profile_env["OPENAI_API_KEY"] = _required_endpoint_field(endpoint, "api_key")
    elif profile.type == "claudecode":
        profile_env["ANTHROPIC_MODEL"] = profile.model
        profile_env["ANTHROPIC_BASE_URL"] = _required_endpoint_field(endpoint, "base_url")
        profile_env["ANTHROPIC_AUTH_TOKEN"] = _required_endpoint_field(endpoint, "api_key")
    return {**worker.env, **profile_env}


def _required_endpoint_field(endpoint: ProviderEndpointPublic, name: str) -> str:
    value = getattr(endpoint, name, None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"endpoint {endpoint.id} missing field: {name}")
    return value.strip()


def _validate_optional_positive_int_env(worker_name: str, env: dict[str, str], key: str) -> None:
    value = env.get(key)
    if value is None or not value.strip():
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"worker {worker_name} env {key} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"worker {worker_name} env {key} must be greater than 0")


def _expand_env_vars(value: str) -> str:
    if not isinstance(value, str):
        return value
    return os.path.expandvars(value)


def validate_prompt_resources(prompt_group: str) -> None:
    prompts_dir = resources.files("cairn.dispatcher.prompts")
    group_dir = prompts_dir.joinpath(prompt_group)
    if not group_dir.is_dir():
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = PROMPT_REQUIRED_TOKENS_BY_GROUP.get(prompt_group, DEFAULT_PROMPT_REQUIRED_TOKENS)
    for name, tokens in required_tokens.items():
        try:
            content = group_dir.joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}")


def resolve_mock_behavior(worker_name: str, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    unknown = sorted(key for key in env if key.startswith("MOCK_") and key not in MOCK_ALLOWED_ENV_KEYS)
    if unknown:
        raise ValueError(f"worker {worker_name} has unsupported mock env keys: {', '.join(unknown)}")

    behavior: dict[str, dict[str, Any]] = {}
    for phase, allowed_outcomes in MOCK_ALLOWED_OUTCOMES.items():
        prefix = _mock_env_prefix(phase)
        payload = _parse_mock_phase_payload(worker_name, env, prefix, MOCK_DEFAULT_BEHAVIOR[phase])
        min_delay, max_delay = _parse_mock_delay_range(worker_name, prefix, payload.get("delay"))
        if max_delay < min_delay:
            raise ValueError(f"worker {worker_name} {prefix}.delay[1] must be greater than or equal to delay[0]")
        raw_outcomes = payload.get("outcomes")
        if not isinstance(raw_outcomes, dict):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes must be an object")
        unknown_outcomes = sorted(set(raw_outcomes) - allowed_outcomes)
        if unknown_outcomes:
            raise ValueError(f"worker {worker_name} {prefix}.outcomes has unsupported keys: {', '.join(unknown_outcomes)}")
        outcomes: dict[str, float] = {}
        total = Decimal("0")
        for outcome in sorted(allowed_outcomes):
            weight = _parse_mock_probability(
                worker_name,
                prefix,
                raw_outcomes,
                outcome,
            )
            outcomes[outcome] = float(weight)
            total += weight
        if total != Decimal("1"):
            raise ValueError(f"worker {worker_name} {prefix}.outcomes probabilities must sum to 1.0, got {total}")
        behavior[phase] = {
            "delay": {"min": min_delay, "max": max_delay},
            "outcomes": outcomes,
        }
        rules = payload.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ValueError(f"worker {worker_name} {prefix}.rules must be an array")
            normalized_rules: list[dict[str, Any]] = []
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"worker {worker_name} {prefix}.rules[{index}] must be an object")
                force = rule.get("force")
                if not isinstance(force, str) or force not in allowed_outcomes:
                    raise ValueError(
                        f"worker {worker_name} {prefix}.rules[{index}].force must be one of: {', '.join(sorted(allowed_outcomes))}"
                    )
                entry: dict[str, Any] = {"force": force}
                if "fact_ids_gte" in rule:
                    value = rule["fact_ids_gte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_gte must be a non-negative integer")
                    entry["fact_ids_gte"] = value
                if "fact_ids_lte" in rule:
                    value = rule["fact_ids_lte"]
                    if not isinstance(value, int) or value < 0:
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].fact_ids_lte must be a non-negative integer")
                    entry["fact_ids_lte"] = value
                if "open_intents_empty" in rule:
                    value = rule["open_intents_empty"]
                    if not isinstance(value, bool):
                        raise ValueError(f"worker {worker_name} {prefix}.rules[{index}].open_intents_empty must be boolean")
                    entry["open_intents_empty"] = value
                normalized_rules.append(entry)
            behavior[phase]["rules"] = normalized_rules
    return behavior


def _mock_env_prefix(phase: str) -> str:
    return f"MOCK_{phase.upper()}"


def _parse_mock_phase_payload(worker_name: str, env: dict[str, str], key: str, default: dict[str, Any]) -> dict[str, Any]:
    raw = env.get(key)
    if raw is None:
        return json.loads(json.dumps(default))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker {worker_name} {key} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"worker {worker_name} {key} must be a JSON object")
    return value


def _parse_mock_delay_range(worker_name: str, key: str, value: Any) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"worker {worker_name} {key}.delay must be a two-element number array")
    min_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[0]", value[0])
    max_delay = _coerce_mock_seconds(worker_name, f"{key}.delay[1]", value[1])
    return min_delay, max_delay


def _coerce_mock_seconds(worker_name: str, key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"worker {worker_name} {key} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"worker {worker_name} {key} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"worker {worker_name} {key} must be non-negative")
    return parsed


def _parse_mock_probability(worker_name: str, phase_key: str, outcomes: dict[str, Any], outcome: str) -> Decimal:
    raw = outcomes.get(outcome, MOCK_DEFAULT_BEHAVIOR[phase_key.removeprefix("MOCK_").lower()]["outcomes"][outcome])
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be a decimal probability") from exc
    if value < 0 or value > 1:
        raise ValueError(f"worker {worker_name} {phase_key}.outcomes.{outcome} must be between 0 and 1")
    return value
