from __future__ import annotations

from cairn.server.db import get_conn
from cairn.server.models import WorkerInventoryItem, WorkerInventoryUpsertRequest
from cairn.server.services import dumps_json, loads_json_list, utcnow

from fastapi import APIRouter

router = APIRouter(tags=["workers"])


@router.get("/workers", response_model=list[WorkerInventoryItem])
def list_workers():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM worker_inventory ORDER BY priority, name").fetchall()
        return [
            WorkerInventoryItem(
                name=row["name"],
                type=row["type"],
                model_profile=row["model_profile"],
                endpoint=row["endpoint"],
                task_types=loads_json_list(row["task_types_json"]) or [],
                max_running=row["max_running"],
                priority=row["priority"],
                allowed_environments=loads_json_list(row["allowed_environments_json"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]


@router.put("/workers", response_model=list[WorkerInventoryItem])
def upsert_workers(body: WorkerInventoryUpsertRequest):
    with get_conn() as conn:
        now = utcnow()
        names = [worker.name for worker in body.workers]
        conn.execute("DELETE FROM worker_inventory")
        for worker in body.workers:
            conn.execute(
                """
                INSERT INTO worker_inventory (
                    name, type, model_profile, endpoint, task_types_json, max_running,
                    priority, allowed_environments_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker.name,
                    worker.type,
                    worker.model_profile,
                    worker.endpoint,
                    dumps_json(worker.task_types) or "[]",
                    worker.max_running,
                    worker.priority,
                    dumps_json(worker.allowed_environments),
                    now,
                ),
            )
        rows = conn.execute(
            "SELECT * FROM worker_inventory WHERE name IN (%s) ORDER BY priority, name"
            % ",".join("?" for _ in names),
            tuple(names),
        ).fetchall() if names else []
        return [
            WorkerInventoryItem(
                name=row["name"],
                type=row["type"],
                model_profile=row["model_profile"],
                endpoint=row["endpoint"],
                task_types=loads_json_list(row["task_types_json"]) or [],
                max_running=row["max_running"],
                priority=row["priority"],
                allowed_environments=loads_json_list(row["allowed_environments_json"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
