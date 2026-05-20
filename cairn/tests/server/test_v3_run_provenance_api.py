from __future__ import annotations

from pathlib import Path
import os
import shutil
import tempfile
import unittest

from cairn.server import db
from cairn.server.models import CreateProjectRequest, RemoteSessionProvenance, RunProvenanceUpsert
from cairn.server.routers.projects import create_project
from cairn.server.routers.runs import (
    get_project_run_transcript,
    resolve_project_anchor,
    update_run_provenance_session,
    upsert_run_provenance,
)
from cairn.server.services import dumps_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "run_logs"


class V3RunProvenanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.run_tmp = tempfile.TemporaryDirectory()
        self._old_run_dir = os.environ.get("CAIRN_RUN_LOG_DIR")
        os.environ["CAIRN_RUN_LOG_DIR"] = self.run_tmp.name
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))

    def tearDown(self) -> None:
        if self._old_run_dir is None:
            os.environ.pop("CAIRN_RUN_LOG_DIR", None)
        else:
            os.environ["CAIRN_RUN_LOG_DIR"] = self._old_run_dir
        self.run_tmp.cleanup()
        self.tmp.cleanup()

    def test_provenance_endpoint_records_session_and_resolves_run_anchor(self) -> None:
        provenance = upsert_run_provenance(
            self.project.project.id,
            RunProvenanceUpsert(
                run_log_id="run_log_001",
                intent_id="i001",
                task_type="explore",
                phase="explore_execute",
                worker_name="pi-main",
                worker_type="pi",
                started_at="2026-05-19T00:00:00Z",
            ),
        )
        self.assertEqual(provenance.remote_session.status, "unresolved")

        updated = update_run_provenance_session(
            self.project.project.id,
            "run_log_001",
            RemoteSessionProvenance(
                id="pi-session-1",
                kind="pi_session",
                status="available",
                capture_method="stdout_event",
            ),
        )
        resolved = resolve_project_anchor(self.project.project.id, "run", "run_log_001")

        self.assertEqual(updated.remote_session.id, "pi-session-1")
        self.assertEqual(resolved.status, "exact")
        self.assertEqual(resolved.available_modes, ["fresh_context"])

    def test_resolved_anchor_modes_include_fork_from_worker_inventory(self) -> None:
        capability = {
            "can_resume_session": True,
            "can_fork_session": True,
            "can_use_tools": True,
            "can_stream_events": True,
            "resume_mutates_source": True,
            "fork_creates_remote_log": True,
            "question_modes": ["fork", "resume", "fresh_context"],
            "unavailable_reasons": {},
        }
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO worker_inventory (
                    name, type, task_types_json, max_running, priority,
                    question_capability_json, capability_updated_at, capability_source, updated_at
                ) VALUES ('pi-main', 'pi', ?, 1, 0, ?, ?, 'static', ?)
                """,
                (dumps_json(["explore"]), dumps_json(capability), "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
            )
        upsert_run_provenance(
            self.project.project.id,
            RunProvenanceUpsert(
                run_log_id="run_log_002",
                intent_id="i001",
                task_type="explore",
                phase="explore_execute",
                worker_name="pi-main",
                worker_type="pi",
                started_at="2026-05-19T00:00:00Z",
            ),
        )
        update_run_provenance_session(
            self.project.project.id,
            "run_log_002",
            RemoteSessionProvenance(
                id="pi-session-2",
                kind="pi_session",
                status="available",
                capture_method="stdout_event",
            ),
        )

        resolved = resolve_project_anchor(self.project.project.id, "run", "run_log_002")

        self.assertEqual(resolved.available_modes, ["fork", "resume", "fresh_context"])

    def test_transcript_endpoint_reads_full_run_log_and_uses_provenance_parser(self) -> None:
        project_dir = Path(self.run_tmp.name) / self.project.project.id
        project_dir.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "pi_large_stream.jsonl", project_dir / "run_pi_fixture.jsonl")
        upsert_run_provenance(
            self.project.project.id,
            RunProvenanceUpsert(
                run_log_id="run_pi_fixture",
                task_type="explore",
                phase="explore_execute",
                worker_name="pi-main",
                worker_type="pi",
                started_at="2026-05-19T00:00:00Z",
            ),
        )

        transcript = get_project_run_transcript(self.project.project.id, "run_pi_fixture", limit_events=20)

        self.assertEqual(transcript.parser, "pi")
        self.assertEqual(transcript.provenance.worker_type, "pi")
        self.assertTrue(any(event.kind == "message" and event.text == "hello world" for event in transcript.events))


if __name__ == "__main__":
    unittest.main()
