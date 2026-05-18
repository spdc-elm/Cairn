from __future__ import annotations

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
