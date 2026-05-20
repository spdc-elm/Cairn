from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cairn.server.models import AnchorResolution, RemoteSessionProvenance
from cairn.server.transcripts.models import TranscriptEvent


QuestionAnchorType = Literal["fact", "intent", "run"]
QuestionMode = Literal["auto", "fork", "resume", "fresh_context"]
ResolvedQuestionMode = Literal["fork", "resume", "fresh_context"]
SessionEffect = Literal["forked", "continued", "fresh"]
QuestionStatus = Literal["active", "closed", "failed"]
QuestionThreadStatus = Literal["active", "closing", "closed", "failed", "expired"]
QuestionJobStatus = Literal["pending", "claimed", "running", "succeeded", "failed", "cancelled"]
QuestionRole = Literal["user", "assistant", "system", "tool"]
PromotionKind = Literal["hint", "fact", "intent"]


class SourceSession(BaseModel):
    kind: str | None = None
    id: str | None = None
    status: str = "missing"


class QuestionJob(BaseModel):
    id: str
    thread_id: str
    project_id: str
    seq: int
    mode: ResolvedQuestionMode
    message: str
    prompt_context: dict[str, Any] | None = None
    status: QuestionJobStatus
    claimed_by: str | None = None
    claimed_at: str | None = None
    claim_expires_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result_text: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    run_log_id: str | None = None
    question_session: RemoteSessionProvenance | None = None
    created_at: str
    updated_at: str


class QuestionEvent(BaseModel):
    id: str
    thread_id: str
    job_id: str | None = None
    seq: int
    event_key: str | None = None
    event: dict[str, Any]
    created_at: str


class QuestionCreateRequest(BaseModel):
    anchor_type: QuestionAnchorType
    anchor_id: str
    mode: QuestionMode = "auto"
    message: str | None = None
    worker_name: str | None = None
    allow_resume_without_fork: bool = False
    confirm_resume: bool = False

    @field_validator("anchor_id", "message", "worker_name")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class QuestionMessageRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class QuestionMessage(BaseModel):
    id: str
    role: QuestionRole
    text: str | None = None
    events: list[TranscriptEvent] = Field(default_factory=list)
    created_at: str


class QuestionThread(BaseModel):
    id: str
    project_id: str
    anchor_type: QuestionAnchorType
    anchor_id: str
    worker_name: str | None = None
    execution_environment_id: str | None = None
    execution_worker_type: str | None = None
    execution_endpoint_id: str | None = None
    execution_model_profile_id: str | None = None
    source_run_log_id: str | None = None
    anchor_resolution: AnchorResolution
    source_session: RemoteSessionProvenance = Field(default_factory=RemoteSessionProvenance)
    question_session: RemoteSessionProvenance | None = None
    mode: ResolvedQuestionMode
    session_effect: SessionEffect
    status: QuestionThreadStatus = "active"
    notice: str | None = None
    messages: list[QuestionMessage] = Field(default_factory=list)
    jobs: list[QuestionJob] = Field(default_factory=list)
    events: list[QuestionEvent] = Field(default_factory=list)
    active_job: QuestionJob | None = None
    expires_at: str | None = None
    created_at: str
    updated_at: str


class QuestionClaimRequest(BaseModel):
    dispatcher_id: str
    worker_names: list[str] = Field(default_factory=list)
    environment_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=1, ge=1, le=10)


class QuestionClaimJob(BaseModel):
    id: str
    thread_id: str
    project_id: str
    mode: ResolvedQuestionMode
    worker_name: str | None = None
    execution_environment_id: str | None = None
    execution_worker_type: str | None = None
    execution_endpoint_id: str | None = None
    execution_model_profile_id: str | None = None
    source_session: SourceSession = Field(default_factory=SourceSession)
    prompt_context: dict[str, Any] | None = None
    message: str


class QuestionClaimResponse(BaseModel):
    job: QuestionClaimJob | None = None


class QuestionJobEventPayload(BaseModel):
    event_key: str
    event: dict[str, Any]


class QuestionJobEventsRequest(BaseModel):
    dispatcher_id: str
    batch_id: str
    events: list[QuestionJobEventPayload] = Field(default_factory=list)


class QuestionJobTerminalRequest(BaseModel):
    dispatcher_id: str
    result_text: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    run_log_id: str | None = None
    question_remote_session: RemoteSessionProvenance | None = None


class QuestionJobHeartbeatRequest(BaseModel):
    dispatcher_id: str


class QuestionCloseResponse(BaseModel):
    id: str
    status: QuestionThreadStatus


class QuestionPromotionRequest(BaseModel):
    kind: PromotionKind
    content: str
    title: str | None = None
    from_fact_ids: list[str] | None = None
    answer_summary: str | None = None

    @field_validator("content", "title", "answer_summary")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class QuestionPromotionResponse(BaseModel):
    kind: PromotionKind
    object_id: str
