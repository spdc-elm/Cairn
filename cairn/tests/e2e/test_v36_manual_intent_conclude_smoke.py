from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest

from tests.e2e.test_v34_stream_reliability_smoke import (
    E2E_API_KEY,
    E2E_DB,
    SSH_CONFIG,
    _all_events,
    _assert_real_pi_available,
    _free_port,
    _get,
    _post,
    _start,
    _stop,
    _upsert_environment,
    _wait_execution,
    _wait_http,
)


ROOT = Path(__file__).resolve().parents[3]


class V36ManualIntentConcludeSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CAIRN_E2E_SSH_PI") == "1", "real SSH/Pi sign-off requires CAIRN_E2E_SSH_PI=1")
    def test_real_ssh_pi_manual_conclude_failed_session_and_long_stream(self) -> None:
        db_path = Path(os.environ.get("CAIRN_E2E_DB", str(E2E_DB)))
        ssh_config = Path(os.environ.get("CAIRN_E2E_SSH_CONFIG", str(SSH_CONFIG)))
        api_key = os.environ.get("CAIRN_E2E_API_KEY", E2E_API_KEY)
        port = int(os.environ.get("CAIRN_E2E_SERVER_PORT", "0")) or _free_port()
        server_url = f"http://127.0.0.1:{port}"
        _assert_real_pi_available(ssh_config)

        with tempfile.TemporaryDirectory() as tmp:
            dispatch_config = Path(tmp) / "dispatch.v36.pi.yaml"
            dispatch_config.write_text(
                _dispatch_yaml_v36(
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
            dispatcher = None
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
                project_id = self._create_project(server_url)
                source_execution = self._prepare_failed_available_intent_execution(server_url, project_id)

                prompt = _get(server_url, f"/projects/{project_id}/intents/i001/manual-conclude-prompt")
                self.assertEqual(prompt["source_execution_id"], source_execution["id"])
                self.assertTrue(prompt["source_session_available"])
                self.assertIn("Return only one raw JSON object", prompt["prompt"])

                invalid = self._post_allow_error(
                    server_url,
                    f"/projects/{project_id}/intents/i001/manual-conclude",
                    {"actor": "manual-e2e", "source_execution_id": source_execution["id"], "raw_json": '{"accepted": false, "reason": "not_enough"}'},
                )
                self.assertEqual(invalid.status_code, 400)

                json_text = self._resume_for_conclude_json(server_url, project_id, source_execution["id"], prompt["prompt"])
                result = _post(
                    server_url,
                    f"/projects/{project_id}/intents/i001/manual-conclude",
                    {"actor": "manual-e2e", "source_execution_id": source_execution["id"], "raw_json": json_text},
                )
                self.assertEqual(result["intent"]["to"], result["fact"]["id"])

                duplicate = self._post_allow_error(
                    server_url,
                    f"/projects/{project_id}/intents/i001/manual-conclude",
                    {"actor": "manual-e2e", "source_execution_id": source_execution["id"], "raw_json": '{"description": "duplicate"}'},
                )
                self.assertEqual(duplicate.status_code, 409)

                graph = _get(server_url, f"/projects/{project_id}/graph")
                fact = next(item for item in graph["facts"] if item["id"] == result["fact"]["id"])
                self.assertEqual(fact["producing_execution_id"], source_execution["id"])
                concluded_intent = next(item for item in graph["intents"] if item["id"] == "i001")
                self.assertEqual(concluded_intent["to"], result["fact"]["id"])

                self._assert_fork_resume_and_long_stream(server_url, project_id, source_execution["id"])
            finally:
                if dispatcher is not None:
                    _stop(dispatcher)
                _stop(server)

    def _create_project(self, server_url: str) -> str:
        project = _post(
            server_url,
            "/projects",
            {
                "title": f"v3.6 manual conclude e2e {int(time.time())}",
                "origin": "V3.6 manual conclude E2E origin.",
                "goal": "Verify manual conclude, failed-session fork/resume, and long stream output.",
                "environment_id": "pentestvm-v32",
                "auto_reason": False,
            },
        )["project"]
        _post(
            server_url,
            f"/projects/{project['id']}/intents",
            {"from": ["origin"], "description": "manual conclude E2E intent", "creator": "manual-e2e"},
        )
        return project["id"]

    def _prepare_failed_available_intent_execution(self, server_url: str, project_id: str) -> dict:
        fresh_branch = _post(server_url, f"/projects/{project_id}/branches", {"mode": "fresh_context"})
        fresh_execution = _post(
            server_url,
            f"/projects/{project_id}/branches/{fresh_branch['id']}/messages",
            {"message": "Reply exactly: V36-MANUAL-SOURCE-SESSION-READY"},
        )["execution"]
        fresh_finished = _wait_execution(server_url, project_id, fresh_execution["id"], timeout=360)
        self.assertEqual(fresh_finished["remote_session_out_status"], "available")
        execution = _post(
            server_url,
            f"/projects/{project_id}/executions",
            {"intent_id": "i001", "task_type": "explore", "phase": "run"},
        )
        leased = _post(
            server_url,
            "/dispatcher/executions/lease",
            {
                "execution_id": execution["id"],
                "dispatcher_id": "manual-e2e",
                "worker_name": "pi-e2e",
                "worker_type": "pi",
                "environment_id": "pentestvm-v32",
                "endpoint_id": "pi-e2e",
                "model_profile_id": "pi-e2e",
            },
        )
        patched = self._patch(
            server_url,
            f"/dispatcher/executions/{leased['id']}",
            {
                "status": "failed",
                "remote_session_out_kind": fresh_finished["remote_session_out_kind"],
                "remote_session_out_id": fresh_finished["remote_session_out_id"],
                "remote_session_out_status": "available",
                "error_code": "manual_e2e_failed_source",
                "error_detail": "synthetic failed execution with available real Pi session",
            },
        )
        with_worker = self._patch(
            server_url,
            f"/dispatcher/executions/{leased['id']}",
            {"metadata": {"manual_e2e_source": True}},
        )
        return {**patched, **with_worker}

    def _resume_for_conclude_json(self, server_url: str, project_id: str, source_execution_id: str, prompt: str) -> str:
        branch = _post(server_url, f"/projects/{project_id}/branches", {"mode": "resume", "source_execution_id": source_execution_id})
        execution = _post(
            server_url,
            f"/projects/{project_id}/branches/{branch['id']}/messages",
            {"message": prompt + '\n\nReturn JSON with title "V36 Manual Fact" and description "V36 manual conclude confirmed result".'},
        )["execution"]
        finished = _wait_execution(server_url, project_id, execution["id"], timeout=360)
        events = _all_events(server_url, project_id, finished["id"], limit=41)
        text = "\n".join(
            event.get("payload", {}).get("text", "")
            for event in events
            if event.get("event_type") == "message" and event.get("role") == "assistant"
        )
        self.assertIn("V36 manual conclude confirmed result", text)
        return text

    def _assert_fork_resume_and_long_stream(self, server_url: str, project_id: str, source_execution_id: str) -> None:
        fork = _post(server_url, f"/projects/{project_id}/branches", {"mode": "fork", "source_execution_id": source_execution_id})
        long_prompt = "\n".join(
            [
                "Answer with 320 numbered lines.",
                "Each line must be exactly: V36-LONG-STREAM-NNN where NNN is 001 through 320.",
                "Do not use tools. Do not skip numbers. Do not add commentary.",
            ]
        )
        long_execution = _post(server_url, f"/projects/{project_id}/branches/{fork['id']}/messages", {"message": long_prompt})["execution"]
        long_finished = _wait_execution(server_url, project_id, long_execution["id"], timeout=420)
        events = _all_events(server_url, project_id, long_finished["id"], limit=37)
        text = "\n".join(
            event.get("payload", {}).get("text", "")
            for event in events
            if event.get("event_type") == "message" and event.get("role") == "assistant"
        )
        self.assertIn("V36-LONG-STREAM-001", text)
        self.assertIn("V36-LONG-STREAM-320", text)
        self.assertEqual(events[-1]["event_type"], "status")
        self.assertEqual(events[-1]["payload"]["status"], "succeeded")

        resume = _post(server_url, f"/projects/{project_id}/branches", {"mode": "resume", "source_execution_id": source_execution_id})
        resume_execution = _post(
            server_url,
            f"/projects/{project_id}/branches/{resume['id']}/messages",
            {"message": "Reply exactly: V36-RESUME-OK"},
        )["execution"]
        resume_finished = _wait_execution(server_url, project_id, resume_execution["id"], timeout=360)
        resume_events = _all_events(server_url, project_id, resume_finished["id"], limit=29)
        resume_text = "\n".join(
            event.get("payload", {}).get("text", "")
            for event in resume_events
            if event.get("event_type") == "message" and event.get("role") == "assistant"
        )
        self.assertIn("V36-RESUME-OK", resume_text)

    def _post_allow_error(self, server_url: str, path: str, payload: dict):
        import requests

        return requests.post(f"{server_url}{path}", json=payload, timeout=20)

    def _patch(self, server_url: str, path: str, payload: dict) -> dict:
        import requests

        response = requests.patch(f"{server_url}{path}", json=payload, timeout=20)
        if not response.ok:
            raise AssertionError(f"PATCH {path} failed {response.status_code}: {response.text}")
        return response.json()


def _dispatch_yaml_v36(*, server_url: str, ssh_config: Path, base_url: str, provider_api: str, api_key: str, model: str) -> str:
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
            label: "pentestVM v3.6 E2E"
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
            task_types: [bootstrap, reason, explore, question]
            max_running: 1
            priority: 0
            allowed_environments: ["pentestvm-v32"]
        """
    )


if __name__ == "__main__":
    unittest.main()
