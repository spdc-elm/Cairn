from __future__ import annotations

from typing import Any

from cairn.server.transcripts.models import TranscriptEvent
from cairn.server.transcripts.parsers.base import iter_json_lines, make_event, preview_json, text_from_content


PARSER_NAME = "claudecode"


def parse(stdout: str, stderr: str) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    seq = 0
    for payload in iter_json_lines(stdout):
        seq += 1
        if not isinstance(payload, dict):
            events.append(make_event(seq, source="worker_stdout", kind="raw", text=payload, raw=payload))
            continue
        event_type = payload.get("type")
        if event_type == "system":
            events.append(
                make_event(
                    seq,
                    source="worker_stdout",
                    kind="run_started",
                    title=_str(payload.get("subtype")) or "Claude system",
                    text=_str(payload.get("session_id")),
                    raw=payload,
                )
            )
            continue
        if event_type == "assistant":
            message = payload.get("message")
            events.extend(_message_events(seq, message, payload))
            continue
        if event_type == "user":
            message = payload.get("message")
            text = text_from_content(message.get("content") if isinstance(message, dict) else None)
            events.append(make_event(seq, source="worker_stdout", kind="message", role="user", text=text, raw=payload))
            continue
        if event_type == "result":
            events.append(
                make_event(
                    seq,
                    source="worker_stdout",
                    kind="message",
                    role="assistant",
                    title="Result",
                    text=_str(payload.get("result")) or preview_json(payload.get("result")),
                    status="success",
                    raw=payload,
                )
            )
            continue
        events.append(make_event(seq, source="worker_stdout", kind="raw", raw=payload))
    if stderr:
        seq += 1
        events.append(make_event(seq, source="worker_stderr", kind="raw", text=stderr, raw=stderr))
    return events


def _message_events(seq: int, message: Any, raw: dict[str, Any]) -> list[TranscriptEvent]:
    if not isinstance(message, dict):
        return [make_event(seq, source="worker_stdout", kind="raw", raw=raw)]
    content = message.get("content")
    text = text_from_content(content)
    events: list[TranscriptEvent] = []
    if text:
        events.append(make_event(seq, source="worker_stdout", kind="message", role="assistant", text=text, raw=raw))
        seq += 1
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                events.append(
                    make_event(
                        seq,
                        source="worker_stdout",
                        kind="tool_call",
                        title=_str(item.get("name")),
                        tool_name=_str(item.get("name")),
                        tool_args_preview=preview_json(item.get("input")),
                        status="running",
                        raw=raw,
                    )
                )
                seq += 1
            elif item.get("type") == "tool_result":
                events.append(
                    make_event(
                        seq,
                        source="worker_stdout",
                        kind="tool_result",
                        text=text_from_content(item.get("content")) or preview_json(item),
                        status="success" if not item.get("is_error") else "error",
                        raw=raw,
                    )
                )
                seq += 1
    return events or [make_event(seq, source="worker_stdout", kind="raw", raw=raw)]


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
