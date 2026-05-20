from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi import HTTPException

from cairn.server import db
from cairn.server.models import ProviderEndpointUpsert, WorkEnvironmentUpsert
from cairn.server.routers.environments import (
    create_environment,
    create_environment_endpoint,
    delete_environment,
    get_environment_endpoint,
    healthcheck_environment,
    list_environments,
    update_environment_endpoint,
)


class EnvironmentEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        create_environment(
            WorkEnvironmentUpsert(
                id="pentestvm",
                label="pentestVM",
                backend="ssh",
                ssh_command="ssh host",
                workspace_root="/home/kali/cairn-workspaces",
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_endpoint_secret_is_stored_but_redacted_by_default(self) -> None:
        endpoint = create_environment_endpoint(
            "pentestvm",
            ProviderEndpointUpsert(
                id="pi-default",
                type="pi",
                base_url="https://pi.example.test/v1",
                provider_api="openai-completions",
                api_key="sk-secret-test",
            ),
        )

        self.assertTrue(endpoint.has_api_key)
        self.assertNotIn("api_key", endpoint.model_dump())
        environments = list_environments()
        env = next(item for item in environments if item.id == "pentestvm")
        self.assertEqual(env.provider_endpoints[0].id, "pi-default")
        self.assertNotIn("sk-secret-test", env.model_dump_json())

        secret = get_environment_endpoint("pentestvm", "pi-default", include_secret=True)
        self.assertEqual(secret.api_key, "sk-secret-test")

    def test_endpoint_update_without_key_keeps_existing_key(self) -> None:
        create_environment_endpoint(
            "pentestvm",
            ProviderEndpointUpsert(
                id="pi-default",
                type="pi",
                base_url="https://old.example.test/v1",
                provider_api="openai-completions",
                api_key="sk-old",
            ),
        )

        update_environment_endpoint(
            "pentestvm",
            "pi-default",
            ProviderEndpointUpsert(
                id="pi-default",
                type="pi",
                base_url="https://new.example.test/v1",
                provider_api="openai-completions",
            ),
        )

        secret = get_environment_endpoint("pentestvm", "pi-default", include_secret=True)
        self.assertEqual(secret.base_url, "https://new.example.test/v1")
        self.assertEqual(secret.api_key, "sk-old")

    def test_clear_api_key_removes_existing_key(self) -> None:
        create_environment_endpoint(
            "pentestvm",
            ProviderEndpointUpsert(
                id="pi-default",
                type="pi",
                base_url="https://pi.example.test/v1",
                provider_api="openai-completions",
                api_key="sk-old",
            ),
        )

        endpoint = update_environment_endpoint(
            "pentestvm",
            "pi-default",
            ProviderEndpointUpsert(
                id="pi-default",
                type="pi",
                base_url="https://pi.example.test/v1",
                provider_api="openai-completions",
                clear_api_key=True,
            ),
        )

        self.assertFalse(endpoint.has_api_key)
        secret = get_environment_endpoint("pentestvm", "pi-default", include_secret=True)
        self.assertIsNone(secret.api_key)

    def test_delete_environment_cascades_endpoints(self) -> None:
        create_environment_endpoint(
            "pentestvm",
            ProviderEndpointUpsert(
                id="pi-default",
                type="pi",
                base_url="https://pi.example.test/v1",
                provider_api="openai-completions",
                api_key="sk-old",
            ),
        )

        delete_environment("pentestvm")

        with self.assertRaises(HTTPException) as ctx:
            get_environment_endpoint("pentestvm", "pi-default", include_secret=True)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_new_schema_does_not_create_harness_column(self) -> None:
        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(work_environments)").fetchall()}

        self.assertNotIn("harness", columns)

    def test_legacy_harness_input_is_accepted_but_not_returned(self) -> None:
        env = create_environment(
            WorkEnvironmentUpsert(
                id="legacy-input",
                label="Legacy Input",
                backend="ssh",
                ssh_command="ssh legacy",
                workspace_root="/home/kali/cairn-workspaces",
                harness="pi",
            )
        )

        self.assertNotIn("harness", env.model_dump())

    def test_environment_healthcheck_derives_worker_types_from_endpoints(self) -> None:
        create_environment_endpoint(
            "pentestvm",
            ProviderEndpointUpsert(
                id="codex-default",
                type="codex",
                base_url="https://codex.example.test/v1",
                api_key="sk-codex",
            ),
        )
        create_environment_endpoint(
            "pentestvm",
            ProviderEndpointUpsert(
                id="claude-default",
                type="claudecode",
                base_url="https://claude.example.test",
                api_key="sk-claude",
            ),
        )
        result = healthcheck_environment("pentestvm")

        self.assertEqual(result["status"], "delegated")
        self.assertEqual(result["checks"][0]["status"], "delegated")
        self.assertIn("dispatcher execution plane", result["checks"][0]["stderr"])

    def test_existing_legacy_harness_column_is_tolerated(self) -> None:
        legacy_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(legacy_tmp.cleanup)
        legacy_path = Path(legacy_tmp.name) / "legacy.db"
        with sqlite3.connect(str(legacy_path)) as conn:
            conn.execute(
                """
                CREATE TABLE work_environments (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    ssh_command TEXT,
                    workspace_root TEXT,
                    harness TEXT NOT NULL DEFAULT 'pi',
                    cleanup_json TEXT,
                    terminal_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_health_status TEXT,
                    last_healthcheck_json TEXT
                )
                """
            )
        db._db_path = None
        db.configure(legacy_path)

        env = create_environment(
            WorkEnvironmentUpsert(
                id="legacy-db",
                label="Legacy DB",
                backend="ssh",
                ssh_command="ssh legacy-db",
                workspace_root="/home/kali/cairn-workspaces",
            )
        )
        environments = list_environments()

        self.assertEqual(env.id, "legacy-db")
        self.assertNotIn("harness", env.model_dump())
        self.assertTrue(any(item.id == "legacy-db" for item in environments))


if __name__ == "__main__":
    unittest.main()
