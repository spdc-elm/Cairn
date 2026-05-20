from __future__ import annotations

from typing import Any

from cairn.server.transcripts.models import TranscriptEvent
from cairn.server.transcripts.parsers.base import iter_json_lines, make_event, preview_json


PARSER_NAME = "codex"


def parse(stdout: str, stderr: str) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    seq = 0
    for payload in iter_json_lines(stdout):
        seq += 1
        if not isinstance(payload, dict):
            events.append(make_event(seq, source="worker_stdout", kind="raw", text=payload, raw=payload))
            continue
        event_type = payload.get("type")
        if event_type == "thread.started":
            events.append(
                make_event(
                    seq,
                    source="worker_stdout",
                    kind="run_started",
                    title="Codex thread",
                    text=_str(payload.get("thread_id")),
                    raw=payload,
                )
            )
            continue
        if event_type == "turn.started":
            events.append(make_event(seq, source="worker_stdout", kind="thinking", title="Turn started", raw=payload))
            continue
        if event_type == "turn.completed":
            events.append(make_event(seq, source="worker_stdout", kind="thinking", title="Turn completed", status="success", raw=payload))
            continue
        item = payload.get("item")
        if isinstance(item, dict):
            mapped = _event_from_item(seq, item, payload)
            if mapped is not None:
                events.append(mapped)
                continue
        events.append(make_event(seq, source="worker_stdout", kind="raw", raw=payload))
    if stderr:
        seq += 1
        events.append(make_event(seq, source="worker_stderr", kind="raw", text=stderr, raw=stderr))
    return events


def _event_from_item(seq: int, item: dict[str, Any], raw: dict[str, Any]) -> TranscriptEvent | None:
    item_type = item.get("type")
    if item_type == "agent_message":
        return make_event(
            seq,
            source="worker_stdout",
            kind="message",
            role="assistant",
            text=_str(item.get("text")),
            raw=raw,
        )
    if item_type == "command_execution":
        status = _status(item.get("status"), item.get("exit_code"))
        if raw.get("type") == "item.started":
            return make_event(
                seq,
                source="worker_stdout",
                kind="tool_call",
                title="Command",
                tool_name="command",
                tool_args_preview=_str(item.get("command")),
                status="running",
                raw=raw,
            )
        return make_event(
            seq,
            source="worker_stdout",
            kind="tool_result",
            title="Command result",
            text=_str(item.get("aggregated_output")),
            tool_name="command",
            tool_args_preview=preview_json({"command": item.get("command"), "exit_code": item.get("exit_code")}),
            status=status,
            raw=raw,
        )
    return None


def _status(status: object, exit_code: object) -> str | None:
    if status == "in_progress":
        return "running"
    if isinstance(exit_code, int):
        return "success" if exit_code == 0 else "error"
    if status == "completed":
        return "success"
    return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
