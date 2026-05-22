from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from click.testing import CliRunner

from cairn.cli import main
from cairn.server.migrations import runner
from cairn.server.services import dumps_json, utcnow


class PruneRawEventsTests(unittest.TestCase):
    def test_prune_raw_events_sanitises_only_stdout_stderr_and_preserves_semantic_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cairn.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            runner.migrate(conn)
            now = utcnow()
            conn.execute(
                "INSERT INTO projects (id, title, status, created_at) VALUES ('proj_004', 'keep', 'active', ?)",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO execution_runs (id, project_id, task_type, phase, status, created_at, updated_at)
                VALUES ('ex001', 'proj_004', 'question', 'followup', 'succeeded', ?, ?)
                """,
                (now, now),
            )
            raw = {"text": json.dumps({"type": "agent_end", "messages": [{"role": "user", "content": "x" * 20000}]})}
            semantic = {"text": "final assistant", "status": "success"}
            conn.execute(
                """
                INSERT INTO execution_events (id, execution_id, project_id, seq, project_seq, cursor, ts, event_type, role, payload_json, event_key, created_at)
                VALUES ('evt_raw', 'ex001', 'proj_004', 1, 1, 'c1', ?, 'stdout', NULL, ?, 'raw', ?)
                """,
                (now, dumps_json(raw), now),
            )
            conn.execute(
                """
                INSERT INTO execution_events (id, execution_id, project_id, seq, project_seq, cursor, ts, event_type, role, payload_json, event_key, created_at)
                VALUES ('evt_msg', 'ex001', 'proj_004', 2, 2, 'c2', ?, 'message', 'assistant', ?, 'msg', ?)
                """,
                (now, dumps_json(semantic), now),
            )
            conn.commit()
            conn.close()

            runner_cli = CliRunner()
            dry = runner_cli.invoke(main, ["db", "prune-raw-events", "--db-path", str(db_path), "--dry-run"])
            self.assertEqual(dry.exit_code, 0, dry.output)

            applied = runner_cli.invoke(main, ["db", "prune-raw-events", "--db-path", str(db_path), "--yes"])
            self.assertEqual(applied.exit_code, 0, applied.output)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT event_type, payload_json FROM execution_events ORDER BY project_seq").fetchall()
            self.assertEqual(rows[1]["event_type"], "message")
            self.assertIn("final assistant", rows[1]["payload_json"])
            self.assertIn("messages_omitted", rows[0]["payload_json"])
            self.assertNotIn('"messages"', rows[0]["payload_json"])
            self.assertIsNotNone(conn.execute("SELECT 1 FROM projects WHERE id = 'proj_004'").fetchone())


if __name__ == "__main__":
    unittest.main()
