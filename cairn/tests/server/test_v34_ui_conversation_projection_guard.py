from __future__ import annotations

from pathlib import Path
import unittest


HTML = Path("cairn/src/cairn/server/static/index.html")


class V34UiConversationProjectionGuardTests(unittest.TestCase):
    def test_conversation_exhausts_cursor_and_does_not_parse_raw_stdout_jsonl(self) -> None:
        html = HTML.read_text(encoding="utf-8")
        load_start = html.index("async loadSelectedConversation")
        load_end = html.index("\n    executionTranscript", load_start)
        load_block = html[load_start:load_end]
        projection_start = html.index("executionEventToConversationEvents(event)")
        projection_end = html.index("\n    messageStreamKey", projection_start)
        projection_block = html[projection_start:projection_end]

        self.assertIn("loadAllExecutionEvents", load_block)
        self.assertIn("loadAllBranchTimeline", load_block)
        self.assertIn("after_cursor", html)
        self.assertNotIn("workerJsonLinesToConversationEvents", projection_block)
        self.assertIn("kind: 'raw'", projection_block)

    def test_question_polling_has_no_fixed_120_iteration_cutoff(self) -> None:
        html = HTML.read_text(encoding="utf-8")
        poll_start = html.index("async pollQuestionThread")
        poll_end = html.index("async closeQuestionThread", poll_start)
        poll_block = html[poll_start:poll_end]

        self.assertIn("while (true)", poll_block)
        self.assertNotIn("i < 120", poll_block)
        self.assertIn("loadAllBranchTimeline", poll_block)
        self.assertIn("active_job: ['pending', 'leased', 'running'].includes(execution.status) ? execution : null", poll_block)

    def test_final_status_after_long_event_stream_is_reachable_by_helper_contract(self) -> None:
        html = HTML.read_text(encoding="utf-8")
        helper_start = html.index("async loadAllExecutionEvents")
        helper_end = html.index("\n    async loadAllBranchTimeline", helper_start)
        helper_block = html[helper_start:helper_end]

        self.assertIn("pageIndex < 200", helper_block)
        self.assertIn("Math.min(limit || 250, 250)", helper_block)
        self.assertIn("events.push(...pageEvents)", helper_block)
        self.assertIn("next === cursor", helper_block)


if __name__ == "__main__":
    unittest.main()
