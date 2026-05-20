from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from cairn.server.models import RunProvenance


TranscriptSource = Literal["run_log", "worker_stdout", "worker_stderr", "parser"]
TranscriptKind = Literal["run_started", "run_finished", "message", "tool_call", "tool_result", "thinking", "error", "raw"]
TranscriptRole = Literal["user", "assistant", "system", "tool"] | None
TranscriptStatus = Literal["running", "success", "error", "cancelled"] | None


class TranscriptEvent(BaseModel):
    id: str
    ts: str | None = None
    seq: int
    source: TranscriptSource
    kind: TranscriptKind
    role: TranscriptRole = None
    title: str | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_args_preview: str | None = None
    status: TranscriptStatus = None
    raw: dict[str, Any] | str | None = None
    collapsed: bool = False


class TranscriptResponse(BaseModel):
    run_log_id: str
    project_id: str
    provenance: RunProvenance | None = None
    events: list[TranscriptEvent] = Field(default_factory=list)
    events_omitted_before: int = 0
    large_event_collapsed: bool = False
    parser: str
    raw_available: bool = True
