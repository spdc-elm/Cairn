from __future__ import annotations

from cairn.server.transcripts.models import TranscriptEvent
from cairn.server.transcripts.parsers.base import make_event


PARSER_NAME = "raw"


def parse(stdout: str, stderr: str) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    seq = 0
    if stdout:
        seq += 1
        events.append(make_event(seq, source="worker_stdout", kind="raw", text=stdout, raw=stdout))
    if stderr:
        seq += 1
        events.append(make_event(seq, source="worker_stderr", kind="raw", text=stderr, raw=stderr))
    return events
