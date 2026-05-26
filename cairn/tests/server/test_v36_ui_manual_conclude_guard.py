from __future__ import annotations

from pathlib import Path
import unittest


HTML = Path("cairn/src/cairn/server/static/index.html").read_text(encoding="utf-8")


class V36UiManualConcludeGuardTests(unittest.TestCase):
    def test_request_conclude_uses_active_execution_projection_not_stale_worker(self) -> None:
        self.assertIn("selectedAutoConcludableOpenIntentRecord", HTML)
        start = HTML.index("\n    selectedAutoConcludableOpenIntentRecord() {")
        end = HTML.index("\n    selectedManualConcludableOpenIntentRecord", start)
        block = HTML[start:end]

        self.assertNotIn("intent.worker", block)
        helper_start = HTML.index("\n    isIntentActive(intent) {")
        helper_end = HTML.index("\n    intentActiveWorkerName", helper_start)
        helper = HTML[helper_start:helper_end]
        self.assertIn("intent?.active_execution_id", helper)
        self.assertIn("['pending', 'leased', 'running'].includes(intent.runtime_status)", helper)

    def test_manual_conclude_is_available_for_any_open_active_project_intent(self) -> None:
        self.assertIn("selectedManualConcludableOpenIntentRecord", HTML)
        start = HTML.index("\n    selectedManualConcludableOpenIntentRecord() {")
        end = HTML.index("\n    selectedReleasableOpenIntentRecord", start)
        block = HTML[start:end]

        self.assertIn("selectedOpenIntentRecord", block)
        self.assertIn("projectIsActive", block)
        self.assertNotIn("intent.worker", block)
        self.assertIn("Manual Conclude", HTML)

    def test_manual_modal_uses_prompt_and_json_import_api(self) -> None:
        self.assertIn("manualConcludeForm", HTML)
        self.assertIn("/manual-conclude-prompt", HTML)
        self.assertIn("/manual-conclude", HTML)
        self.assertIn("manualConcludePreview", HTML)
        self.assertIn("copyManualConcludePrompt", HTML)

    def test_human_actor_cannot_worker_claim_intent(self) -> None:
        self.assertIn("actorIsWorker", HTML)
        start = HTML.index("\n    selectedActionableOpenIntentRecord() {")
        end = HTML.index("\n    selectedAutoConcludableOpenIntentRecord", start)
        block = HTML[start:end]

        self.assertIn("!this.actorIsWorker()", block)
        self.assertIn("return null", block)


if __name__ == "__main__":
    unittest.main()
