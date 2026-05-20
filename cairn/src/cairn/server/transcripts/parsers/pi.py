from __future__ import annotations

from typing import Any

from cairn.server.transcripts.models import TranscriptEvent
from cairn.server.transcripts.parsers.base import iter_json_lines, make_event, preview_json, text_from_content


PARSER_NAME = "pi"


def parse(stdout: str, stderr: str) -> list[TranscriptEvent]:
    raw_events = iter_json_lines(stdout)
    output: list[TranscriptEvent] = []
    messages: list[dict[str, Any]] = []
    tool_events: dict[str, dict[str, Any]] = {}
    session_id: str | None = None

    for payload in raw_events:
        if not isinstance(payload, dict):
            messages.append({"role": "system", "text": payload, "raw": payload})
            continue
        event_type = payload.get("type")
        if event_type == "session":
            candidate = payload.get("id")
            session_id = candidate if isinstance(candidate, str) else session_id
            continue
        if event_type in {"message_start", "message_update", "message_end"}:
            message = payload.get("message")
            if isinstance(message, dict):
                _upsert_message(messages, message, payload)
            continue
        if event_type in {"tool_execution_start", "tool_execution_update", "tool_execution_end"}:
            tool_id = _str(payload.get("toolCallId")) or f"tool-{len(tool_events) + 1}"
            existing = tool_events.setdefault(tool_id, {"id": tool_id, "updates": []})
            existing.update({key: payload.get(key) for key in ("toolName", "args") if key in payload})
            existing["raw"] = payload
            if event_type == "tool_execution_end":
                existing["result"] = payload.get("result")
                existing["is_error"] = bool(payload.get("isError"))
            elif event_type == "tool_execution_update":
                existing["updates"].append(payload)
            continue

    seq = 0
    if session_id:
        seq += 1
        output.append(make_event(seq, source="worker_stdout", kind="run_started", title="Pi session", text=session_id))
    for message in messages:
        seq += 1
        output.extend(_message_to_events(seq, message))
        seq = output[-1].seq if output else seq
    for tool in tool_events.values():
        seq += 1
        output.append(
            make_event(
                seq,
                source="worker_stdout",
                kind="tool_call",
                title=tool.get("toolName"),
                tool_name=_str(tool.get("toolName")),
                tool_args_preview=preview_json(tool.get("args")),
                status="running",
                raw=tool.get("raw"),
            )
        )
        seq += 1
        output.append(
            make_event(
                seq,
                source="worker_stdout",
                kind="tool_result",
                title=f"{tool.get('toolName') or 'Tool'} result",
                text=_result_text(tool.get("result")),
                tool_name=_str(tool.get("toolName")),
                status="error" if tool.get("is_error") else "success",
                raw=tool.get("result"),
            )
        )
    if stderr:
        seq += 1
        output.append(make_event(seq, source="worker_stderr", kind="raw", text=stderr, raw=stderr))
    return output


def _upsert_message(messages: list[dict[str, Any]], message: dict[str, Any], raw: dict[str, Any]) -> None:
    role = _normalize_role(_str(message.get("role")))
    text = text_from_content(message.get("content"))
    tool_calls = _tool_calls_from_content(message.get("content"))
    key = (role, len(messages))
    if messages and messages[-1].get("role") == role:
        key = messages[-1]["key"]
    item = next((candidate for candidate in reversed(messages) if candidate.get("key") == key), None)
    if item is None:
        item = {"key": key, "role": role, "text": "", "tool_calls": [], "raw": raw}
        messages.append(item)
    if text:
        item["text"] = text
    if tool_calls:
        item["tool_calls"] = tool_calls
    item["raw"] = raw


def _message_to_events(seq: int, message: dict[str, Any]) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    text = _str(message.get("text"))
    if text:
        events.append(
            make_event(
                seq,
                source="worker_stdout",
                kind="message",
                role=message.get("role"),
                text=text,
                raw=message.get("raw"),
            )
        )
        seq += 1
    for call in message.get("tool_calls") or []:
        events.append(
            make_event(
                seq,
                source="worker_stdout",
                kind="tool_call",
                title=call.get("name"),
                tool_name=call.get("name"),
                tool_args_preview=preview_json(call.get("arguments") or call.get("partialArgs")),
                status="running",
                raw=message.get("raw"),
            )
        )
        seq += 1
    return events


def _tool_calls_from_content(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "toolCall":
            continue
        calls.append(
            {
                "id": _str(item.get("id")),
                "name": _str(item.get("name")),
                "arguments": item.get("arguments"),
                "partialArgs": item.get("partialArgs"),
            }
        )
    return calls


def _result_text(result: Any) -> str | None:
    if isinstance(result, dict):
        text = text_from_content(result.get("content"))
        if text:
            return text
    return preview_json(result, limit=2000)


def _normalize_role(role: str | None) -> str:
    if role == "toolResult":
        return "tool"
    if role in {"user", "assistant", "system", "tool"}:
        return role
    return "assistant"


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
