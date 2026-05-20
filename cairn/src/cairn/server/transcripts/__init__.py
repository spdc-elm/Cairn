from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cairn.server.models import RunProvenance
from cairn.server.transcripts.models import TranscriptEvent, TranscriptResponse
from cairn.server.transcripts.run_log_reader import read_run_log_full
from cairn.server.transcripts.parsers import claudecode, codex, pi, raw


Parser = Callable[[str, str], list[TranscriptEvent]]
MAX_LIMIT_EVENTS = 1000
DEFAULT_LIMIT_EVENTS = 200
LARGE_EVENT_TEXT_LIMIT = 12_000

_PARSERS: dict[str, tuple[str, Parser]] = {
    "codex": (codex.PARSER_NAME, codex.parse),
    "pi": (pi.PARSER_NAME, pi.parse),
    "claudecode": (claudecode.PARSER_NAME, claudecode.parse),
    "claude": (claudecode.PARSER_NAME, claudecode.parse),
    "raw": (raw.PARSER_NAME, raw.parse),
}


def build_transcript_from_path(
    path: Path,
    *,
    worker_type: str | None = None,
    provenance: RunProvenance | None = None,
    limit_events: int = DEFAULT_LIMIT_EVENTS,
) -> TranscriptResponse:
    run_log = read_run_log_full(path)
    inferred_worker_type = worker_type or (provenance.worker_type if provenance else None) or _infer_worker_type(run_log.worker_name)
    parser_name, parser = _PARSERS.get(inferred_worker_type or "", _PARSERS["raw"])
    stdout = run_log.stream_text("stdout")
    stderr = run_log.stream_text("stderr")
    events = _with_outer_events(parser(stdout, stderr), run_log.records)
    for line in run_log.malformed_lines:
        events.append(
            TranscriptEvent(
                id=f"evt_{len(events) + 1:06d}",
                seq=len(events) + 1,
                source="run_log",
                kind="error",
                title="Malformed run log line",
                text=line,
                raw=line,
            )
        )
    collapsed, large_event_collapsed = _collapse_large_events(events)
    limited, omitted = _limit_events(collapsed, limit_events)
    return TranscriptResponse(
        run_log_id=run_log.run_log_id,
        project_id=run_log.project_id,
        provenance=provenance,
        events=limited,
        events_omitted_before=omitted,
        large_event_collapsed=large_event_collapsed,
        parser=parser_name,
        raw_available=True,
    )


def build_transcript_from_streams(
    stdout: str,
    stderr: str,
    *,
    project_id: str,
    run_log_id: str,
    worker_type: str | None = None,
    provenance: RunProvenance | None = None,
    limit_events: int = DEFAULT_LIMIT_EVENTS,
) -> TranscriptResponse:
    inferred_worker_type = worker_type or (provenance.worker_type if provenance else None)
    parser_name, parser = _PARSERS.get(inferred_worker_type or "", _PARSERS["raw"])
    events = parser(stdout, stderr)
    collapsed, large_event_collapsed = _collapse_large_events(events)
    limited, omitted = _limit_events(collapsed, limit_events)
    return TranscriptResponse(
        run_log_id=run_log_id,
        project_id=project_id,
        provenance=provenance,
        events=limited,
        events_omitted_before=omitted,
        large_event_collapsed=large_event_collapsed,
        parser=parser_name,
        raw_available=bool(stdout or stderr),
    )


def _with_outer_events(events: list[TranscriptEvent], records: list[dict]) -> list[TranscriptEvent]:
    output: list[TranscriptEvent] = []
    first = records[0] if records else None
    finished = next((record for record in reversed(records) if record.get("event") == "run_finished"), None)
    if first is not None:
        output.append(
            TranscriptEvent(
                id="evt_outer_start",
                ts=first.get("ts") if isinstance(first.get("ts"), str) else None,
                seq=0,
                source="run_log",
                kind="run_started",
                title=f"{first.get('task_type') or 'run'} / {first.get('phase') or ''}".strip(" /"),
                raw=first,
            )
        )
    offset = len(output)
    for index, event in enumerate(events, start=1):
        event.seq = index + offset
        event.id = f"evt_{event.seq:06d}"
        output.append(event)
    if finished is not None:
        status = "success" if finished.get("returncode") == 0 and not finished.get("timed_out") and not finished.get("cancelled") else "error"
        output.append(
            TranscriptEvent(
                id=f"evt_{len(output) + 1:06d}",
                ts=finished.get("ts") if isinstance(finished.get("ts"), str) else None,
                seq=len(output) + 1,
                source="run_log",
                kind="run_finished",
                title="Run finished",
                status=status,
                raw=finished,
            )
        )
    return output


def _limit_events(events: list[TranscriptEvent], limit_events: int) -> tuple[list[TranscriptEvent], int]:
    limit = max(1, min(limit_events, MAX_LIMIT_EVENTS))
    omitted = max(0, len(events) - limit)
    if omitted:
        return events[-limit:], omitted
    return events, 0


def _collapse_large_events(events: list[TranscriptEvent]) -> tuple[list[TranscriptEvent], bool]:
    collapsed = False
    output: list[TranscriptEvent] = []
    for event in events:
        if event.text and len(event.text) > LARGE_EVENT_TEXT_LIMIT:
            event = event.model_copy(
                update={
                    "text": event.text[:LARGE_EVENT_TEXT_LIMIT] + f"\n\n[collapsed {len(event.text) - LARGE_EVENT_TEXT_LIMIT} chars]",
                    "collapsed": True,
                }
            )
            collapsed = True
        output.append(event)
    return output, collapsed


def _infer_worker_type(worker_name: str | None) -> str | None:
    if not worker_name:
        return None
    lowered = worker_name.lower()
    if lowered.startswith("codex"):
        return "codex"
    if lowered.startswith("pi"):
        return "pi"
    if lowered.startswith("claude"):
        return "claudecode"
    return None
