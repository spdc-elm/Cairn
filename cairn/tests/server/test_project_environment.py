from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server import db
from cairn.server.models import CreateProjectRequest, WorkEnvironmentUpsert
from cairn.server.routers.environments import create_environment, list_environments
from cairn.server.routers.projects import create_project


class ProjectEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_environment_exists(self) -> None:
        environments = list_environments()

        self.assertEqual(environments[0].id, "docker-default")
        self.assertEqual(environments[0].backend, "docker")

    def test_environment_rejects_worker_secret_fields(self) -> None:
        with self.assertRaises(ValidationError):
            WorkEnvironmentUpsert(
                    label="pentestVM",
                    backend="ssh",
                    ssh_command="ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm",
                    workspace_root="/home/kali/cairn-workspaces",
                    pi_api_key="sk-test",
            )

    def test_create_project_binds_environment_without_snapshot(self) -> None:
        environment = create_environment(
            WorkEnvironmentUpsert(
                id="pentestvm",
                label="pentestVM",
                backend="ssh",
                ssh_command="ssh host",
                workspace_root="/home/kali/cairn-workspaces",
            )
        )
        project = create_project(
            CreateProjectRequest(
                title="ssh env smoke",
                origin="start",
                goal="finish",
                environment_id=environment.id,
            )
        )

        self.assertEqual(project.project.environment_id, "pentestvm")
        self.assertIsNotNone(project.project.environment)
        self.assertEqual(project.project.environment.backend, "ssh")
        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
            self.assertNotIn("environment_snapshot_json", columns)

    def test_create_project_rejects_unknown_environment(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            create_project(
                CreateProjectRequest(
                    title="bad env",
                    origin="start",
                    goal="finish",
                    environment_id="missing",
                )
            )

        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
