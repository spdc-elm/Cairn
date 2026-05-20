from __future__ import annotations

from dataclasses import dataclass

from cairn.dispatcher.runtime.cancellation import TaskCancellation


@dataclass(slots=True)
class RunningTask:
    project_id: str
    task_type: str
    worker_name: str
    cancellation: TaskCancellation
    environment_id: str | None = None
    worker_type: str | None = None
    endpoint_id: str | None = None
    model_profile_id: str | None = None
    intent_id: str | None = None
    execution_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    open_intent_count: int | None = None


@dataclass(slots=True)
class TaskOutcome:
    status: str
    worker_health: dict | None = None


@dataclass(slots=True)
class ReasonCheckpoint:
    fact_count: int
    hint_count: int
    open_intent_count: int
