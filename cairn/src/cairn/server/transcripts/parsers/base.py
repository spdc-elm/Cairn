from __future__ import annotations

import json
from typing import Any

from cairn.server.transcripts.models import TranscriptEvent


def iter_json_lines(text: str) -> list[dict[str, Any] | str]:
    events: list[dict[str, Any] | str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            events.append(stripped)
            continue
        events.append(payload if isinstance(payload, dict) else stripped)
    return events


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def preview_json(value: Any, limit: int = 400) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def make_event(
    seq: int,
    *,
    source: str,
    kind: str,
    role: str | None = None,
    title: str | None = None,
    text: str | None = None,
    tool_name: str | None = None,
    tool_args_preview: str | None = None,
    status: str | None = None,
    raw: dict[str, Any] | str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        id=f"evt_{seq:06d}",
        seq=seq,
        source=source,
        kind=kind,
        role=role,
        title=title,
        text=text,
        tool_name=tool_name,
        tool_args_preview=tool_args_preview,
        status=status,
        raw=raw,
    )
