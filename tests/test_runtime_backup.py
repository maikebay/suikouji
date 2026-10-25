from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.backup import backup_database, inspect_database, restore_database
from engine.runtime import RuntimeConfig, probe_database
from engine.store import SqliteStore


class RuntimeConfigTests(unittest.TestCase):
    def test_non_loopback_requires_long_api_key(self):
        with self.assertRaises(ValueError):
            RuntimeConfig("/tmp/staging.db", bind="0.0.0.0").validate()
        with self.assertRaises(ValueError):
            RuntimeConfig("/tmp/staging.db", bind="0.0.0.0", api_key="short").validate()
        RuntimeConfig("/tmp/staging.db", bind="0.0.0.0", api_key="x" * 32).validate()

    def test_probe_rejects_missing_control_tables_or_columns_without_migration(self):
        for alteration in ("DROP TABLE agent_request", "DROP TABLE agent_import_preview",
                           "ALTER TABLE agent_request DROP COLUMN lease_token",
                           "ALTER TABLE agent_import_preview DROP COLUMN consumed_request_id"):
            with self.subTest(alteration=alteration), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "incomplete.db"
                store = SqliteStore(str(path))
                store.ensure_shop("shop", owner_id="owner")
                store.conn.execute(alteration)
                store.conn.commit()
                store.close()
                before = path.read_bytes()
                self.assertFalse(probe_database(str(path), "owner", "shop")["ok"])
                self.assertEqual(path.read_bytes(), before)

    def test_probe_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "missing.db")
            self.assertEqual(probe_database(path), {"ok": False, "reason": "database_missing"})
            self.assertFalse(Path(path).exists())


class BackupTests(unittest.TestCase):
    def test_backup_restore_and_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            backup = Path(tmp) / "backup.db"
            restored = Path(tmp) / "restored.db"
            store = SqliteStore(str(source))
            store.ensure_shop("shop", "测试店", owner_id="owner")
            store.agent_request_claim("agent", "shop", "capture", "backup-req", "fingerprint")
            store.agent_import_preview_put("backup-preview", "agent", "shop", "sha256", {"rows": []})
            store.agent_import_preview_consume("backup-preview", "agent", "shop", "backup-import")
            store.close()
            self.assertTrue(inspect_database(str(source))["ok"])
            self.assertTrue(backup_database(str(source), str(backup))["ok"])
            self.assertTrue(restore_database(str(backup), str(restored))["ok"])
            self.assertTrue(inspect_database(str(restored))["ok"])
            self.assertEqual(probe_database(str(restored), "owner", "shop")["ok"], True)
            restored_store = SqliteStore(str(restored))
            self.assertIsNotNone(restored_store.agent_request_get("agent", "shop", "capture", "backup-req"))
            preview = restored_store.agent_import_preview_get("backup-preview", "agent", "shop")
            self.assertEqual(preview["sha256"], "sha256")
            self.assertEqual(preview["consumed_request_id"], "backup-import")
            restored_store.close()

    def test_restore_does_not_overwrite_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            backup = Path(tmp) / "backup.db"
            target = Path(tmp) / "target.db"
            store = SqliteStore(str(source))
            store.ensure_shop("shop", "测试店", owner_id="owner")
            store.close()
            backup_database(str(source), str(backup))
            target.write_bytes(b"keep this file")
            with self.assertRaises(ValueError):
                restore_database(str(backup), str(target))
            self.assertEqual(target.read_bytes(), b"keep this file")


if __name__ == "__main__":
    unittest.main()
