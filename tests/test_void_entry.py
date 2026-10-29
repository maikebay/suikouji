"""Soft-void ledger entries: schema v8, API contract, summary exclusion."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from engine.agent_service import AgentError, AgentService, Principal
from engine.deployment import migrate_database
from engine.store import SCHEMA_VERSION, SqliteStore, database_schema_ready
from tests import test_agent_http_contract as http_contract


P = Principal("agent", "owner")
RECEIVED = "2026-10-29T20:00:00+08:00"


class VoidEntryStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "void.db"
        self.store = SqliteStore(str(self.db))
        self.addCleanup(self.store.close)
        self.store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
        self.service = AgentService(self.store)

    def _capture_purchase(self, request_id="buy-1", amount=10.5, item="马蹄粉"):
        text = f"今天进了{item}{amount}元"
        structured = {
            "entries": [{
                "date": "2026-10-29", "type": "purchase", "category": "食材成本",
                "amount": amount, "item": item, "amount_basis": "gross",
                "granularity": "transaction", "assertion": "actual",
                "source_quote": text,
            }],
            "sales": [], "observations": [], "leftovers": [],
        }
        return self.service.capture_text(
            P, "shop", text, request_id, received_at=RECEIVED, structured=structured)

    def test_fresh_database_is_schema_v8_with_void_columns(self):
        self.assertEqual(SCHEMA_VERSION, 8)
        self.assertTrue(database_schema_ready(self.store.conn))
        columns = {row[1] for row in self.store.conn.execute("PRAGMA table_info(entry)")}
        self.assertTrue({"voided_at", "void_reason", "voided_by"} <= columns)
        self.assertTrue(self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='entry_void_audit'").fetchone())

    def test_void_excludes_from_list_and_summary_and_is_idempotent(self):
        captured = self._capture_purchase()
        entry_id = captured["entries"][0]["id"]
        self.assertEqual(len(self.service.entries(P, "shop")), 1)
        self.assertEqual(self.service.summary(P, "shop")["purchase"], 10.5)

        first = self.service.void_entry(P, "shop", entry_id, "void-1", reason="听错了")
        self.assertFalse(first["duplicate"])
        self.assertFalse(first["already_voided"])
        self.assertEqual(first["entry"]["void_reason"], "听错了")
        self.assertEqual(first["entry"]["voided_by"], "agent")
        self.assertIsNotNone(first["entry"]["voided_at"])
        self.assertIsNotNone(first["audit_id"])

        self.assertEqual(self.service.entries(P, "shop"), [])
        self.assertEqual(self.service.summary(P, "shop")["purchase"], 0)
        kept = self.store.list_entries("shop", "0000", "9999", include_voided=True)
        self.assertEqual([(row["id"], row["void_reason"]) for row in kept],
                         [(entry_id, "听错了")])

        again = self.service.void_entry(P, "shop", entry_id, "void-2", reason="再作废一次")
        self.assertTrue(again["already_voided"])
        self.assertEqual(again["entry"]["void_reason"], "听错了")
        self.assertIsNone(again["audit_id"])

        replay = self.service.void_entry(P, "shop", entry_id, "void-1", reason="听错了")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["entry"]["id"], entry_id)

        detail = self.service.capture_detail(P, "shop", captured["capture"]["id"])
        self.assertEqual(detail["entries"][0]["id"], entry_id)
        self.assertIsNotNone(detail["entries"][0]["voided_at"])

        timeline = self.service.timeline(P, "shop")
        item = next(row for row in timeline["items"]
                    if row["record"]["id"] == captured["capture"]["id"])
        self.assertEqual(item["projection_counts"]["entries"], 0)
        self.assertEqual(item["ledger_preview"], [])

    def test_void_rejects_other_workspace_entry(self):
        captured = self._capture_purchase()
        entry_id = captured["entries"][0]["id"]
        self.store.ensure_shop("other", "别店", owner_id="owner", profile_id="restaurant")
        with self.assertRaises(AgentError) as caught:
            self.service.void_entry(P, "other", entry_id, "void-x", reason="错店")
        self.assertEqual(caught.exception.code, "entry_not_found")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(self.service.entries(P, "shop")), 1)


class VoidEntryHttpTests(http_contract.AgentHttpContractTests):
    def test_void_entry_http_contract(self):
        status, _, captured = self.request("POST", "/v1/captures", {
            "workspace_id": "shop",
            "request_id": "http-buy",
            "received_at": RECEIVED,
            "text": "今天进了马蹄粉12元",
            "structured": {
                "entries": [{
                    "date": "2026-10-29", "type": "purchase", "category": "食材成本",
                    "amount": 12, "item": "马蹄粉", "amount_basis": "gross",
                    "granularity": "transaction", "assertion": "actual",
                    "source_quote": "今天进了马蹄粉12元",
                }],
                "sales": [], "observations": [], "leftovers": [],
            },
        })
        self.assertEqual(status, 200)
        entry_id = captured["entries"][0]["id"]
        payload = {"workspace_id": "shop", "request_id": "http-void", "reason": "测试作废"}
        status, _, body = self.request("POST", f"/v1/entries/{entry_id}/void", payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["duplicate"])
        self.assertFalse(body["already_voided"])
        self.assertEqual(body["entry"]["void_reason"], "测试作废")
        status, _, entries = self.request("GET", "/v1/entries?workspace_id=shop")
        self.assertEqual(status, 200)
        self.assertEqual(entries["entries"], [])
        status, _, caps = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        void_cap = next(c for c in caps["capabilities"] if c["id"] == "ledger.void")
        self.assertEqual(void_cap["status"], "available")
        self.assertEqual(caps["operations"]["void_entry"]["path"], "/v1/entries/{entry_id}/void")


class VoidEntryMigrateTests(unittest.TestCase):
    def test_v6_copy_migration_reaches_v8_with_void_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "v6.db", root / "v8.db"
            with closing(SqliteStore(str(source))) as store:
                store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
                store.conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
                for name in ("custom_field", "custom_field_option", "custom_field_config",
                             "custom_field_audit", "custom_entry_value", "custom_sale_value",
                             "custom_observation_value", "custom_thought_value",
                             "custom_field_gap", "custom_field_suggestion",
                             "custom_field_preview", "custom_field_alias",
                             "custom_import_mapping", "entry_void_audit"):
                    store.conn.execute(f"DROP TABLE IF EXISTS {name}")
                for name in ("idx_entry_workspace_id", "idx_sale_workspace_id",
                             "idx_observation_workspace_id", "idx_entry_void_audit",
                             "idx_entry_active"):
                    store.conn.execute(f"DROP INDEX IF EXISTS {name}")
                cols = [row[1] for row in store.conn.execute("PRAGMA table_info(entry)")]
                if "voided_at" in cols:
                    keep = [c for c in cols if c not in ("voided_at", "void_reason", "voided_by")]
                    store.conn.execute("ALTER TABLE entry RENAME TO entry_old")
                    store.conn.execute(
                        f"CREATE TABLE entry AS SELECT {', '.join(keep)} FROM entry_old")
                    store.conn.execute("DROP TABLE entry_old")
                    store.conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_entry_shop_date ON entry(shop_id, date)")
                store.conn.commit()
            with closing(sqlite3.connect(source)) as conn:
                self.assertTrue(database_schema_ready(conn, expected_version=6))
            original = source.read_bytes()
            result = migrate_database(str(source), str(destination))
            self.assertEqual(result["source_schema_version"], 6)
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(source.read_bytes(), original)
            with closing(sqlite3.connect(destination)) as conn:
                self.assertTrue(database_schema_ready(conn))
                columns = {row[1] for row in conn.execute("PRAGMA table_info(entry)")}
                self.assertTrue({"voided_at", "void_reason", "voided_by"} <= columns)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM entry_void_audit").fetchone()[0], 0)

    def test_v7_copy_migration_reaches_v8_with_void_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "v7.db", root / "v8.db"
            with closing(SqliteStore(str(source))) as store:
                store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
                entry_id = store.add_entries([{
                    "shop_id": "shop", "date": "2026-10-29", "type": "purchase",
                    "category": "食材成本", "amount": 12.5, "item": "马蹄粉",
                }])[0]
                store.conn.execute("DROP INDEX IF EXISTS idx_entry_active")
                store.conn.execute("DROP INDEX IF EXISTS idx_entry_void_audit")
                store.conn.execute("DROP TABLE IF EXISTS entry_void_audit")
                for column in ("voided_by", "void_reason", "voided_at"):
                    store.conn.execute(f"ALTER TABLE entry DROP COLUMN {column}")
                store.conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
                store.conn.commit()
            with closing(sqlite3.connect(source)) as conn:
                self.assertTrue(database_schema_ready(conn, expected_version=7))
                self.assertEqual(conn.execute(
                    "SELECT amount, item FROM entry WHERE id=?", (entry_id,)).fetchone(),
                    (12.5, "马蹄粉"))
            original = source.read_bytes()
            result = migrate_database(str(source), str(destination))
            self.assertEqual(result["source_schema_version"], 7)
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(source.read_bytes(), original)
            with closing(sqlite3.connect(destination)) as conn:
                self.assertTrue(database_schema_ready(conn))
                row = conn.execute(
                    "SELECT amount, item, voided_at, void_reason, voided_by FROM entry WHERE id=?",
                    (entry_id,)).fetchone()
                self.assertEqual(row, (12.5, "马蹄粉", None, None, None))
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM entry_void_audit").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
