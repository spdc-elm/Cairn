from __future__ import annotations

from pathlib import Path
import unittest

from cairn.server.transcripts import build_transcript_from_path


FIXTURES = Path(__file__).parents[1] / "fixtures" / "run_logs"


class V3TranscriptParserTests(unittest.TestCase):
    def test_codex_fixture_maps_messages_and_command_execution(self) -> None:
        transcript = build_transcript_from_path(FIXTURES / "codex_conclude.jsonl", worker_type="codex", limit_events=50)

        self.assertEqual(transcript.parser, "codex")
        self.assertTrue(any(event.kind == "message" and event.role == "assistant" and "accepted" in (event.text or "") for event in transcript.events))
        self.assertTrue(any(event.kind == "tool_call" and event.tool_name == "command" for event in transcript.events))
        self.assertTrue(any(event.kind == "tool_result" and "hi" in (event.text or "") for event in transcript.events))

    def test_pi_fixture_coalesces_message_updates_and_split_inner_json_lines(self) -> None:
        transcript = build_transcript_from_path(FIXTURES / "pi_large_stream.jsonl", worker_type="pi", limit_events=50)

        messages = [event for event in transcript.events if event.kind == "message"]
        tools = [event for event in transcript.events if event.kind in {"tool_call", "tool_result"}]

        self.assertEqual(transcript.parser, "pi")
        self.assertEqual([message.text for message in messages], ["hello world"])
        self.assertEqual(len(tools), 2)
        self.assertLess(len(transcript.events), 8)

    def test_claude_fixture_maps_assistant_and_result_events(self) -> None:
        transcript = build_transcript_from_path(FIXTURES / "claude_stream_json.jsonl", worker_type="claudecode", limit_events=50)

        self.assertEqual(transcript.parser, "claudecode")
        self.assertTrue(any(event.kind == "message" and event.text == "working" for event in transcript.events))
        self.assertTrue(any(event.kind == "message" and "accepted" in (event.text or "") for event in transcript.events))

    def test_limit_applies_after_normalization(self) -> None:
        transcript = build_transcript_from_path(FIXTURES / "pi_large_stream.jsonl", worker_type="pi", limit_events=1)

        self.assertEqual(len(transcript.events), 1)
        self.assertGreater(transcript.events_omitted_before, 0)
        self.assertEqual(transcript.parser, "pi")


if __name__ == "__main__":
    unittest.main()
