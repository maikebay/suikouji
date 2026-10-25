"""Import confirmation artifacts expire and bind one durable request."""
from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from engine.agent_service import AgentError, AgentService, Principal
from engine.store import SqliteStore


CSV = "日期,类型,金额\n2026-10-25,支出,10\n"


class PreviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_shop("shop", "测试店", owner_id="owner")
        self.store.ensure_shop("other-shop", "另一店", owner_id="owner")
        self.service = AgentService(self.store)
        self.principal = Principal("agent", "owner")
        self.preview = self.service.preview_import(self.principal, "shop", CSV)
        self.token = self.preview["preview_token"]

    def commit(self, request_id="import-1", **kwargs):
        return self.service.commit_import(self.principal, "shop", CSV, request_id,
                                          confirmed=True, preview_token=self.token, **kwargs)

    def age(self, created_at):
        self.store.conn.execute("UPDATE agent_import_preview SET created_at=? WHERE token=?",
                                (created_at, self.token))
        self.store.conn.commit()

    def test_preview_expires_before_any_business_write(self):
        self.assertEqual(self.preview["expires_in_seconds"], 1800)
        for created_at in ("2000-01-01T00:00:00", "not-a-date",
                           (dt.datetime.now() + dt.timedelta(days=1)).isoformat()):
            with self.subTest(created_at=created_at):
                self.age(created_at)
                before = self.store.export_all("shop")
                with self.assertRaises(AgentError) as caught:
                    self.commit()
                self.assertEqual(caught.exception.code, "preview_expired")
                self.assertEqual(self.store.export_all("shop"), before)

    def test_token_is_consumed_once_but_same_request_replays_after_expiry(self):
        first = self.commit()
        self.age("2000-01-01T00:00:00")
        replay = self.commit()
        self.assertEqual(replay, {**first, "duplicate": True})
        with self.assertRaises(AgentError) as caught:
            self.commit("different-request")
        self.assertEqual(caught.exception.code, "preview_consumed")
        self.assertEqual(len(self.store.list_entries("shop", "0000", "9999")), 1)

    def test_preview_scope_and_csv_binding(self):
        for principal, workspace, csv in (
            (Principal("other-agent", "owner"), "shop", CSV),
            (self.principal, "other-shop", CSV),
            (self.principal, "shop", CSV + "\n"),
        ):
            with self.subTest(principal=principal, workspace=workspace):
                with self.assertRaises(AgentError) as caught:
                    self.service.commit_import(principal, workspace, csv, "foreign",
                                                confirmed=True, preview_token=self.token)
                self.assertEqual(caught.exception.code, "preview_mismatch")
        self.assertEqual(self.store.list_entries("shop", "0000", "9999"), [])

    def test_changed_amount_without_external_id_conflicts_and_leaves_preview_unused(self):
        self.commit()
        changed_csv = CSV.replace(",10\n", ",11\n")
        preview = self.service.preview_import(self.principal, "shop", changed_csv)
        before = self.store.export_all("shop")
        with self.assertRaises(AgentError) as caught:
            self.service.commit_import(self.principal, "shop", changed_csv, "changed-amount",
                                        confirmed=True, preview_token=preview["preview_token"])
        self.assertEqual(caught.exception.code, "import_conflict")
        self.assertEqual(self.store.export_all("shop"), before)
        self.assertIsNone(self.store.agent_import_preview_get(
            preview["preview_token"], self.principal.id, "shop")["consumed_request_id"])

    def test_old_preview_table_adds_consumption_without_changing_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy-preview.db")
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE agent_import_preview (token TEXT PRIMARY KEY, "
                               "principal_id TEXT NOT NULL, workspace_id TEXT NOT NULL, "
                               "sha256 TEXT NOT NULL, plan TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute("INSERT INTO agent_import_preview VALUES(?,?,?,?,?,?)",
                               ("old-token", "agent", "shop", "hash", '{"rows": []}',
                                "2026-10-25T12:00:00"))
            connection.commit()
            connection.close()
            migrated = SqliteStore(path)
            try:
                saved = migrated.agent_import_preview_get("old-token", "agent", "shop")
                self.assertEqual(saved["plan"], {"rows": []})
                self.assertIsNone(saved["consumed_request_id"])
                self.assertTrue(migrated.agent_import_preview_consume("old-token", "agent", "shop", "request"))
                self.assertFalse(migrated.agent_import_preview_consume("old-token", "agent", "shop", "other"))
            finally:
                migrated.close()
            reopened = SqliteStore(path)
            try:
                self.assertEqual(reopened.agent_import_preview_get(
                    "old-token", "agent", "shop")["consumed_request_id"], "request")
            finally:
                reopened.close()

    def test_failed_response_does_not_consume_token(self):
        with patch.object(self.store, "agent_request_finish", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.commit()
        self.assertEqual(self.store.list_entries("shop", "0000", "9999"), [])
        first = self.commit("retry-with-new-id")
        self.assertEqual(first["result"]["imported"], 1)


if __name__ == "__main__":
    unittest.main()
