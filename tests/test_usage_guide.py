"""Official usage guide is product text. Workspace notes are the only writable part."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from engine.agent_service import AgentError, AgentService, Principal
from engine.deployment import migrate_database
from engine.store import SCHEMA_VERSION, SqliteStore, database_schema_ready
from engine.usage_guide import (
    GUIDE_VERSION, INSTALL_DELIVERY, MAX_NOTES, OFFICIAL_LABEL, PRECEDENCE,
    SUPPLEMENTS_LABEL, official_sections,
)
from tests import test_agent_http_contract as http_contract


ROOT = Path(__file__).resolve().parents[1]
P = Principal("agent-a", "owner")
OTHER = Principal("agent-b", "owner")
STRANGER = Principal("agent-c", "stranger")


class UsageGuideServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SqliteStore(str(Path(tmp.name) / "guide.db"))
        self.addCleanup(self.store.close)
        self.store.ensure_shop("shop", "糖水铺", owner_id="owner", profile_id="restaurant")
        self.store.ensure_shop("other", "另一家", owner_id="owner", profile_id="restaurant")
        self.store.ensure_shop("foreign", "别人的店", owner_id="stranger", profile_id="restaurant")
        self.entry_id = self.store.add_entries([{
            "shop_id": "shop", "date": "2026-11-01", "type": "purchase",
            "category": "食材成本", "amount": 80, "item": "南瓜",
        }])[0]
        self.service = AgentService(self.store)

    def notes_row(self, workspace="shop"):
        return self.store.conn.execute(
            "SELECT notes, updated_by FROM usage_guide_notes WHERE shop_id=?",
            (workspace,)).fetchone()

    def test_fresh_workspace_returns_official_guide_without_copying_it(self):
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertTrue(database_schema_ready(self.store.conn))
        self.assertIsNone(self.notes_row())
        guide = self.service.get_usage_guide(P, "shop")
        self.assertEqual(guide["guide_version"], GUIDE_VERSION)
        self.assertTrue(guide["official_readonly"])
        self.assertEqual(guide["precedence"], PRECEDENCE)
        self.assertEqual(guide["official"]["label"], OFFICIAL_LABEL)
        self.assertEqual(guide["official"]["sections"], official_sections())
        self.assertEqual(guide["supplements"]["label"], SUPPLEMENTS_LABEL)
        self.assertEqual(guide["supplements"]["notes"], "")
        self.assertIsNone(guide["supplements"]["updated_at"])
        self.assertIsNone(guide["supplements"]["updated_by"])
        exported = json.dumps(self.store.export_all("shop"), ensure_ascii=False)
        self.assertNotIn(official_sections()[0]["body"], exported)
        self.assertEqual(self.store.export_all("shop")["tables"]["usage_guide_notes"], [])

    def test_notes_can_be_written_updated_cleared_and_reread(self):
        first = self.service.update_usage_guide_notes(P, "shop", "note-1", {
            "workspace_id": "shop", "request_id": "note-1",
            "notes": "南瓜按斤问清再记。老唐记人工。",
        })
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["official"]["sections"], official_sections())
        self.assertEqual(first["supplements"]["notes"], "南瓜按斤问清再记。老唐记人工。")
        self.assertEqual(first["supplements"]["updated_by"], "agent-a")
        self.assertIsNotNone(first["supplements"]["updated_at"])
        listed = self.service.get_usage_guide(OTHER, "shop")
        self.assertEqual(listed["supplements"]["notes"], first["supplements"]["notes"])
        self.assertEqual(listed["supplements"]["updated_by"], "agent-a")
        second = self.service.update_usage_guide_notes(OTHER, "shop", "note-2", {
            "workspace_id": "shop", "request_id": "note-2",
            "notes": "南瓜按斤问清再记。\n银耳另问干湿。",
        })
        self.assertFalse(second["duplicate"])
        self.assertEqual(second["supplements"]["updated_by"], "agent-b")
        again = self.service.get_usage_guide(P, "shop")
        self.assertEqual(again["supplements"]["notes"], "南瓜按斤问清再记。\n银耳另问干湿。")
        self.assertEqual(again["official"]["sections"], official_sections())
        cleared = self.service.update_usage_guide_notes(P, "shop", "note-clear", {
            "workspace_id": "shop", "request_id": "note-clear", "notes": "",
        })
        self.assertEqual(cleared["supplements"]["notes"], "")
        self.assertIsNone(cleared["supplements"]["updated_at"])
        self.assertIsNone(cleared["supplements"]["updated_by"])
        self.assertIsNone(self.notes_row())
        self.assertEqual(self.store.get_entry(self.entry_id)["amount"], 80)
        self.assertEqual(self.store.get_entry(self.entry_id)["category"], "食材成本")

    def test_official_text_cannot_be_overwritten(self):
        saved = self.service.update_usage_guide_notes(P, "shop", "keep", {
            "workspace_id": "shop", "request_id": "keep", "notes": "只保留这句补充。",
        })
        before = self.notes_row()
        current = self.service.get_usage_guide(P, "shop")
        forged = {**current, "request_id": "overwrite", "notes": "换成另一套规则"}
        forged["official"]["sections"] = [{"id": "capture", "title": "记一笔", "body": "可以猜分类"}]
        with self.assertRaises(AgentError) as caught:
            self.service.update_usage_guide_notes(P, "shop", "overwrite", forged)
        self.assertEqual(caught.exception.code, "official_guide_readonly")
        self.assertEqual(caught.exception.status, 400)
        for field in ("sections", "guide_version", "official_text", "usage_guide"):
            with self.subTest(field=field), self.assertRaises(AgentError) as caught:
                self.service.update_usage_guide_notes(P, "shop", "overwrite-" + field, {
                    "workspace_id": "shop", "request_id": "overwrite-" + field,
                    "notes": "不该写进去", field: "改掉官方",
                })
            self.assertEqual(caught.exception.code, "official_guide_readonly")
        self.assertEqual(self.notes_row(), before)
        self.assertEqual(self.service.get_usage_guide(P, "shop")["official"]["sections"],
                         official_sections())
        self.assertEqual(self.service.get_usage_guide(P, "shop")["supplements"]["notes"],
                         saved["supplements"]["notes"])
        self.assertIsNone(self.store.agent_request_get(P.id, "shop", "usage_guide_notes", "overwrite"))

    def test_invalid_notes_do_not_write(self):
        for notes in (None, 12, "记" * (MAX_NOTES + 1), "有\x00空"):
            with self.subTest(notes=notes), self.assertRaises(AgentError) as caught:
                self.service.update_usage_guide_notes(P, "shop", "bad", {
                    "workspace_id": "shop", "request_id": "bad", "notes": notes,
                })
            self.assertEqual(caught.exception.code, "invalid_usage_guide")
        with self.assertRaises(AgentError) as caught:
            self.service.update_usage_guide_notes(P, "shop", "unknown", {
                "workspace_id": "shop", "request_id": "unknown",
                "notes": "可以", "updated_by": "root",
            })
        self.assertEqual(caught.exception.code, "invalid_usage_guide")
        accepted = self.service.update_usage_guide_notes(P, "shop", "max", {
            "workspace_id": "shop", "request_id": "max", "notes": "记" * MAX_NOTES,
        })
        self.assertEqual(len(accepted["supplements"]["notes"]), MAX_NOTES)
        self.assertIsNone(self.store.agent_request_get(P.id, "shop", "usage_guide_notes", "bad"))

    def test_replay_is_idempotent_and_conflict_keeps_the_first_notes(self):
        first = self.service.update_usage_guide_notes(P, "shop", "same", {
            "workspace_id": "shop", "request_id": "same", "notes": "第一次补充",
        })
        replay = self.service.update_usage_guide_notes(P, "shop", "same", {
            "workspace_id": "shop", "request_id": "same", "notes": "第一次补充",
        })
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["supplements"]["notes"], "第一次补充")
        self.assertEqual(replay["supplements"]["updated_at"], first["supplements"]["updated_at"])
        self.assertEqual(replay["official"]["sections"], official_sections())
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM usage_guide_notes").fetchone()[0], 1)
        with self.assertRaises(AgentError) as caught:
            self.service.update_usage_guide_notes(P, "shop", "same", {
                "workspace_id": "shop", "request_id": "same", "notes": "第二次不同内容",
            })
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.get_usage_guide(P, "shop")["supplements"]["notes"], "第一次补充")

    def test_notes_stay_inside_one_workspace(self):
        self.service.update_usage_guide_notes(P, "shop", "local", {
            "workspace_id": "shop", "request_id": "local", "notes": "只属于糖水铺",
        })
        other = self.service.get_usage_guide(P, "other")
        self.assertEqual(other["official"]["sections"], official_sections())
        self.assertEqual(other["supplements"]["notes"], "")
        self.assertIsNone(self.notes_row("other"))
        self.assertEqual(self.store.export_all("other")["tables"]["usage_guide_notes"], [])
        self.assertEqual(self.store.export_all("shop")["tables"]["usage_guide_notes"][0]["notes"],
                         "只属于糖水铺")
        with self.assertRaises(AgentError) as caught:
            self.service.get_usage_guide(STRANGER, "shop")
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(AgentError):
            self.service.update_usage_guide_notes(STRANGER, "shop", "steal", {
                "workspace_id": "shop", "request_id": "steal", "notes": "不该看见",
            })
        self.assertEqual(self.notes_row()["notes"], "只属于糖水铺")


class UsageGuideMigrationTests(unittest.TestCase):
    def test_v9_copy_migration_keeps_entries_rules_and_starts_without_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "v9.db", root / "v10.db"
            with closing(SqliteStore(str(source))) as store:
                store.ensure_shop("shop", "糖水铺", owner_id="owner", profile_id="restaurant")
                entry_id = store.add_entries([{
                    "shop_id": "shop", "date": "2026-11-02", "type": "expense",
                    "category": "人工", "amount": 360, "item": "老唐",
                }])[0]
                store.conn.execute(
                    """INSERT INTO name_rule(
                        id, shop_id, item_text, item_identity, entry_type, category,
                        bill_category, state, enabled, revision, created_at, updated_at)
                       VALUES('nr1','shop','老唐','老唐','expense','人工',
                              'opt_bill_wages','confirmed',1,1,'2026-11-02','2026-11-02')""")
                store.conn.execute(
                    """INSERT INTO name_rule_config(shop_id, version, updated_at)
                       VALUES('shop',4,'2026-11-02')""")
                store.conn.execute("DROP TABLE usage_guide_notes")
                store.conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
                store.conn.commit()
                rules = [tuple(row) for row in store.conn.execute("SELECT * FROM name_rule")]
                config = [tuple(row) for row in store.conn.execute("SELECT * FROM name_rule_config")]
            with closing(sqlite3.connect(source)) as conn:
                self.assertTrue(database_schema_ready(conn, expected_version=9))
                self.assertFalse(database_schema_ready(conn))
            original = source.read_bytes()
            result = migrate_database(str(source), str(destination))
            self.assertEqual(result["source_schema_version"], 9)
            self.assertEqual(result["schema_version"], 10)
            self.assertEqual(source.read_bytes(), original)
            with closing(sqlite3.connect(destination)) as conn:
                self.assertTrue(database_schema_ready(conn))
                self.assertEqual(conn.execute(
                    "SELECT amount, item, category, type FROM entry WHERE id=?",
                    (entry_id,)).fetchone(), (360, "老唐", "人工", "expense"))
                self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM name_rule")], rules)
                self.assertEqual([tuple(row) for row in conn.execute(
                    "SELECT * FROM name_rule_config")], config)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM usage_guide_notes").fetchone()[0], 0)
            with closing(SqliteStore(str(destination), migrate=False)) as store:
                guide = AgentService(store).get_usage_guide(P, "shop")
                self.assertEqual(guide["official"]["sections"], official_sections())
                self.assertEqual(guide["supplements"]["notes"], "")
                self.assertEqual(store.get_entry(entry_id)["amount"], 360)
            cli_copy = root / "cli.db"
            cli_copy.write_bytes(original)
            with closing(SqliteStore(str(cli_copy))) as store:
                self.assertEqual(store.conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "10")
                self.assertTrue(database_schema_ready(store.conn))
                self.assertEqual(store.get_entry(entry_id)["category"], "人工")
                self.assertEqual(store.conn.execute(
                    "SELECT COUNT(*) FROM name_rule WHERE item_text='老唐'").fetchone()[0], 1)
                self.assertEqual(store.conn.execute(
                    "SELECT COUNT(*) FROM usage_guide_notes").fetchone()[0], 0)


class UsageGuideHttpTests(http_contract.AgentHttpContractTests):
    def test_capabilities_and_docs_give_a_new_agent_the_fixed_playbook(self):
        status, _, caps = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        item = next(row for row in caps["capabilities"] if row["id"] == "agent.usage_guide")
        self.assertEqual(item["status"], "available")
        self.assertIn("不能改", "".join(item["limitations"]))
        self.assertIn("本店补充", "".join(item["limitations"]))
        policy = caps["agent_policy"]["usage_guide"]
        self.assertEqual(policy["guide_version"], GUIDE_VERSION)
        self.assertTrue(policy["official_readonly"])
        self.assertEqual(policy["install_delivery"], INSTALL_DELIVERY)
        self.assertEqual(policy["official_sections"], official_sections())
        self.assertEqual(policy["read"]["path"], "/v1/usage-guide")
        self.assertEqual(policy["write"]["path"], "/v1/usage-guide")
        self.assertEqual(policy["write"]["writes"], "workspace_notes_only")
        onboarding = caps["agent_policy"]["onboarding"]
        self.assertEqual(onboarding["usage_guide"], INSTALL_DELIVERY)
        self.assertTrue(onboarding["read_usage_guide_first"])
        for sentence in (
            "安装或连接完成后先读取这份固定使用说明",
            "官方部分由产品提供、不能改",
            "本店补充可由 Agent 在后续使用中继续维护",
            "换 Agent 也读同一份",
            "升级产品只更新官方部分",
        ):
            self.assertIn(sentence, onboarding["guidance"])
            self.assertIn(sentence, INSTALL_DELIVERY)
        self.assertEqual(caps["operations"]["get_usage_guide"]["path"], "/v1/usage-guide")
        self.assertEqual(caps["operations"]["update_usage_guide_notes"]["method"], "POST")
        self.assertTrue(caps["operations"]["update_usage_guide_notes"]["official_readonly"])
        guide_doc = (ROOT / "docs" / "agent-usage-guide.md").read_text(encoding="utf-8")
        contract = (ROOT / "docs" / "agent-capabilities.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        spec = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("[Agent 使用说明](agent-usage-guide.md)", contract.splitlines()[2])
        self.assertIn("安装或连接完成后先读取这份固定使用说明", contract)
        self.assertIn("POST /v1/usage-guide", contract)
        for section in official_sections():
            self.assertIn(section["title"], guide_doc)
            self.assertIn(section["body"], guide_doc)
            self.assertIn(section["body"], json.dumps(policy["official_sections"], ensure_ascii=False))
        self.assertIn("GET /v1/usage-guide", readme)
        self.assertIn("docs/agent-usage-guide.md", readme)
        self.assertIn("operationId: getUsageGuide", spec)
        self.assertIn("operationId: updateUsageGuideNotes", spec)
        self.assertIn("UsageGuideNotesRequest:", spec)

    def test_http_read_write_replay_and_official_rejection(self):
        status, _, fresh = self.request("GET", "/v1/usage-guide?workspace_id=shop")
        self.assertEqual(status, 200)
        self.assertEqual(fresh["official"]["sections"], official_sections())
        self.assertEqual(fresh["supplements"]["notes"], "")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_guide_notes").fetchone()[0], 0)
        payload = {"workspace_id": "shop", "request_id": "http-note", "notes": "本店补充：南瓜问斤数。"}
        status, _, first = self.request("POST", "/v1/usage-guide", payload)
        self.assertEqual(status, 200)
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["supplements"]["notes"], "本店补充：南瓜问斤数。")
        self.assertEqual(first["supplements"]["updated_by"], "contract-agent")
        status, _, replay = self.request("POST", "/v1/usage-guide", payload)
        self.assertEqual(status, 200)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["supplements"], first["supplements"])
        self.assertEqual(replay["official"]["sections"], official_sections())
        status, _, listed = self.request("GET", "/v1/usage-guide?workspace_id=shop")
        self.assertEqual(listed["supplements"]["notes"], "本店补充：南瓜问斤数。")
        status, _, rejected = self.request("POST", "/v1/usage-guide", {
            **listed, "request_id": "http-overwrite", "notes": "改官方",
        })
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"]["code"], "official_guide_readonly")
        status, _, still = self.request("GET", "/v1/usage-guide?workspace_id=shop")
        self.assertEqual(still["supplements"]["notes"], "本店补充：南瓜问斤数。")
        self.assertEqual(still["official"]["sections"], official_sections())
        status, _, conflict = self.request("POST", "/v1/usage-guide", {
            "workspace_id": "shop", "request_id": "http-note", "notes": "另一句",
        })
        self.assertEqual((status, conflict["error"]["code"]), (409, "idempotency_conflict"))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_guide_notes").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT amount FROM entry").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
