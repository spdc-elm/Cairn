from __future__ import annotations

import json
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

import requests

from cairn.server import db
from cairn.server.models import AppendExecutionEventsRequest, CreateExecutionRequest, CreateProjectRequest, ExecutionEventAppend, FinishExecutionPatch, FinishExecutionRequest, LeaseExecutionRequest
from cairn.server.routers.executions import create_project_execution, dispatcher_append_execution_events, dispatcher_finish_execution, dispatcher_lease_pending_execution, get_project_execution_events
from cairn.server.routers.projects import create_project


ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = ROOT / "cairn" / "tests" / "e2e"
SSH_CONFIG = E2E_DIR / "pentestvm_v32_ssh_config"
E2E_DB = E2E_DIR / "cairn.e2e.db"
E2E_API_KEY = "sk-JJ51WM5mGQLNu3qBMqOULfFRZpMeMW4a4ZDO5Ep6rubmJopS"


class V34StreamReliabilitySmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db._db_path = None
        db.configure(Path(self.tmp.name) / "cairn.db")
        project = create_project(CreateProjectRequest(title="case", origin="start", goal="finish"))
        self.project_id = project.project.id

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mock_long_stream_final_is_reachable_after_cursor_pagination(self) -> None:
        execution = create_project_execution(self.project_id, CreateExecutionRequest(task_type="explore", phase="bootstrap"))
        leased = dispatcher_lease_pending_execution(
            LeaseExecutionRequest(execution_id=execution.id, dispatcher_id="disp", worker_name="mock", worker_type="mock")
        )
        events = [
            ExecutionEventAppend(event_type="stdout", payload={"text": f"chunk {index}\n"}, event_key=f"chunk-{index}", ts=f"t{index:03d}")
            for index in range(300)
        ]
        for offset in range(0, len(events), 100):
            dispatcher_append_execution_events(leased.id, AppendExecutionEventsRequest(dispatcher_id="disp", events=events[offset : offset + 100]))

        dispatcher_finish_execution(
            leased.id,
            FinishExecutionRequest(
                dispatcher_id="disp",
                events=[
                    ExecutionEventAppend(event_type="message", role="assistant", payload={"text": "complete final"}, event_key="assistant-final", ts="t301"),
                    ExecutionEventAppend(event_type="status", payload={"status": "succeeded"}, event_key="terminal", ts="t302"),
                ],
                patch=FinishExecutionPatch(status="succeeded", returncode=0),
            ),
        )

        seen = []
        cursor = None
        for _ in range(5):
            page = get_project_execution_events(self.project_id, leased.id, after_cursor=cursor, limit=100)
            seen.extend(page.events)
            cursor = page.next_cursor
            if not page.events:
                break

        self.assertGreaterEqual(len(seen), 302)
        self.assertEqual(seen[-2].payload["text"], "complete final")
        self.assertEqual(seen[-1].payload["status"], "succeeded")

    @unittest.skipUnless(os.environ.get("CAIRN_E2E_SSH_PI") == "1", "real SSH/Pi sign-off requires CAIRN_E2E_SSH_PI=1")
    def test_real_ssh_pi_signoff_gate_is_explicit(self) -> None:
        db_path = Path(os.environ.get("CAIRN_E2E_DB", str(E2E_DB)))
        ssh_config = Path(os.environ.get("CAIRN_E2E_SSH_CONFIG", str(SSH_CONFIG)))
        api_key = os.environ.get("CAIRN_E2E_API_KEY", E2E_API_KEY)
        port = int(os.environ.get("CAIRN_E2E_SERVER_PORT", "0")) or _free_port()
        server_url = f"http://127.0.0.1:{port}"
        dispatch_config = Path(self.tmp.name) / "dispatch.v34.pi.yaml"
        _assert_real_pi_available(ssh_config)
        dispatch_config.write_text(
            _dispatch_yaml(
                server_url=server_url,
                ssh_config=ssh_config,
                base_url=os.environ.get("CAIRN_E2E_BASE_URL", "http://host.docker.internal:3000"),
                provider_api=os.environ.get("CAIRN_E2E_PROVIDER_API", "openai-completions"),
                api_key=api_key,
                model=os.environ.get("CAIRN_E2E_MODEL", "gpt-5.4"),
            ),
            encoding="utf-8",
        )

        server = _start(
            [
                sys.executable,
                "-c",
                "from cairn.cli import main; main()",
                "serve",
                "--db-path",
                str(db_path),
                "--port",
                str(port),
                "--no-access-log",
            ]
        )
        dispatcher: subprocess.Popen[str] | None = None
        try:
            _wait_http(f"{server_url}/settings", timeout=30)
            _upsert_environment(server_url, ssh_config, api_key)
            dispatcher = _start(
                [
                    sys.executable,
                    "-c",
                    "from cairn.cli import main; main()",
                    "dispatch",
                    "--config",
                    str(dispatch_config),
                    "--log-level",
                    "INFO",
                ]
            )
            project = _post(
                server_url,
                "/projects",
                {
                    "title": f"v3.4 real ssh pi {int(time.time())}",
                    "origin": "Real SSH/Pi v3.4 stream reliability sign-off.",
                    "goal": "Verify long stream, finish barrier, cursor pagination, fork, and branch continue.",
                    "environment_id": "pentestvm-v32",
                    "auto_reason": False,
                },
            )["project"]
            project_id = project["id"]
            _post(
                server_url,
                f"/projects/{project_id}/intents",
                {"from": ["origin"], "description": "manual e2e scheduling guard", "creator": "manual-e2e", "worker": "manual-e2e"},
            )

            fresh_branch = _post(server_url, f"/projects/{project_id}/branches", {"mode": "fresh_context"})
            fresh_execution = _post(
                server_url,
                f"/projects/{project_id}/branches/{fresh_branch['id']}/messages",
                {"message": "Say exactly: V34-FRESH-SESSION-READY. Keep the answer short."},
            )["execution"]
            fresh_finished = _wait_execution(server_url, project_id, fresh_execution["id"], timeout=360)
            self.assertEqual(fresh_finished["status"], "succeeded")
            self.assertEqual(fresh_finished["remote_session_out_status"], "available")
            self.assertTrue(fresh_finished["remote_session_out_id"])

            fork_branch = _post(
                server_url,
                f"/projects/{project_id}/branches",
                {"mode": "fork", "source_execution_id": fresh_finished["id"], "worker_name": "pi-e2e"},
            )
            long_prompt = "\n".join(
                [
                    "Answer with 320 numbered lines.",
                    "Each line must be exactly: V34-LONG-STREAM-NNN where NNN is 001 through 320.",
                    "Do not use tools. Do not skip numbers. Do not add commentary.",
                ]
            )
            fork_execution = _post(
                server_url,
                f"/projects/{project_id}/branches/{fork_branch['id']}/messages",
                {"message": long_prompt},
            )["execution"]
            fork_finished = _wait_execution(server_url, project_id, fork_execution["id"], timeout=420)
            self.assertEqual(fork_finished["status"], "succeeded")
            self.assertEqual(fork_finished["session_action"], "fork_initial")
            self.assertEqual(fork_finished["remote_session_in_id"], fresh_finished["remote_session_out_id"])
            self.assertEqual(fork_finished["remote_session_out_status"], "available")
            fork_events = _all_events(server_url, project_id, fork_finished["id"], limit=73)
            self.assertGreaterEqual(len(fork_events), 30)
            self.assertTrue(any(event["event_type"] == "stdout" for event in fork_events))
            self.assertTrue(any(event["event_type"] == "message" and event.get("role") == "assistant" for event in fork_events))
            self.assertEqual(fork_events[-1]["event_type"], "status")
            self.assertEqual(fork_events[-1]["payload"].get("status"), "succeeded")
            assistant_text = "\n".join(
                event["payload"].get("text", "")
                for event in fork_events
                if event["event_type"] == "message" and event.get("role") == "assistant"
            )
            self.assertIn("V34-LONG-STREAM-001", assistant_text)
            self.assertIn("V34-LONG-STREAM-320", assistant_text)

            continue_execution = _post(
                server_url,
                f"/projects/{project_id}/branches/{fork_branch['id']}/messages",
                {"message": "Reply exactly: V34-BRANCH-CONTINUE-OK"},
            )["execution"]
            continue_finished = _wait_execution(server_url, project_id, continue_execution["id"], timeout=360)
            self.assertEqual(continue_finished["status"], "succeeded")
            self.assertEqual(continue_finished["session_action"], "branch_continue")
            self.assertEqual(continue_finished["remote_session_in_id"], fork_finished["remote_session_out_id"])
            continue_events = _all_events(server_url, project_id, continue_finished["id"], limit=17)
            continue_text = "\n".join(
                event["payload"].get("text", "")
                for event in continue_events
                if event["event_type"] == "message" and event.get("role") == "assistant"
            )
            self.assertIn("V34-BRANCH-CONTINUE-OK", continue_text)

            timeline = _all_branch_events(server_url, project_id, fork_branch["id"], limit=31)
            self.assertGreaterEqual(len(timeline), len(fork_events) + len(continue_events))
            self.assertEqual(timeline[-1]["payload"].get("status"), "succeeded")
        finally:
            if dispatcher is not None:
                _stop(dispatcher)
            _stop(server)


def _start(argv: list[str]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath = str(ROOT / "cairn" / "src")
    env["PYTHONPATH"] = pythonpath if not env.get("PYTHONPATH") else f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    return subprocess.Popen(
        argv,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_real_pi_available(ssh_config: Path) -> None:
    result = subprocess.run(
        ["ssh", "-F", str(ssh_config), "-o", "BatchMode=yes", "cairn-pentestvm-v32", "whoami; command -v pi; pi --version"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"SSH/Pi probe failed: {result.stderr or result.stdout}")
    if "kali" not in result.stdout or "pi" not in result.stdout:
        raise AssertionError(f"SSH/Pi probe returned unexpected output: {result.stdout}")


def _dispatch_yaml(*, server_url: str, ssh_config: Path, base_url: str, provider_api: str, api_key: str, model: str) -> str:
    return textwrap.dedent(
        f"""
        server: "{server_url}"
        runtime:
          interval: 1
          max_workers: 2
          max_running_projects: 1
          max_project_workers: 2
          healthcheck_timeout: 45
          prompt_group: "default"
        tasks:
          bootstrap:
            timeout: 90
            conclude_timeout: 60
          reason:
            timeout: 90
            max_intents: 1
          explore:
            timeout: 90
            conclude_timeout: 60
        environments:
          - id: "pentestvm-v32"
            label: "pentestVM v3.4 E2E"
            backend: "ssh"
            workspace_root: "/home/kali/cairn-workspaces"
            ssh_command: "ssh -F {ssh_config} cairn-pentestvm-v32"
            cleanup:
              completed_action: "stop"
            terminal:
              mode: "none"
        model_profiles:
          - id: "pi-e2e"
            type: "pi"
            model: "{model}"
            context_window: 262144
        workers:
          - name: "pi-e2e"
            type: "pi"
            model_profile: "pi-e2e"
            endpoint: "pi-e2e"
            task_types: [bootstrap, reason, explore]
            max_running: 1
            priority: 0
            allowed_environments: ["pentestvm-v32"]
        """
    )


def _upsert_environment(server_url: str, ssh_config: Path, api_key: str) -> None:
    body = {
        "id": "pentestvm-v32",
        "label": "pentestVM v3.4 E2E",
        "backend": "ssh",
        "ssh_command": f"ssh -F {ssh_config} cairn-pentestvm-v32",
        "workspace_root": "/home/kali/cairn-workspaces",
        "cleanup": {"completed_action": "stop"},
        "terminal": {"mode": "none"},
        "provider_endpoints": [
            {
                "id": "pi-e2e",
                "type": "pi",
                "base_url": os.environ.get("CAIRN_E2E_BASE_URL", "http://host.docker.internal:3000"),
                "provider_api": os.environ.get("CAIRN_E2E_PROVIDER_API", "openai-completions"),
                "api_key": api_key,
            }
        ],
    }
    response = requests.post(f"{server_url}/environments", json=body, timeout=10)
    if response.status_code == 409:
        response = requests.put(f"{server_url}/environments/pentestvm-v32", json=body, timeout=10)
    response.raise_for_status()


def _wait_http(url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = requests.get(url, timeout=1)
            if response.ok:
                return
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for {url}: {last_error}")


def _post(server_url: str, path: str, payload: dict) -> dict:
    response = requests.post(f"{server_url}{path}", json=payload, timeout=20)
    if not response.ok:
        raise AssertionError(f"POST {path} failed {response.status_code}: {response.text}")
    return response.json()


def _get(server_url: str, path: str, **params) -> dict:
    response = requests.get(f"{server_url}{path}", params=params, timeout=20)
    if not response.ok:
        raise AssertionError(f"GET {path} failed {response.status_code}: {response.text}")
    return response.json()


def _wait_execution(server_url: str, project_id: str, execution_id: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        execution = _get(server_url, f"/projects/{project_id}/executions/{execution_id}")
        last_status = execution["status"]
        if last_status in {"succeeded", "failed", "cancelled"}:
            if last_status != "succeeded":
                events = _all_events(server_url, project_id, execution_id)
                tail = json.dumps(events[-5:], ensure_ascii=False)
                raise AssertionError(f"execution {execution_id} ended {last_status}: {execution}; tail={tail}")
            return execution
        time.sleep(1)
    raise AssertionError(f"Timed out waiting execution {execution_id}; last_status={last_status}")


def _all_events(server_url: str, project_id: str, execution_id: str, *, limit: int = 100) -> list[dict]:
    events: list[dict] = []
    cursor = None
    for _ in range(200):
        params = {"limit": limit}
        if cursor:
            params["after_cursor"] = cursor
        page = _get(server_url, f"/projects/{project_id}/executions/{execution_id}/events", **params)
        batch = page["events"]
        events.extend(batch)
        next_cursor = page.get("next_cursor")
        if not batch or next_cursor == cursor:
            break
        cursor = next_cursor
    return events


def _all_branch_events(server_url: str, project_id: str, branch_id: str, *, limit: int = 100) -> list[dict]:
    events: list[dict] = []
    cursor = None
    for _ in range(300):
        params = {"limit": limit}
        if cursor:
            params["after_cursor"] = cursor
        page = _get(server_url, f"/projects/{project_id}/branches/{branch_id}/timeline", **params)
        batch = page["events"]
        events.extend(batch)
        next_cursor = page.get("next_cursor")
        if not batch or next_cursor == cursor:
            break
        cursor = next_cursor
    return events


if __name__ == "__main__":
    unittest.main()
