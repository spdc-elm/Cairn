from __future__ import annotations

from pathlib import Path
import unittest


class EnvironmentHealthcheckUiGuardTests(unittest.TestCase):
    def test_ui_uses_healthcheck_request_api_and_does_not_treat_delegated_as_final(self) -> None:
        html = Path("cairn/src/cairn/server/static/index.html").read_text(encoding="utf-8")
        start = html.index("async runEnvironmentHealthcheck(env)")
        end = html.index("rememberProjectListScroll()", start)
        block = html[start:end]

        self.assertIn("/healthcheck-requests", block)
        self.assertNotIn("/healthcheck`", block)
        self.assertIn("pollEnvironmentHealthcheck", block)
        self.assertNotIn("delegated", block)


class V32ConversationUiGuardTests(unittest.TestCase):
    def test_output_polling_and_question_history_use_execution_events(self) -> None:
        html = Path("cairn/src/cairn/server/static/index.html").read_text(encoding="utf-8")
        self.assertNotIn("/runs/", html)
        self.assertNotIn("/questions", html)

        conversation_start = html.index("async loadSelectedConversation")
        conversation_end = html.index("\n    executionTranscript", conversation_start)
        conversation_block = html[conversation_start:conversation_end]
        self.assertIn("scheduleConversationPoll", conversation_block)
        self.assertIn("questionAvailabilityForExecution", conversation_block)
        self.assertIn("loadConversationBranchHistory", conversation_block)
        self.assertIn("forceConversationRefresh", html)
        self.assertIn("this.loadSelectedConversation(forceConversationRefresh)", html)
        self.assertIn("workerJsonLinesToConversationEvents", html)
        self.assertIn("type === 'system'", html)
        self.assertIn("type === 'assistant' || type === 'user'", html)
        self.assertIn("type === 'result'", html)
        self.assertIn("type === 'stream_event'", html)
        self.assertIn("claudeStreamEventToConversationEvents", html)
        self.assertIn("delta.type === 'text_delta'", html)
        self.assertIn("stream_delta", html)
        self.assertIn(":run:started", html)
        self.assertIn("running ? 'started' : 'finished'", html)
        self.assertIn("terminalStatusByExecution", html)
        self.assertIn("event.kind === 'run_started' ? terminalStatus : event.text", html)
        self.assertIn("message_update", html)
        self.assertIn("message_end", html)
        self.assertIn("workerMessageToConversationEvents", html)
        self.assertIn("agent_end", html)
        self.assertIn("itemType === 'toolCall' || itemType === 'tool_call' || itemType === 'tool_use'", html)
        self.assertIn("itemType === 'tool_result'", html)

        events_start = html.index("\n    conversationEvents()")
        events_end = html.index("conversationEventLabel", events_start)
        events_block = html[events_start:events_end]
        self.assertIn("this.question.activeThread?.timeline", events_block)
        self.assertIn("flatMap(event => this.executionEventToConversationEvents(event))", events_block)
        self.assertIn("activeBranch?.mode === 'resume'", events_block)
        self.assertNotIn("activeBranch?.mode === 'fork'", events_block)
        self.assertIn("compactConversationEvents", events_block)
        self.assertIn("'raw'", events_block)

        branch_history_start = html.index("async loadConversationBranchHistory")
        branch_history_end = html.index("\n    executionIdForConversationAnchor", branch_history_start)
        branch_history_block = html[branch_history_start:branch_history_end]
        self.assertIn("filter(branch => branch.mode === 'resume')", branch_history_block)

        question_messages_start = html.index("\n\t    questionMessages()")
        question_messages_end = html.index("questionMessageText", question_messages_start)
        question_messages_block = html[question_messages_start:question_messages_end]
        self.assertIn("branch?.mode === 'resume'", question_messages_block)
        self.assertIn("compactConversationEvents", question_messages_block)
        self.assertIn("this.question.activeThread?.timeline", question_messages_block)

        ask_start = html.index("async askSelectedConversation")
        ask_end = html.index("async pollQuestionThread", ask_start)
        ask_block = html[ask_start:ask_end]
        self.assertIn("intent?.active_execution_id", html)
        self.assertIn("intent?.latest_execution_id", html)
        self.assertIn("applyDefaultQuestionMode", html)
        self.assertIn("availableModes.includes('fork') ? 'fork' : 'fresh_context'", html)
        self.assertIn("activeThread?.branch", ask_block)
        self.assertIn("activeThread?.branch?.mode === mode", ask_block)
        self.assertIn("if (!branch)", ask_block)
        self.assertIn("/messages", ask_block)

    def test_output_tabs_and_question_thread_have_scroll_guards(self) -> None:
        html = Path("cairn/src/cairn/server/static/index.html").read_text(encoding="utf-8")

        self.assertIn('x-ref="conversationScroll"', html)
        self.assertIn("sticky top-0 z-10 flex rounded-lg", html)
        self.assertIn('x-ref="questionThreadScroll"', html)
        self.assertIn("max-h-80 overflow-y-auto", html)
        self.assertIn("shouldStickConversationScroll", html)
        self.assertIn("scrollQuestionThreadToBottom", html)
        self.assertIn("projectWorkspaceHint()", html)


if __name__ == "__main__":
    unittest.main()
