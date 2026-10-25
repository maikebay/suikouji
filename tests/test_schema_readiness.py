"""Every current Store column is required before runtime may open a writer."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from engine.runtime import probe_database
from engine.store import SqliteStore, database_schema_ready


class CompleteSchemaReadinessTests(unittest.TestCase):
    def test_each_runtime_column_is_checked_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.db"
            with closing(SqliteStore(str(path))) as store:
                store.ensure_shop("shop", owner_id="owner")
                columns = [(table[0], column[1])
                           for table in store.conn.execute(
                               "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                           for column in store.conn.execute(f'PRAGMA table_info("{table[0]}")').fetchall()]
            self.assertTrue(probe_database(str(path), "owner", "shop")["ok"])
            for table, column in columns:
                with self.subTest(table=table, column=column):
                    with closing(sqlite3.connect(str(path))) as writer:
                        writer.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{column}" TO "missing_column"')
                        writer.commit()
                    before = path.read_bytes()
                    try:
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                            self.assertFalse(database_schema_ready(reader))
                        self.assertFalse(probe_database(str(path), "owner", "shop")["ok"])
                        self.assertEqual(path.read_bytes(), before)
                    finally:
                        with closing(sqlite3.connect(str(path))) as writer:
                            writer.execute(f'ALTER TABLE "{table}" RENAME COLUMN "missing_column" TO "{column}"')
                            writer.commit()


if __name__ == "__main__":
    unittest.main()
