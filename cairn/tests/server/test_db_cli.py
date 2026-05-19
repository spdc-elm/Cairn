from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from click.testing import CliRunner

from cairn.cli import main
from cairn.server.migrations import runner


class DbCliTests(unittest.TestCase):
    def test_db_status_and_migrate(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "cairn.db"
        cli = CliRunner()

        missing = cli.invoke(main, ["db", "status", "--db", str(path)])
        self.assertEqual(missing.exit_code, 0, missing.output)
        self.assertIn("exists: no", missing.output)
        self.assertIn("pending: 0001_initial, 0002_current_additive_schema", missing.output)

        migrated = cli.invoke(main, ["db", "migrate", "--db", str(path)])
        self.assertEqual(migrated.exit_code, 0, migrated.output)
        self.assertIn("applied now: 0001_initial, 0002_current_additive_schema", migrated.output)
        self.assertIn("pending: none", migrated.output)

        status = cli.invoke(main, ["db", "status", "--db", str(path)])
        self.assertEqual(status.exit_code, 0, status.output)
        self.assertIn("exists: yes", status.output)
        self.assertIn("applied: 0001_initial, 0002_current_additive_schema", status.output)
        self.assertIn("pending: none", status.output)

    def test_db_status_uses_available_migration_order(self) -> None:
        versions = tuple(migration.version for migration in runner.available_migrations())

        self.assertEqual(versions, tuple(sorted(versions)))


if __name__ == "__main__":
    unittest.main()
