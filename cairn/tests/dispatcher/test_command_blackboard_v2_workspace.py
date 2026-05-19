from __future__ import annotations

import unittest

from cairn.dispatcher.runtime.environments.base import EnvironmentHandle
from cairn.dispatcher.runtime.environments.docker import DockerEnvironment
from cairn.dispatcher.runtime.environments.ssh import SshEnvironment


class FakeManager:
    def __init__(self) -> None:
        self.mkdir_calls: list[tuple[str, str]] = []
        self.writes: dict[tuple[str, str], str] = {}

    def ensure_running(self, project_id: str) -> str:
        return f"container-{project_id}"

    def exec_mkdir(self, name: str, path: str) -> None:
        self.mkdir_calls.append((name, path))

    def write_text_file(self, name: str, path: str, content: str) -> None:
        self.writes[(name, path)] = content

    def file_exists(self, name: str, path: str) -> bool:
        return (name, path) in self.writes

    def read_text_file(self, name: str, path: str) -> str:
        return self.writes[(name, path)]


class CommandBlackboardV2WorkspaceTests(unittest.TestCase):
    def test_docker_prepare_project_returns_project_workspace(self) -> None:
        env = DockerEnvironment.__new__(DockerEnvironment)
        env.id = "docker-default"
        env.label = "Docker"
        env._manager = FakeManager()
        env._startup_handles = set()

        handle = env.prepare_project("proj_001")

        self.assertEqual(handle.workspace, "/home/kali/workspace/.cairn/projects/proj_001")
        self.assertEqual(env._manager.mkdir_calls, [("container-proj_001", handle.workspace)])

    def test_docker_build_process_injects_workspace(self) -> None:
        class ProcessManager(FakeManager):
            def build_exec_process(self, name, env, command, timeout_seconds=None, kill_after_seconds=5, run_logger=None):
                self.last_env = env
                return object()

        env = DockerEnvironment.__new__(DockerEnvironment)
        env.id = "docker-default"
        env.label = "Docker"
        env._manager = ProcessManager()
        handle = EnvironmentHandle(project_id="proj_001", target_name="container-proj_001", workspace="/home/kali/workspace/.cairn/projects/proj_001")

        env.build_process(handle, {}, ["true"])

        self.assertEqual(env._manager.last_env["CAIRN_WORKSPACE"], handle.workspace)

    def test_paths_must_be_inside_workspace(self) -> None:
        env = DockerEnvironment.__new__(DockerEnvironment)
        handle = EnvironmentHandle(project_id="proj_001", target_name="c", workspace="/home/kali/workspace/.cairn/projects/proj_001")

        self.assertTrue(env.is_path_in_workspace(handle, "/home/kali/workspace/.cairn/projects/proj_001/.cairn/reports/r.md"))
        self.assertFalse(env.is_path_in_workspace(handle, "/home/kali/workspace/.cairn/projects/proj_0012/r.md"))
        self.assertFalse(env.is_path_in_workspace(handle, "/home/kali/workspace/.cairn/projects/proj_001/../other/r.md"))

    def test_ssh_graph_snapshot_path_is_under_workspace(self) -> None:
        env = SshEnvironment.__new__(SshEnvironment)
        handle = EnvironmentHandle(project_id="proj_001", target_name="ssh", workspace="/tmp/cairn/proj_001")

        self.assertEqual(env.graph_snapshot_path(handle, "explore"), "/tmp/cairn/proj_001/.cairn/prompts/explore")


if __name__ == "__main__":
    unittest.main()
