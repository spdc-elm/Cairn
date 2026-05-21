from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cairn.server import db


class V34SqliteWriteBackpressureTests(unittest.TestCase):
    def test_connection_sets_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with db.connect(Path(tmp) / "cairn.db") as conn:
                value = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertGreaterEqual(value, 5000)


if __name__ == "__main__":
    unittest.main()
