from __future__ import annotations

import sqlite3

VERSION = "0006_worker_inventory_capability"
DESCRIPTION = "Backfill worker inventory public question capability columns"


def apply(conn: sqlite3.Connection) -> None:
    _add_column(conn, "worker_inventory", "question_capability_json", "TEXT")
    _add_column(conn, "worker_inventory", "capability_updated_at", "TEXT")
    _add_column(conn, "worker_inventory", "capability_source", "TEXT")


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
