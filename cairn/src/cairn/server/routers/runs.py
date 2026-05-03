from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cairn.dispatcher.runtime.run_logs import run_log_root
from cairn.server.db import get_conn
from cairn.server.services import get_project_or_404

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


@router.get("/projects/{project_id}/runs/latest", response_model=RunLogDetail)
def get_latest_project_run(project_id: str, intent_id: str | None = None):
    _ensure_project(project_id)
    summaries = list_project_runs(project_id, intent_id=intent_id, limit=1)
    if not summaries:
        raise HTTPException(404, "Run log not found")
    return _read_detail(_run_path(project_id, summaries[0].run_id))


@router.get("/projects/{project_id}/runs/{run_id}", response_model=RunLogDetail)
def get_project_run(project_id: str, run_id: str):
    _ensure_project(project_id)
    if "/" in run_id or "\\" in run_id or not run_id.startswith("run_"):
        raise HTTPException(400, "Invalid run id")
    path = _run_path(project_id, run_id)
    if not path.is_file():
        raise HTTPException(404, "Run log not found")
    return _read_detail(path)


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
