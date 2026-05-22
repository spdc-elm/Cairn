from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cairn.dispatcher.runtime.agent_context import AgentContextMaterializationError, materialize_agent_context
from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.base import WorkerRuntimeContext
from cairn.dispatcher.config import WorkerConfig


class FakeEnvironment:
    id = "fake"
    label = "fake"
    backend = "fake"

    def __init__(self, root: Path):
        self.root = root

    def _local(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def write_text_file(self, handle: EnvironmentHandle, path: str, content: str) -> None:
        target = self._local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read_text_file(self, handle: EnvironmentHandle, path: str) -> str:
        return self._local(path).read_text(encoding="utf-8")

    def delete_file(self, handle: EnvironmentHandle, path: str) -> None:
        self._local(path).unlink(missing_ok=True)

    def exists(self, handle: EnvironmentHandle, path: str) -> bool:
        return self._local(path).is_file()

    def is_path_in_workspace(self, handle: EnvironmentHandle, path: str) -> bool:
        return path.startswith(handle.workspace.rstrip("/") + "/") or path == handle.workspace


class AgentContextRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = FakeEnvironment(Path(self.tmp.name))
        self.handle = EnvironmentHandle(project_id="proj_001", target_name="fake", workspace="/workspace")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_materialize_writes_managed_agents_and_manifest(self) -> None:
        result = materialize_agent_context(
            self.env,
            self.handle,
            {
                "enabled": True,
                "kind": "agents_md",
                "content": "hello AGENTS",
                "content_hash": "ignored",
                "source_template_id": "tpl_001",
                "source_template_hash": "hash",
            },
        )

        self.assertEqual(result.status, "materialized")
        self.assertEqual(self.env.read_text_file(self.handle, "/workspace/AGENTS.md"), "hello AGENTS")
        manifest = json.loads(self.env.read_text_file(self.handle, "/workspace/.cairn/agent-context/agents_md.manifest.json"))
        self.assertEqual(manifest["last_materialized_hash"], result.content_hash)

    def test_materialize_refuses_unmanaged_conflict_and_cleans_disabled_managed_file(self) -> None:
        materialize_agent_context(self.env, self.handle, {"enabled": True, "kind": "agents_md", "content": "old"})
        materialize_agent_context(self.env, self.handle, {"enabled": True, "kind": "agents_md", "content": "new"})
        self.assertEqual(self.env.read_text_file(self.handle, "/workspace/AGENTS.md"), "new")

        self.env.write_text_file(self.handle, "/workspace/AGENTS.md", "manual edit")
        with self.assertRaises(AgentContextMaterializationError):
            materialize_agent_context(self.env, self.handle, {"enabled": True, "kind": "agents_md", "content": "next"})

        self.env.write_text_file(self.handle, "/workspace/AGENTS.md", "new")
        result = materialize_agent_context(self.env, self.handle, {"enabled": False, "kind": "agents_md", "content": "new"})
        self.assertEqual(result.status, "disabled")
        self.assertFalse(self.env.exists(self.handle, "/workspace/AGENTS.md"))

    def test_pi_context_flag_depends_on_runtime_context_but_healthcheck_stays_isolated(self) -> None:
        worker = WorkerConfig(
            name="pi-test",
            type="pi",
            task_types=["explore"],
            max_running=1,
            priority=1,
            env={"PI_MODEL": "gpt-5.4", "PI_BASE_URL": "http://x", "PI_PROVIDER_API": "openai", "PI_API_KEY": "sk"},
        )
        driver = PiDriver()

        disabled = driver.build_execute(worker, "prompt", None).argv
        enabled = driver.build_execute(
            worker,
            "prompt",
            None,
            runtime_context=WorkerRuntimeContext(agent_context_enabled=True, agent_context_kind="agents_md"),
        ).argv
        healthcheck = driver.build_healthcheck(worker)

        self.assertIn("--no-context-files", disabled)
        self.assertNotIn("--no-context-files", enabled)
        self.assertIn("--no-context-files", healthcheck)


if __name__ == "__main__":
    unittest.main()
