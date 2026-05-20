from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RawRunLog:
    path: Path
    records: list[dict[str, Any]]
    malformed_lines: list[str] = field(default_factory=list)

    @property
    def run_log_id(self) -> str:
        if self.records:
            value = self.records[0].get("run_id")
            if isinstance(value, str) and value:
                return value
        return self.path.stem

    @property
    def project_id(self) -> str:
        if self.records:
            value = self.records[0].get("project_id")
            if isinstance(value, str) and value:
                return value
        return self.path.parent.name

    @property
    def worker_name(self) -> str | None:
        if self.records:
            value = self.records[0].get("worker")
            if isinstance(value, str) and value:
                return value
        return None

    def stream_text(self, stream: str) -> str:
        parts: list[str] = []
        for record in self.records:
            if record.get("event") != "stream" or record.get("stream") != stream:
                continue
            text = record.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)


def read_run_log_full(path: Path) -> RawRunLog:
    records: list[dict[str, Any]] = []
    malformed: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                malformed.append(line.rstrip("\n"))
                continue
            if isinstance(payload, dict):
                records.append(payload)
            else:
                malformed.append(line.rstrip("\n"))
    return RawRunLog(path=path, records=records, malformed_lines=malformed)
