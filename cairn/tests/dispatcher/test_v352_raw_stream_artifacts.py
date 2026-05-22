from __future__ import annotations

import json
import unittest

from cairn.dispatcher.runtime.event_sink import ExecutionEventSink
from cairn.dispatcher.workers.adapters.pi import PiStreamProjector


class FakeResponse:
    ok = True
    status_code = 200
    text = ""


class FakeClient:
    def __init__(self):
        self.events = []

    def append_execution_events(self, execution_id, *, dispatcher_id=None, events=None):
        self.events.extend(events or [])
        return FakeResponse()


class RawStreamArtifactTests(unittest.TestCase):
    def test_stdout_db_preview_is_bounded_and_sanitises_pi_agent_end_messages(self) -> None:
        client = FakeClient()
        sink = ExecutionEventSink(client, "ex001", event_projector=PiStreamProjector("ex001"), raw_ref={"kind": "metadata", "key": "raw_stream"})
        messages = [{"role": "user", "content": [{"type": "text", "text": "u" * 1000}]} for _ in range(20)]
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "FINAL ANSWER TAIL"}]})
        line = json.dumps({"type": "agent_end", "messages": messages}) + "\n"

        sink.write_stream("stdout", line * 20)
        sink.flush()
        sink.flush_projector_events()
        sink.flush()

        stdout_payloads = [event["payload"] for event in client.events if event["event_type"] == "stdout"]
        message_payloads = [event["payload"] for event in client.events if event["event_type"] == "message"]
        stdout_text = "\n".join(payload.get("text", "") for payload in stdout_payloads)

        self.assertLess(sum(len(json.dumps(payload).encode("utf-8")) for payload in stdout_payloads), 16 * 1024)
        self.assertIn("messages_omitted", stdout_text)
        self.assertNotIn('"messages":', stdout_text)
        self.assertTrue(any(payload.get("text") == "FINAL ANSWER TAIL" for payload in message_payloads))

    def test_pi_projector_does_not_keep_full_agent_end_messages(self) -> None:
        projector = PiStreamProjector("ex001")
        messages = [{"role": "user", "content": [{"type": "text", "text": "u" * 1000}]} for _ in range(50)]
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "final"}]})
        projector.feed("stdout", json.dumps({"type": "agent_end", "messages": messages}) + "\n")

        self.assertFalse(hasattr(projector, "_pending_agent_messages"))
        events = projector.close()
        self.assertEqual(events[-1].payload["text"], "final")


if __name__ == "__main__":
    unittest.main()
