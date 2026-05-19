from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import (
    ConcludeRequest,
    CreateIntentRequest,
    CreateProjectRequest,
    HeartbeatRequest,
    RequestConcludeRequest,
    UpdateProjectRequest,
    UpdateIntentRequest,
)
from cairn.server.routers.export import _export_yaml
from cairn.server.routers.intents import conclude, create_intent, delete_open_intent, heartbeat, request_conclude, update_intent
from cairn.server.routers.projects import create_project, update_project


class CommandBlackboardV2ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_project_defaults_auto_reason_false(self) -> None:
        self.assertFalse(self.project.project.auto_reason)

    def test_create_project_saves_auto_scope_and_timeouts(self) -> None:
        project = create_project(
            CreateProjectRequest(
                title="manual",
                origin="o",
                goal="g",
                auto_reason=True,
                allowed_auto_workers=["pi-GPT5.5"],
                default_timeout_seconds=600,
                default_conclude_timeout_seconds=60,
            )
        )

        self.assertTrue(project.project.auto_reason)
        self.assertEqual(project.project.allowed_auto_workers, ["pi-GPT5.5"])
        self.assertEqual(project.project.default_timeout_seconds, 600)
        self.assertEqual(project.project.default_conclude_timeout_seconds, 60)

    def test_update_project_scheduling_settings(self) -> None:
        updated = update_project(
            self.project.project.id,
            UpdateProjectRequest(
                auto_reason=True,
                allowed_auto_workers=["pi-GPT5.4"],
                default_timeout_seconds=420,
                default_conclude_timeout_seconds=45,
            ),
        )

        self.assertTrue(updated.auto_reason)
        self.assertEqual(updated.allowed_auto_workers, ["pi-GPT5.4"])
        self.assertEqual(updated.default_timeout_seconds, 420)
        self.assertEqual(updated.default_conclude_timeout_seconds, 45)

        cleared = update_project(
            self.project.project.id,
            UpdateProjectRequest(auto_reason=False, allowed_auto_workers=None),
        )
        self.assertFalse(cleared.auto_reason)
        self.assertIsNone(cleared.allowed_auto_workers)

    def test_create_intent_requested_worker_does_not_claim_worker(self) -> None:
        intent = create_intent(
            self.project.project.id,
            CreateIntentRequest(**{"from": ["origin"], "description": "explore", "creator": "human", "requested_worker": "pi-GPT5.5"}),
        )

        self.assertEqual(intent.requested_worker, "pi-GPT5.5")
        self.assertIsNone(intent.worker)

    def test_legacy_create_intent_with_worker_still_claims(self) -> None:
        intent = create_intent(
            self.project.project.id,
            CreateIntentRequest(**{"from": ["origin"], "description": "explore", "creator": "human", "worker": "human"}),
        )

        self.assertEqual(intent.worker, "human")
        self.assertIsNotNone(intent.last_heartbeat_at)

    def test_patch_pending_and_reject_running_scheduling_change(self) -> None:
        intent = create_intent(
            self.project.project.id,
            CreateIntentRequest(**{"from": ["origin"], "description": "old", "creator": "human"}),
        )
        patched = update_intent(
            self.project.project.id,
            intent.id,
            UpdateIntentRequest(description="new", requested_worker="pi-GPT5.5", timeout_override_seconds=300),
        )
        self.assertEqual(patched.description, "new")
        self.assertEqual(patched.requested_worker, "pi-GPT5.5")
        self.assertEqual(patched.timeout_override_seconds, 300)

        heartbeat(self.project.project.id, intent.id, HeartbeatRequest(worker="pi-GPT5.5"))
        with self.assertRaises(HTTPException) as ctx:
            update_intent(self.project.project.id, intent.id, UpdateIntentRequest(requested_worker="other"))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_delete_running_rejected(self) -> None:
        intent = create_intent(
            self.project.project.id,
            CreateIntentRequest(**{"from": ["origin"], "description": "run", "creator": "human"}),
        )
        heartbeat(self.project.project.id, intent.id, HeartbeatRequest(worker="pi"))

        with self.assertRaises(HTTPException) as ctx:
            delete_open_intent(self.project.project.id, intent.id)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_request_conclude_and_fact_metadata_export(self) -> None:
        intent = create_intent(
            self.project.project.id,
            CreateIntentRequest(**{"from": ["origin"], "description": "run", "creator": "human"}),
        )
        control = request_conclude(
            self.project.project.id,
            intent.id,
            RequestConcludeRequest(actor="human", reason="enough"),
        )
        self.assertEqual(control.control_state, "conclude_requested")
        self.assertEqual(control.control_requested_by, "human")

        result = conclude(
            self.project.project.id,
            intent.id,
            ConcludeRequest(worker="pi", description="found fact", metadata={"report_path": "/tmp/report.md"}),
        )
        self.assertEqual(result.fact.metadata["report_path"], "/tmp/report.md")
        with db.get_conn() as conn:
            exported = _export_yaml(conn, self.project.project.id)
        self.assertIn("requested_worker", exported)
        self.assertIn("report_path", exported)


if __name__ == "__main__":
    unittest.main()
