from __future__ import annotations

from pathlib import PurePosixPath

from cairn.dispatcher.runtime.environments.base import EnvironmentHandle, WorkEnvironment


def build_report_path(handle: EnvironmentHandle, intent_id: str, run_id: str | None = None) -> str:
    suffix = f"{intent_id}-{run_id}" if run_id else intent_id
    return str(PurePosixPath(handle.workspace) / ".cairn" / "reports" / f"execution-{suffix}.md")


def report_instruction(report_path: str) -> str:
    return (
        "Before returning your final JSON, write a Markdown execution report to this exact path:\n\n"
        f"{report_path}\n\n"
        "Keep the JSON fact description short. Put commands, evidence, artifacts, failures, and uncertainty in the report."
    )


def validate_report_written(environment: WorkEnvironment, handle: EnvironmentHandle, report_path: str) -> bool:
    return environment.is_path_in_workspace(handle, report_path) and environment.exists(handle, report_path)


def write_failure_report(
    environment: WorkEnvironment,
    handle: EnvironmentHandle,
    report_path: str,
    *,
    intent_id: str,
    worker: str,
    run_id: str | None,
    reason: str,
    details: str = "",
) -> None:
    body = "\n".join(
        [
            "# Execution Report",
            "",
            "## Intent",
            intent_id,
            "",
            "## Summary",
            "The run did not produce a successful fact.",
            "",
            "## Failures And Dead Ends",
            f"- worker: {worker}",
            f"- run_id: {run_id or 'unknown'}",
            f"- reason: {reason}",
            f"- details: {details}" if details else "- details: unavailable",
            "",
            "## Uncertainty",
            "Retry or edit the intent after reviewing dispatcher logs and workspace artifacts.",
            "",
        ]
    )
    environment.write_text_file(handle, report_path, body)


def metadata_for_report(report_path: str, run_id: str | None, worker: str, intent_id: str) -> dict:
    return {
        "report_path": report_path,
        "run_id": run_id,
        "worker": worker,
        "intent_id": intent_id,
    }
