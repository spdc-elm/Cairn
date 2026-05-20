from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cairn.server import db
from cairn.server.models import CreateProjectRequest, RemoteSessionProvenance, RunProvenanceUpsert, WorkerRuntimeHealth, WorkerRuntimeHealthUpsertRequest
from cairn.server.questions.models import QuestionClaimRequest, QuestionCreateRequest
from cairn.server.routers.environments import healthcheck_environment
from cairn.server.routers.projects import create_project
from cairn.server.routers.questions import create_question, dispatcher_claim_question_job, reset_question_state_for_tests
from cairn.server.routers.runs import update_run_provenance_session, upsert_run_provenance
from cairn.server.routers.workers import list_workers, upsert_worker_health
from cairn.server.services import dumps_json, resolve_anchor


class V3WorkerRuntimeHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        reset_question_state_for_tests()
        self.project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish", environment_id="docker-default"))
        self.project_id = self.project.project.id
        self._insert_pi_worker()

    def tearDown(self) -> None:
        reset_question_state_for_tests()
        self.tmp.cleanup()

    def test_worker_health_upsert_is_returned_by_inventory(self) -> None:
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="unhealthy",
                        checked_at="2026-05-19T00:00:00Z",
                        disabled_until="2026-05-19T00:00:05Z",
                        source="startup_healthcheck",
                        detail={"stderr_preview": "connection failed"},
                    )
                ]
            )
        )

        worker = next(item for item in list_workers() if item.name == "pi-main")

        self.assertEqual(worker.runtime_health[0].environment_id, "docker-default")
        self.assertEqual(worker.runtime_health[0].status, "unhealthy")
        self.assertEqual(worker.runtime_health[0].detail["stderr_preview"], "connection failed")

    def test_unhealthy_source_disables_fork_and_resume_only(self) -> None:
        self._available_run("run_log_001")
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="unhealthy",
                        checked_at="2026-05-19T00:00:00Z",
                        disabled_until="2999-01-01T00:00:00Z",
                        source="runtime_healthcheck",
                    )
                ]
            )
        )

        with db.get_conn() as conn:
            resolved = resolve_anchor(conn, self.project_id, "run", "run_log_001")

        self.assertEqual(resolved.available_modes, ["fresh_context"])
        self.assertEqual(resolved.unavailable_reasons["fork"], "worker_environment_unhealthy")
        self.assertEqual(resolved.unavailable_reasons["resume"], "worker_environment_unhealthy")

    def test_endpoint_change_disables_old_source_session(self) -> None:
        self._available_run("run_log_001")
        with db.get_conn() as conn:
            conn.execute("UPDATE worker_inventory SET endpoint = 'pi-other' WHERE name = 'pi-main'")

        with db.get_conn() as conn:
            resolved = resolve_anchor(conn, self.project_id, "run", "run_log_001")

        self.assertEqual(resolved.available_modes, ["fresh_context"])
        self.assertEqual(resolved.unavailable_reasons["fork"], "source_worker_identity_changed")

    def test_removed_endpoint_marks_runtime_health_unknown(self) -> None:
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="ok",
                        checked_at="2026-05-19T00:00:00Z",
                        stale_after="2999-01-01T00:00:00Z",
                        source="startup_healthcheck",
                    )
                ]
            )
        )
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM environment_provider_endpoints WHERE environment_id = 'docker-default' AND endpoint_id = 'pi-default'"
            )

        worker = next(item for item in list_workers() if item.name == "pi-main")

        self.assertEqual(worker.runtime_health[0].status, "unknown")
        self.assertEqual(worker.runtime_health[0].detail["reason"], "worker_endpoint_unavailable")

    def test_environment_healthcheck_includes_runtime_worker_detail(self) -> None:
        upsert_worker_health(
            WorkerRuntimeHealthUpsertRequest(
                health=[
                    WorkerRuntimeHealth(
                        environment_id="docker-default",
                        worker_name="pi-main",
                        worker_type="pi",
                        endpoint_id="pi-default",
                        model_profile_id="pi-main",
                        status="unhealthy",
                        checked_at="2026-05-19T00:00:00Z",
                        stale_after="2999-01-01T00:00:00Z",
                        disabled_until="2999-01-01T00:00:00Z",
                        source="startup_healthcheck",
                        detail={"returncode": 1, "duration_ms": 123, "stderr_preview": "connection failed"},
                    )
                ]
            )
        )

        result = healthcheck_environment("docker-default")
        worker_check = next(check for check in result["checks"] if check["name"] == "worker:pi-main")

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(worker_check["status"], "unhealthy")
        self.assertIn("returncode=1", worker_check["stdout"])
        self.assertEqual(worker_check["stderr"], "connection failed")

    def test_question_fork_uses_source_environment_and_claim_filters_environment(self) -> None:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO work_environments (id, label, backend, workspace_root, cleanup_json, terminal_json, created_at, updated_at)
                VALUES ('other-env', 'Other', 'docker', NULL, NULL, NULL, '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')
                """
            )
            conn.execute("UPDATE projects SET environment_id = 'other-env' WHERE id = ?", (self.project_id,))
        self._available_run("run_log_001")

        thread = create_question(
            self.project_id,
            QuestionCreateRequest(anchor_type="run", anchor_id="run_log_001", mode="fork", message="why?"),
        )
        wrong_env = dispatcher_claim_question_job(
            QuestionClaimRequest(dispatcher_id="disp", worker_names=["pi-main"], environment_ids=["other-env"])
        )
        right_env = dispatcher_claim_question_job(
            QuestionClaimRequest(dispatcher_id="disp", worker_names=["pi-main"], environment_ids=["docker-default"])
        )

        self.assertEqual(thread.execution_environment_id, "docker-default")
        self.assertIsNone(wrong_env.job)
        self.assertIsNotNone(right_env.job)
        assert right_env.job is not None
        self.assertEqual(right_env.job.execution_environment_id, "docker-default")

    def _insert_pi_worker(self) -> None:
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
                INSERT INTO environment_provider_endpoints (
                    environment_id, endpoint_id, type, base_url, provider_api, api_key, created_at, updated_at
                ) VALUES ('docker-default', 'pi-default', 'pi', 'http://example.invalid', 'openai', 'secret', '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO worker_inventory (
                    name, type, model_profile, endpoint, task_types_json, max_running, priority,
                    question_capability_json, capability_updated_at, capability_source, updated_at
                ) VALUES ('pi-main', 'pi', 'pi-main', 'pi-default', ?, 1, 0, ?, ?, 'static', ?)
                """,
                (dumps_json(["explore"]), dumps_json(capability), "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
            )

    def _available_run(self, run_log_id: str) -> None:
        upsert_run_provenance(
            self.project_id,
            RunProvenanceUpsert(
                run_log_id=run_log_id,
                task_type="explore",
                phase="explore_execute",
                worker_name="pi-main",
                worker_type="pi",
                environment_id="docker-default",
                model_profile_id="pi-main",
                endpoint_id="pi-default",
                started_at="2026-05-19T00:00:00Z",
            ),
        )
        update_run_provenance_session(
            self.project_id,
            run_log_id,
            RemoteSessionProvenance(id="pi-session-1", kind="pi_session", status="available", capture_method="stdout_event"),
        )


if __name__ == "__main__":
    unittest.main()
