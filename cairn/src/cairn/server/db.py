from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from cairn.server.migrations import runner

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

LOG = logging.getLogger(__name__)
_db_path: Path | None = None


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        migration_status = runner.migrate(conn)
    LOG.info("database ready path=%s latest=%s pending=%s", path, migration_status.latest, len(migration_status.pending))


@contextmanager
def connect(path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    with connect(_db_path) as conn:
        yield conn
