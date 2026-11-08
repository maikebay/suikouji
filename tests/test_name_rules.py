"""Server-owned name → category rules. A fresh agent needs no chat history."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from engine.agent_service import AgentError, AgentService, Principal
from engine.custom_fields import CustomFieldStore
from engine.deployment import migrate_database
from engine.name_rules import NameRuleStore
from engine.store import SCHEMA_VERSION, SqliteStore, database_schema_ready
from tests import test_agent_http_contract as http_contract


P = Principal("agent-a", "owner")
OTHER = Principal("agent-b", "owner")
RECEIVED = "2026-11-08T09:00:00+08:00"
TEXT = "老唐 360"
OPTIONS = [
    {"option_id": "opt_bill_revenue", "display_name": "营业额"},
    {"option_id": "opt_bill_ingredients", "display_name": "食材"},
    {"option_id": "opt_bill_rent", "display_name": "房租"},
    {"option_id": "opt_bill_wages", "display_name": "工资"},
    {"option_id": "opt_bill_utilities", "display_name": "水电"},
    {"option_id": "opt_bill_commission", "display_name": "佣金"},
    {"option_id": "opt_bill_packaging", "display_name": "包装"},
    {"option_id": "opt_bill_promotion", "display_name": "推广费"},
    {"option_id": "opt_bill_repair", "display_name": "维修费"},
    {"option_id": "opt_bill_tax", "display_name": "税费"},
    {"option_id": "opt_bill_other", "display_name": "其他"},
]


class NameRuleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "rules.db"
        self.store = SqliteStore(str(self.db))

        def _close():
            try:
                self.store.close()
            except sqlite3.ProgrammingError:
                pass

        self.addCleanup(_close)
        self.store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
        self.service = AgentService(self.store)
        created = self.service.maintain_custom_fields(P, "shop", "create-bill-field", "create", {"spec": {
            "key": "cf_bill_category", "display_name": "账单分类", "value_type": "enum",
            "scopes": [{"record_type": "entry"}], "options": OPTIONS,
        }})
        self.field_id = created["field"]["field_id"]
        self.fields_version = created["custom_fields_version"]
        self.confirmed = self.service.upsert_name_rule(P, "shop", "rule-laotang", {
            "scope": "confirmed", "item": "老唐", "type": "expense", "category": "人工",
            "bill_category": "opt_bill_wages", "raw_text": "老唐以后记人工",
        })

    def capture(self, request_id, principal=P, item="老唐", text=TEXT, **entry):
        row = {
            "date": "2026-11-08", "item": item, "amount": 360,
            "amount_basis": "gross", "granularity": "transaction",
            "assertion": "actual", "source_quote": text,
        }
        row.update(entry)
        structured = {"entries": [row], "sales": [], "observations": [], "leftovers": []}
        return self.service.capture_text(
            principal, "shop", text, request_id, received_at=RECEIVED, structured=structured)

    def bill_of(self, entry_id, store=None):
        values = CustomFieldStore(store or self.store).values_for("shop", "entry", entry_id)
        return values[0]["value"] if values else None

    def test_schema_v9_is_ready(self):
        self.assertEqual(SCHEMA_VERSION, 9)
        self.assertTrue(database_schema_ready(self.store.conn))
        self.assertEqual(self.confirmed["rules_version"], 1)
        listed = self.service.list_name_rules(P, "shop")
        self.assertEqual(listed["rules_version"], 1)
        self.assertEqual(listed["custom_fields_version"], self.fields_version)
        self.assertIn("rules_version", listed)
        self.assertIn("custom_fields_version", listed)
        self.assertEqual(self.confirmed["rule"]["bill_category"], "opt_bill_wages")
        self.assertEqual(self.confirmed["audit"]["actor"], "agent-a")
        self.assertEqual(self.confirmed["audit"]["request_id"], "rule-laotang")
        self.assertIsNone(self.confirmed["audit"]["before"])

    def test_confirmed_rule_captures_without_category_and_without_a_question(self):
        saved = self.capture("laotang-1", item=" 老唐 ")
        entry = saved["entries"][0]
        self.assertEqual(saved["status"], "parsed")
        self.assertEqual(saved["parsed"]["leftovers"], [])
        self.assertEqual((entry["type"], entry["category"], entry["item"], entry["amount"]),
                         ("expense", "人工", "老唐", 360))
        decision = saved["classification"]["entries"][0]
        self.assertEqual(decision["source"], "confirmed_rule")
        self.assertEqual(decision["rule_id"], self.confirmed["rule"]["rule_id"])
        self.assertEqual(decision["bill_category"], "opt_bill_wages")
        self.assertFalse(decision["overrode_rule"])
        self.assertEqual(self.bill_of(entry["id"]), "opt_bill_wages")
        self.assertEqual(self.service.list_name_rules(OTHER, "shop")["rules_version"], 1)

    def test_explicit_category_overrides_one_entry_and_leaves_the_rule(self):
        saved = self.capture("override-1", type="expense", category="包装耗材")
        entry = saved["entries"][0]
        self.assertEqual((entry["type"], entry["category"]), ("expense", "包装耗材"))
        self.assertIsNone(self.bill_of(entry["id"]))
        decision = saved["classification"]["entries"][0]
        self.assertEqual(decision["source"], "explicit")
        self.assertTrue(decision["overrode_rule"])
        listed = self.service.list_name_rules(P, "shop")
        self.assertEqual(listed["rules_version"], 1)
        self.assertEqual(listed["rules"][0]["category"], "人工")

    def test_one_off_correction_changes_only_that_row(self):
        first = self.capture("row-a")
        second = self.capture("row-b")
        target = "entry:" + first["entries"][0]["id"]
        corrected = self.service.review(
            P, "shop", "fix-once", target,
            {"type": "expense", "category": "推广", "bill_category": "opt_bill_promotion"},
            "以后老唐都记推广",
        )
        self.assertFalse(corrected["duplicate"])
        self.assertEqual(corrected["changed"]["category"], "推广")
        self.assertEqual(corrected["changed"]["bill_category"], "opt_bill_promotion")
        self.assertEqual(self.store.get_entry(first["entries"][0]["id"])["category"], "推广")
        self.assertEqual(self.bill_of(first["entries"][0]["id"]), "opt_bill_promotion")
        self.assertEqual(self.store.get_entry(second["entries"][0]["id"])["category"], "人工")
        self.assertEqual(self.bill_of(second["entries"][0]["id"]), "opt_bill_wages")
        self.assertEqual(self.service.list_name_rules(P, "shop")["rules"][0]["category"], "人工")
        self.assertEqual(self.service.list_name_rules(P, "shop")["rules_version"], 1)

    def test_long_term_change_bumps_version_and_keeps_old_entries(self):
        first = self.capture("before-change")
        changed = self.service.review(
            P, "shop", "fix-forever", "entry:" + first["entries"][0]["id"],
            {"type": "expense", "category": "推广", "bill_category": "opt_bill_promotion"},
            "这个名称以后记推广", scope="rule",
        )
        self.assertEqual(changed["scope"], "rule")
        self.assertFalse(changed["entry_changed"])
        self.assertEqual(changed["rules_version"], 2)
        self.assertEqual(self.store.get_entry(first["entries"][0]["id"])["category"], "人工")
        self.assertEqual(self.bill_of(first["entries"][0]["id"]), "opt_bill_wages")
        later = self.capture("after-change")
        self.assertEqual(later["entries"][0]["category"], "推广")
        self.assertEqual(self.bill_of(later["entries"][0]["id"]), "opt_bill_promotion")
        self.assertEqual(later["classification"]["rules_version"], 2)
        self.assertEqual(self.service.list_name_rules(P, "shop")["custom_fields_version"],
                         self.fields_version)

    def test_ambiguous_candidate_does_not_apply_or_block_capture(self):
        original = self.capture("keep-me")
        candidate = self.service.review(
            P, "shop", "maybe", "entry:" + original["entries"][0]["id"],
            {}, "听不清是只改这一笔还是以后都改", scope="candidate",
        )
        self.assertEqual(candidate["scope"], "candidate")
        self.assertFalse(candidate["entry_changed"])
        self.assertEqual(candidate["rule"]["state"], "candidate")
        self.assertEqual(self.store.get_entry(original["entries"][0]["id"])["category"], "人工")
        again = self.capture("still-wages")
        self.assertEqual(again["classification"]["entries"][0]["source"], "confirmed_rule")
        self.assertEqual(again["entries"][0]["category"], "人工")
        newbie = self.capture("xiaowang", item="小王", text="小王 80", amount=80,
                              type="expense", category="其他支出", bill_category="opt_bill_other")
        self.assertEqual(newbie["status"], "parsed")
        self.assertEqual(newbie["entries"][0]["category"], "其他支出")
        with self.assertRaises(AgentError) as caught:
            self.capture("xiaowang-bare", item="小王", text="小王 80", amount=80)
        self.assertEqual(caught.exception.code, "classification_required")
        self.assertEqual(caught.exception.status, 400)

    def test_chinese_name_and_invalid_option_are_client_errors(self):
        spec = {"key": "cup", "display_name": "杯型", "value_type": "enum",
                "scopes": [{"record_type": "entry"}],
                "options": [{"display_name": "大杯"}, {"display_name": "小杯"}]}
        created = self.service.maintain_custom_fields(P, "shop", "create-cup", "create", {"spec": spec})
        cup_id = created["field"]["field_id"]
        with self.assertRaises(AgentError) as chinese:
            self.capture("chinese-cup", custom_fields=[{"field_id": cup_id, "value": "大杯"}])
        self.assertEqual(chinese.exception.status, 400)
        self.assertEqual(chinese.exception.code, "invalid_option")
        self.assertEqual(self.service.entries(P, "shop"), [])
        with self.assertRaises(AgentError) as missing:
            self.capture("missing-cup", custom_fields=[{"field_id": cup_id, "value": "not-an-option"}])
        self.assertEqual((missing.exception.status, missing.exception.code), (400, "invalid_option"))
        with self.assertRaises(AgentError) as named:
            self.service.upsert_name_rule(P, "shop", "bad-name", {
                "scope": "confirmed", "item": "滤网", "type": "purchase", "category": "食材成本",
                "bill_category": "工资",
            })
        self.assertEqual(named.exception.code, "invalid_option")
        with self.assertRaises(AgentError) as illegal:
            self.service.upsert_name_rule(P, "shop", "bad-pair", {
                "scope": "confirmed", "item": "滤网", "type": "purchase", "category": "食材成本",
                "bill_category": "opt_bill_wages",
            })
        self.assertEqual(illegal.exception.code, "illegal_classification")
        self.assertIsNone(next((rule for rule in self.service.list_name_rules(P, "shop")["rules"]
                                if rule["item"] == "滤网"), None))

    def test_rules_are_shared_across_agents_and_a_reopened_database(self):
        path = self.store.path
        self.store.close()
        reopened = SqliteStore(path)
        self.addCleanup(reopened.close)
        service = AgentService(reopened)
        saved = service.capture_text(OTHER, "shop", TEXT, "from-other-agent", received_at=RECEIVED,
                                     structured={"entries": [{
                                         "date": "2026-11-08", "item": "老唐", "amount": 360,
                                         "amount_basis": "gross", "granularity": "transaction",
                                         "assertion": "actual", "source_quote": TEXT,
                                     }], "sales": [], "observations": [], "leftovers": []})
        self.assertEqual(saved["entries"][0]["category"], "人工")
        self.assertEqual(NameRuleStore(reopened).rules_version("shop"), 1)
        self.assertEqual(CustomFieldStore(reopened).values_for(
            "shop", "entry", saved["entries"][0]["id"])[0]["value"], "opt_bill_wages")

    def test_duplicate_request_id_does_not_create_another_rule_or_entry(self):
        replay = self.service.upsert_name_rule(P, "shop", "rule-laotang", {
            "scope": "confirmed", "item": "老唐", "type": "expense", "category": "人工",
            "bill_category": "opt_bill_wages", "raw_text": "老唐以后记人工",
        })
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["rules_version"], 1)
        self.assertEqual(len(self.service.list_name_rules(P, "shop")["rules"]), 1)
        same = self.service.upsert_name_rule(P, "shop", "rule-laotang-again", {
            "scope": "confirmed", "item": "老唐", "type": "expense", "category": "人工",
            "bill_category": "opt_bill_wages",
        })
        self.assertTrue(same["unchanged"])
        self.assertEqual(same["rules_version"], 1)
        saved = self.capture("same-capture")
        again = self.capture("same-capture")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["entries"][0]["id"], saved["entries"][0]["id"])
        self.assertEqual(len(self.service.entries(P, "shop")), 1)

    def test_stale_rules_version_is_not_applied_under_a_concurrent_change(self):
        holder = sqlite3.connect(self.db, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            """UPDATE name_rule
               SET category=?, bill_category=?, revision=revision+1, updated_at=updated_at
               WHERE shop_id=? AND item_identity=? AND state='confirmed'""",
            ("推广", "opt_bill_promotion", "shop", "老唐"))
        holder.execute(
            "UPDATE name_rule_config SET version=version+1 WHERE shop_id=?", ("shop",))
        outcome = {}

        def run():
            store = SqliteStore(str(self.db))
            try:
                service = AgentService(store)
                try:
                    outcome["result"] = service.capture_text(
                        P, "shop", TEXT, "racing-capture", received_at=RECEIVED,
                        rules_version=1, structured={"entries": [{
                            "date": "2026-11-08", "item": "老唐", "amount": 360,
                            "amount_basis": "gross", "granularity": "transaction",
                            "assertion": "actual", "source_quote": TEXT,
                        }], "sales": [], "observations": [], "leftovers": []})
                except AgentError as exc:
                    outcome["error"] = exc
            finally:
                store.close()

        thread = threading.Thread(target=run)
        thread.start()
        holder.commit()
        holder.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["error"].code, "rules_version_conflict")
        self.assertEqual(outcome["error"].status, 409)
        self.assertEqual(self.service.entries(P, "shop"), [])
        retried = self.capture("racing-capture")
        self.assertEqual(retried["entries"][0]["category"], "推广")
        self.assertEqual(retried["classification"]["rules_version"], 2)
        self.assertEqual(self.bill_of(retried["entries"][0]["id"]), "opt_bill_promotion")
        self.assertEqual(len(self.service.entries(P, "shop")), 1)

    def test_disabled_rule_is_kept_and_not_applied(self):
        disabled = self.service.upsert_name_rule(P, "shop", "disable-laotang", {
            "scope": "confirmed", "item": "老唐", "enabled": False,
        })
        self.assertFalse(disabled["rule"]["enabled"])
        self.assertEqual(disabled["rules_version"], 2)
        with self.assertRaises(AgentError) as caught:
            self.capture("disabled-capture")
        self.assertEqual(caught.exception.code, "classification_required")
        self.assertEqual(len(self.service.list_name_rules(P, "shop")["rules"]), 1)

    def test_v8_migration_keeps_entries_and_starts_with_empty_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "v8.db", root / "v9.db"
            with closing(SqliteStore(str(source))) as store:
                store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
                entry_id = store.add_entries([{
                    "shop_id": "shop", "date": "2026-11-01", "type": "expense",
                    "category": "人工", "amount": 360, "item": "老唐",
                }])[0]
                for name in ("name_rule_audit", "name_rule", "name_rule_config"):
                    store.conn.execute(f"DROP TABLE IF EXISTS {name}")
                store.conn.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
                store.conn.commit()
            with closing(sqlite3.connect(source)) as conn:
                self.assertTrue(database_schema_ready(conn, expected_version=8))
            original = source.read_bytes()
            result = migrate_database(str(source), str(destination))
            self.assertEqual(result["source_schema_version"], 8)
            self.assertEqual(result["schema_version"], 9)
            self.assertEqual(source.read_bytes(), original)
            with closing(sqlite3.connect(destination)) as conn:
                self.assertTrue(database_schema_ready(conn))
                self.assertEqual(conn.execute(
                    "SELECT amount, item, category FROM entry WHERE id=?", (entry_id,)).fetchone(),
                    (360, "老唐", "人工"))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM name_rule").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM name_rule_audit").fetchone()[0], 0)


class NameRuleHttpTests(http_contract.AgentHttpContractTests):
    def test_capabilities_tell_a_fresh_agent_how_rules_work(self):
        status, _, caps = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        policy = caps["agent_policy"]["input_preparation"]["classification"]
        self.assertTrue(policy["server_owned"])
        self.assertFalse(policy["history_or_ai_in_mvp"])
        self.assertFalse(policy["keyword_matching"])
        self.assertEqual(policy["bill_field_key"], "cf_bill_category")
        self.assertIn("opt_bill_wages", {item["bill_category"] for item in policy["legal_pairs"]})
        self.assertEqual(caps["operations"]["upsert_name_rule"]["path"], "/v1/name-rules")
        self.assertEqual(caps["operations"]["list_name_rules"]["method"], "GET")
        entry_schema = caps["agent_policy"]["input_preparation"]["structured_schema"]["properties"]["entries"]["items"]
        self.assertNotIn("type", entry_schema["required"])
        self.assertNotIn("category", entry_schema["required"])
        rule = next(item for item in caps["capabilities"] if item["id"] == "classification.rules")
        self.assertEqual(rule["status"], "available")

    def test_chinese_enum_capture_is_http_400(self):
        status, _, created = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "http-cup", "action": "create",
            "change": {"spec": {"key": "cup", "display_name": "杯型", "value_type": "enum",
                                "scopes": [{"record_type": "entry"}],
                                "options": [{"display_name": "大杯"}]}},
        })
        self.assertEqual(status, 200)
        field_id = created["field"]["field_id"]
        status, _, body = self.request("POST", "/v1/captures", {
            "workspace_id": "shop", "request_id": "http-chinese",
            "text": "今天买大杯",
            "structured": {"entries": [{
                "date": "2026-11-08", "type": "expense", "category": "其他支出",
                "amount": 12, "amount_basis": "gross", "granularity": "transaction",
                "assertion": "actual", "source_quote": "今天买大杯",
                "custom_fields": [{"field_id": field_id, "value": "大杯"}],
            }], "sales": [], "observations": [], "leftovers": []},
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_option")
        self.assertNotEqual(status, 500)


if __name__ == "__main__":
    unittest.main()
