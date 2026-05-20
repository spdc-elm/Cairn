from __future__ import annotations

import sqlite3

from cairn.server.models import RunProvenance
from cairn.server.questions.models import QuestionThread
from cairn.server.services import get_project_or_404


def build_question_context(conn: sqlite3.Connection, thread: QuestionThread, user_message: str) -> dict:
    project = get_project_or_404(conn, thread.project_id)
    prior_rows = conn.execute(
        """
        SELECT seq, message, status, result_text, error_code, error_detail
        FROM question_jobs
        WHERE thread_id = ?
        ORDER BY seq
        """,
        (thread.id,),
    ).fetchall()
    history = []
    for row in prior_rows:
        history.append({"role": "user", "text": row["message"], "seq": row["seq"]})
        if row["status"] == "succeeded" and row["result_text"]:
            history.append({"role": "assistant", "text": row["result_text"], "seq": row["seq"]})
        elif row["status"] == "failed":
            history.append({"role": "system", "text": row["error_detail"] or row["error_code"], "seq": row["seq"]})
    context = {
        "project": {"id": project["id"], "title": project["title"]},
        "anchor": {
            "type": thread.anchor_type,
            "id": thread.anchor_id,
            "summary": _anchor_summary(conn, thread),
        },
        "source_run_log_id": thread.source_run_log_id,
        "source_session": thread.source_session.model_dump(mode="json"),
        "run_provenance": thread.anchor_resolution.provenance.model_dump(mode="json") if thread.anchor_resolution.provenance else None,
        "mode": thread.mode,
        "session_effect": thread.session_effect,
        "prior_messages": history,
        "user_message": user_message,
    }
    if thread.mode == "resume":
        context["mode_instruction"] = "Continue the source remote session. Do not write to Cairn unless the user explicitly requested it."
    elif thread.mode == "fork":
        context["mode_instruction"] = "Answer in a forked remote session derived from the source session."
    else:
        context["mode_instruction"] = "Answer from the provided Cairn context. You are not connected to the original worker session."
    return context


def build_question_prompt(
    conn: sqlite3.Connection,
    thread: QuestionThread,
    user_message: str,
    *,
    transcript_summary: str | None = None,
) -> str:
    project = get_project_or_404(conn, thread.project_id)
    anchor = _anchor_summary(conn, thread)
    provenance = _provenance_summary(thread.anchor_resolution.provenance)
    parts = [
        "You are answering a short interactive Cairn follow-up question.",
        "Do not modify the Cairn blackboard. Workspace writes require the user's explicit request.",
        "",
        "Project:",
        f"- id: {project['id']}",
        f"- title: {project['title']}",
        "",
        "Anchor:",
        anchor,
        "",
        "Run provenance:",
        provenance,
    ]
    if thread.messages:
        parts.extend(["", "Prior Q&A messages:"])
        for message in thread.messages:
            parts.append(f"- {message.role}: {message.text or ''}")
    if transcript_summary:
        parts.extend(["", "Source transcript summary:", transcript_summary])
    parts.extend(["", "User question:", user_message])
    return "\n".join(parts)


def _anchor_summary(conn: sqlite3.Connection, thread: QuestionThread) -> str:
    if thread.anchor_type == "fact":
        row = conn.execute(
            "SELECT id, title, description, metadata_json FROM facts WHERE project_id = ? AND id = ?",
            (thread.project_id, thread.anchor_id),
        ).fetchone()
        if row is None:
            return f"- fact: {thread.anchor_id} (missing)"
        return "\n".join(
            [
                f"- fact: {row['id']}",
                f"- title: {row['title'] or row['id']}",
                f"- description: {row['description']}",
            ]
        )
    if thread.anchor_type == "intent":
        row = conn.execute(
            "SELECT id, description, worker, concluded_at FROM intents WHERE project_id = ? AND id = ?",
            (thread.project_id, thread.anchor_id),
        ).fetchone()
        if row is None:
            return f"- intent: {thread.anchor_id} (missing)"
        return "\n".join(
            [
                f"- intent: {row['id']}",
                f"- description: {row['description']}",
                f"- worker: {row['worker'] or 'none'}",
                f"- concluded_at: {row['concluded_at'] or 'open'}",
            ]
        )
    return f"- run: {thread.anchor_id}"


def _provenance_summary(provenance: RunProvenance | None) -> str:
    if provenance is None:
        return "- status: missing"
    return "\n".join(
        [
            f"- run_log_id: {provenance.run_log_id}",
            f"- worker: {provenance.worker_name}",
            f"- worker_type: {provenance.worker_type or 'unknown'}",
            f"- workspace: {provenance.workspace or 'unknown'}",
            f"- remote_session_status: {provenance.remote_session.status}",
            f"- remote_session_kind: {provenance.remote_session.kind or 'none'}",
        ]
    )
