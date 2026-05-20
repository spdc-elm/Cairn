from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cairn.dispatcher.runtime.run_logs import run_log_root
from cairn.server.db import get_conn
from cairn.server.models import AnchorResolution, RemoteSessionProvenance, RunProvenance, RunProvenancePatch, RunProvenanceUpsert
from cairn.server.services import (
    create_run_provenance,
    finish_run_provenance,
    get_project_or_404,
    get_run_provenance_or_none,
    resolve_anchor,
    update_run_remote_session,
)
from cairn.server.transcripts import build_transcript_from_path
from cairn.server.transcripts.models import TranscriptResponse

router = APIRouter(tags=["runs"])

MAX_EVENTS = 600
MAX_TEXT_CHARS = 120_000


class RunLogSummary(BaseModel):
    run_id: str
    project_id: str
    intent_id: str | None = None
    task_type: str
    phase: str
    worker: str
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    timed_out: bool | None = None
    cancelled: bool | None = None
    metadata: dict[str, Any] | None = None


class RunLogEvent(BaseModel):
    ts: str
    seq: int
    event: str
    stream: str | None = None
    text: str | None = None


class RunLogDetail(BaseModel):
    summary: RunLogSummary
    events: list[RunLogEvent]
    stdout: str
    stderr: str
    combined: str
    truncated: bool


@router.get("/projects/{project_id}/runs", response_model=list[RunLogSummary])
def list_project_runs(project_id: str, intent_id: str | None = None, limit: int = 20):
    _ensure_project(project_id)
    limit = max(1, min(limit, 100))
    summaries = [_read_summary(path) for path in _project_run_paths(project_id)]
    summaries = [summary for summary in summaries if summary is not None]
    if intent_id is not None:
        summaries = [summary for summary in summaries if summary.intent_id == intent_id]
    summaries.sort(key=lambda summary: summary.started_at or "", reverse=True)
    return summaries[:limit]


@router.post("/projects/{project_id}/runs/provenance", response_model=RunProvenance, status_code=201)
def upsert_run_provenance(project_id: str, body: RunProvenanceUpsert):
    with get_conn() as conn:
        return create_run_provenance(conn, project_id=project_id, **body.model_dump())


@router.get("/projects/{project_id}/runs/{run_id}/provenance", response_model=RunProvenance)
def get_run_provenance(project_id: str, run_id: str):
    _ensure_project(project_id)
    with get_conn() as conn:
        provenance = get_run_provenance_or_none(conn, project_id, run_id)
    if provenance is None:
        raise HTTPException(404, "Run provenance not found")
    return provenance


@router.patch("/projects/{project_id}/runs/{run_id}/provenance", response_model=RunProvenance)
def patch_run_provenance(project_id: str, run_id: str, body: RunProvenancePatch):
    _ensure_project(project_id)
    with get_conn() as conn:
        provenance = get_run_provenance_or_none(conn, project_id, run_id)
        if provenance is None:
            raise HTTPException(404, "Run provenance not found")
        if body.finished_at is not None or body.returncode is not None or body.timed_out is not None or body.cancelled is not None or body.cancel_reason is not None:
            provenance = finish_run_provenance(
                conn,
                project_id,
                run_id,
                returncode=body.returncode if body.returncode is not None else provenance.returncode or 0,
                timed_out=body.timed_out if body.timed_out is not None else bool(provenance.timed_out),
                cancelled=body.cancelled if body.cancelled is not None else bool(provenance.cancelled),
                cancel_reason=body.cancel_reason,
                finished_at=body.finished_at,
            )
        if body.remote_session is not None:
            provenance = update_run_remote_session(
                conn,
                project_id,
                run_id,
                remote_session_id=body.remote_session.id,
                remote_session_kind=body.remote_session.kind,
                remote_session_status=body.remote_session.status,
                remote_session_capture_method=body.remote_session.capture_method,
            )
        assert provenance is not None
        return provenance


@router.post("/projects/{project_id}/runs/{run_id}/provenance/session", response_model=RunProvenance)
def update_run_provenance_session(project_id: str, run_id: str, body: RemoteSessionProvenance):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        provenance = update_run_remote_session(
            conn,
            project_id,
            run_id,
            remote_session_id=body.id,
            remote_session_kind=body.kind,
            remote_session_status=body.status,
            remote_session_capture_method=body.capture_method,
        )
    if provenance is None:
        raise HTTPException(404, "Run provenance not found")
    return provenance


@router.get("/projects/{project_id}/anchors/resolve", response_model=AnchorResolution)
def resolve_project_anchor(project_id: str, anchor_type: str, anchor_id: str, run_log_id: str | None = None):
    with get_conn() as conn:
        return resolve_anchor(conn, project_id, anchor_type, anchor_id, selected_run_log_id=run_log_id)


@router.get("/projects/{project_id}/runs/latest/transcript", response_model=TranscriptResponse)
def get_latest_project_run_transcript(project_id: str, intent_id: str | None = None, limit_events: int = 200):
    _ensure_project(project_id)
    summaries = list_project_runs(project_id, intent_id=intent_id, limit=1)
    if not summaries:
        raise HTTPException(404, "Run log not found")
    return _read_transcript(project_id, summaries[0].run_id, limit_events=limit_events)


@router.get("/projects/{project_id}/runs/latest", response_model=RunLogDetail)
def get_latest_project_run(project_id: str, intent_id: str | None = None):
    _ensure_project(project_id)
    summaries = list_project_runs(project_id, intent_id=intent_id, limit=1)
    if not summaries:
        raise HTTPException(404, "Run log not found")
    return _read_detail(_run_path(project_id, summaries[0].run_id))


@router.get("/projects/{project_id}/runs/{run_id}/transcript", response_model=TranscriptResponse)
def get_project_run_transcript(project_id: str, run_id: str, limit_events: int = 200):
    _ensure_project(project_id)
    return _read_transcript(project_id, run_id, limit_events=limit_events)


@router.get("/projects/{project_id}/runs/{run_id}", response_model=RunLogDetail)
def get_project_run(project_id: str, run_id: str):
    _ensure_project(project_id)
    if "/" in run_id or "\\" in run_id or not run_id.startswith("run_"):
        raise HTTPException(400, "Invalid run id")
    path = _run_path(project_id, run_id)
    if not path.is_file():
        raise HTTPException(404, "Run log not found")
    return _read_detail(path)


def _read_transcript(project_id: str, run_id: str, *, limit_events: int) -> TranscriptResponse:
    if "/" in run_id or "\\" in run_id or not run_id.startswith("run_"):
        raise HTTPException(400, "Invalid run id")
    path = _run_path(project_id, run_id)
    if not path.is_file():
        raise HTTPException(404, "Run log not found")
    with get_conn() as conn:
        provenance = get_run_provenance_or_none(conn, project_id, run_id)
    return build_transcript_from_path(
        path,
        worker_type=provenance.worker_type if provenance else None,
        provenance=provenance,
        limit_events=limit_events,
    )


def _ensure_project(project_id: str) -> None:
    with get_conn() as conn:
        get_project_or_404(conn, project_id)


def _project_run_paths(project_id: str) -> list[Path]:
    root = run_log_root() / project_id
    if not root.is_dir():
        return []
    return sorted(root.glob("run_*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)


def _run_path(project_id: str, run_id: str) -> Path:
    return run_log_root() / project_id / f"{run_id}.jsonl"


def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except FileNotFoundError:
        raise HTTPException(404, "Run log not found") from None
    return records


def _read_summary(path: Path) -> RunLogSummary | None:
    records = _read_records(path)
    if not records:
        return None
    first = records[0]
    last_finished = next((record for record in reversed(records) if record.get("event") == "run_finished"), None)
    return RunLogSummary(
        run_id=str(first.get("run_id") or path.stem),
        project_id=str(first.get("project_id") or path.parent.name),
        intent_id=_optional_str(first.get("intent_id")),
        task_type=str(first.get("task_type") or ""),
        phase=str(first.get("phase") or ""),
        worker=str(first.get("worker") or ""),
        started_at=_optional_str(first.get("ts")),
        finished_at=_optional_str(last_finished.get("ts") if last_finished else None),
        returncode=_optional_int(last_finished.get("returncode") if last_finished else None),
        timed_out=_optional_bool(last_finished.get("timed_out") if last_finished else None),
        cancelled=_optional_bool(last_finished.get("cancelled") if last_finished else None),
        metadata=first.get("metadata") if isinstance(first.get("metadata"), dict) else None,
    )


def _read_detail(path: Path) -> RunLogDetail:
    records = _read_records(path)
    if not records:
        raise HTTPException(404, "Run log not found")
    summary = _read_summary(path)
    assert summary is not None
    events: list[RunLogEvent] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    combined_parts: list[str] = []
    total_chars = 0
    truncated = False
    for record in records[-MAX_EVENTS:]:
        event = str(record.get("event") or "")
        stream = _optional_str(record.get("stream"))
        text = _optional_str(record.get("text"))
        if text is not None and stream in {"stdout", "stderr"}:
            total_chars += len(text)
            if total_chars > MAX_TEXT_CHARS:
                truncated = True
                continue
            if stream == "stdout":
                stdout_parts.append(text)
            else:
                stderr_parts.append(text)
            combined_parts.append(f"[{stream}] {text}")
        events.append(
            RunLogEvent(
                ts=str(record.get("ts") or ""),
                seq=int(record.get("seq") or 0),
                event=event,
                stream=stream,
                text=text,
            )
        )
    return RunLogDetail(
        summary=summary,
        events=events,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        combined="".join(combined_parts),
        truncated=truncated,
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
