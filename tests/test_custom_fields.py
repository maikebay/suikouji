"""Custom field definitions stay inside one workspace and never rewrite history."""
from __future__ import annotations

import copy
from contextlib import closing
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from engine.agent_service import AgentService, Principal
from engine.custom_fields import (
    MAX_ENABLED_FIELDS,
    MAX_FIELDS,
    CustomFieldError,
    CustomFieldStore,
    canonical_number,
)
from engine.deployment import migrate_database
from engine.store import SCHEMA_VERSION, SqliteStore, database_schema_ready
from tests.test_agent_http_contract import AgentHttpContractTests


CONFIRMATION = {"channel": "local-test", "actor": "owner"}


class CustomFieldDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_shop("a", owner_id="owner-a")
        self.store.ensure_shop("b", owner_id="owner-b")
        self.fields = CustomFieldStore(self.store)

    def spec(self, **changes):
        value = {"key": "supplier", "display_name": "供应商",
                 "value_type": "text",
                 "scopes": [{"record_type": "entry", "entry_types": ["purchase"]}]}
        value.update(changes)
        return value

    def test_workspace_isolation_and_stable_identity(self):
        created = self.fields.create_field("a", self.spec(), actor="owner", confirmation=CONFIRMATION)
        field = created["field"]
        self.assertEqual(created["custom_fields_version"], 1)
        self.assertEqual(self.fields.list_fields("b")["fields"], [])
        with self.assertRaises(CustomFieldError) as caught:
            self.fields.update_field("b", field["field_id"], {"display_name": "越权"},
                                     actor="owner", confirmation=CONFIRMATION)
        self.assertEqual(caught.exception.code, "unknown_field")
        renamed = self.fields.update_field(
            "a", field["field_id"], {"display_name": "供货商"},
            actor="owner", confirmation=CONFIRMATION)["field"]
        self.assertEqual(renamed["field_id"], field["field_id"])
        self.assertEqual(renamed["key"], "supplier")
        self.assertEqual(renamed["revision"], 2)
        self.assertEqual(self.fields.config_version("a"), 2)
        self.assertEqual(self.fields.config_version("b"), 0)

    def test_types_limits_and_disabled_history_remain(self):
        self.assertEqual(canonical_number("200"), "200")
        self.assertEqual(canonical_number("-0.125000"), "-0.125000")
        self.assertEqual(canonical_number("0"), "0")
        for value in (200, True, "1e2", "NaN", "01.2", "0."):
            with self.subTest(value=value), self.assertRaises(CustomFieldError):
                canonical_number(value)
        enum = self.fields.create_field("a", self.spec(
            key="spec", display_name="规格", value_type="enum",
            scopes=[{"record_type": "sale", "product_categories": ["甜品"]}],
            options=[{"display_name": "大杯"}, {"display_name": "小杯"}]),
            actor="owner", confirmation=CONFIRMATION)["field"]
        self.assertEqual([item["display_name"] for item in enum["options"]], ["大杯", "小杯"])
        disabled = self.fields.set_option_enabled(
            "a", enum["field_id"], enum["options"][1]["option_id"], False,
            actor="owner", confirmation=CONFIRMATION)["field"]
        self.assertEqual(disabled["options"][1]["option_id"], enum["options"][1]["option_id"])
        self.assertFalse(disabled["options"][1]["enabled"])
        for index in range(MAX_ENABLED_FIELDS - 1):
            self.fields.create_field("a", self.spec(key=f"field_{index}", display_name=f"字段{index}"),
                                     actor="owner", confirmation=CONFIRMATION)
        with self.assertRaises(CustomFieldError) as caught:
            self.fields.create_field("a", self.spec(key="overflow", display_name="超限"),
                                     actor="owner", confirmation=CONFIRMATION)
        self.assertEqual(caught.exception.code, "limit_exceeded")
        self.fields.update_field("a", enum["field_id"], {"enabled": False},
                                 actor="owner", confirmation=CONFIRMATION)
        replacement = self.fields.create_field(
            "a", self.spec(key="replacement", display_name="替代字段"),
            actor="owner", confirmation=CONFIRMATION)["field"]
        listed = self.fields.list_fields("a")["fields"]
        self.assertEqual(len(listed), MAX_ENABLED_FIELDS + 1)
        self.assertLessEqual(sum(field["enabled"] for field in listed), MAX_ENABLED_FIELDS)
        self.assertIn(enum["field_id"], {field["field_id"] for field in listed})
        self.assertNotEqual(replacement["field_id"], enum["field_id"])
        self.assertLessEqual(len(listed), MAX_FIELDS)

    def test_explicit_invalid_values_are_rejected_without_guessing(self):
        field = self.fields.create_field("a", self.spec(required=True),
                                         actor="owner", confirmation=CONFIRMATION)["field"]
        version = self.fields.config_version("a")
        gaps = self.fields.missing_required(
            "a", "entry", {"entry_types": "purchase", "categories": "原料"}, [])
        self.assertEqual(gaps, [{"field_id": field["field_id"], "display_name": "供应商", "status": "missing"}])
        self.assertEqual(self.fields.missing_required(
            "a", "entry", {"entry_types": "expense"}, []), [])
        accepted = self.fields.validate_assignment(
            "a", "entry", {"entry_types": "purchase"},
            [{"field_id": field["field_id"], "value": "阿明配送",
              "source_quote": "阿明配送"}], config_version=version)
        self.assertEqual(accepted[0]["text_value"], "阿明配送")
        for item in ({"field_id": "missing", "value": "甲"},
                     {"field_id": field["field_id"], "value": ""},
                     {"field_id": field["field_id"], "value": "甲"},
                     {"field_id": field["field_id"], "value": "甲"}):
            with self.subTest(item=item), self.assertRaises(CustomFieldError):
                self.fields.validate_assignment(
                    "a", "entry", {"entry_types": "purchase"}, [item, item], config_version=version)
        with self.assertRaises(CustomFieldError) as caught:
            self.fields.validate_assignment(
                "a", "entry", {"entry_types": "purchase"},
                [{"field_id": field["field_id"], "value": "阿明"}], config_version=version + 1)
        self.assertEqual(caught.exception.code, "config_version_conflict")

    def test_v6_copy_migration_adds_empty_tables_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "v6.db", root / "v7.db"
            with closing(SqliteStore(str(source))) as store:
                store.conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
                for name in ("custom_field", "custom_field_option", "custom_field_config",
                             "custom_field_audit", "custom_entry_value", "custom_sale_value",
                             "custom_observation_value", "custom_thought_value", "custom_field_gap",
                             "custom_field_suggestion", "custom_field_preview",
                             "custom_field_alias", "custom_import_mapping", "entry_void_audit"):
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
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM custom_field").fetchone()[0], 0)

    def test_filters_paging_and_grouping_keep_each_record_once(self):
        number = self.fields.create_field("a", self.spec(
            key="batch_weight", display_name="批次重量", value_type="number",
            scopes=[{"record_type": "entry", "entry_types": ["purchase"]}]),
            actor="owner", confirmation=CONFIRMATION)["field"]
        label = self.fields.create_field("a", self.spec(
            key="channel_note", display_name="渠道备注", value_type="text",
            scopes=[{"record_type": "entry"}]),
            actor="owner", confirmation=CONFIRMATION)["field"]
        version = self.fields.config_version("a")
        purchase_ids, expense_id = [], None
        for index, amount in enumerate(("9", "10", "100"), start=1):
            entry_id = self.store.add_entries([{
                "shop_id": "a", "date": f"2026-10-{index:02d}", "type": "purchase",
                "category": "食材成本", "amount": int(amount)}])[0]
            purchase_ids.append(entry_id)
            self.fields.write_values(
                "a", "entry", entry_id,
                [{"field_id": number["field_id"], "value": amount},
                 {"field_id": label["field_id"], "value": f"批次{index}"}],
                attributes={"entry_types": "purchase", "categories": "食材成本"},
                config_version=version)
        expense_id = self.store.add_entries([{
            "shop_id": "a", "date": "2026-10-04", "type": "expense",
            "category": "房租", "amount": 800}])[0]
        self.fields.write_values(
            "a", "entry", expense_id, [{"field_id": label["field_id"], "value": "月租"}],
            attributes={"entry_types": "expense", "categories": "房租"}, config_version=version)
        missing = self.store.add_entries([{
            "shop_id": "a", "date": "2026-10-05", "type": "purchase",
            "category": "食材成本", "amount": 20}])[0]
        ranged = self.fields.query_records(
            "a", "entry", filters=[{"field_id": number["field_id"], "op": "between",
                                    "value": ["9", "10"]}])
        self.assertEqual({item["id"] for item in ranged["items"]}, set(purchase_ids[:2]))
        first = self.fields.query_records("a", "entry", page_size=2)
        second = self.fields.query_records("a", "entry", page_size=2, before=first["next_before"])
        third = self.fields.query_records("a", "entry", page_size=2, before=second["next_before"])
        seen = [item["id"] for page in (first, second, third) for item in page["items"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {*purchase_ids, expense_id, missing})
        self.assertIsNone(third["next_before"])
        with self.assertRaises(CustomFieldError) as caught:
            self.fields.query_records("a", "sale", before=first["next_before"])
        self.assertEqual(caught.exception.code, "cursor_stale")
        missing_rows = self.fields.query_records(
            "a", "entry", filters=[{"field_id": number["field_id"], "op": "missing"}])
        inapplicable = self.fields.query_records(
            "a", "entry", filters=[{"field_id": number["field_id"], "op": "not_applicable"}])
        self.assertEqual([item["id"] for item in missing_rows["items"]], [missing])
        self.assertEqual([item["id"] for item in inapplicable["items"]], [expense_id])
        grouped = self.fields.group_records("a", "entry", label["field_id"], metric="purchase")
        self.assertEqual(sum(group["count"] for group in grouped["groups"]), 5)
        self.assertEqual(sum(group["amount"] for group in grouped["groups"]), 139)
        self.assertEqual(next(group["amount"] for group in grouped["groups"]
                              if group["label"] == "未填写"), 20)


class CustomFieldCaptureTests(unittest.TestCase):
    def setUp(self):
        self.http = AgentHttpContractTests()
        self.http.setUp()
        self.addCleanup(self.http.doCleanups)
        self.server = self.http.server
        self.server.manager_key = "fake-manager-token"
        self.server.manager_principal_id = "local-manager"

    def request(self, *args, authorization="Bearer fake-contract-token", **kwargs):
        return self.http.request(*args, authorization=authorization, **kwargs)

    def test_capture_keeps_supplier_and_reports_missing_without_changing_finance(self):
        manager = "Bearer fake-manager-token"
        suggestion = {"workspace_id": "shop", "request_id": "suggest-supplier",
                      "suggestion": {"suggestion_type": "field",
                                     "payload": {"display_name": "供应商"}, "evidence": {"rows": 2}}}
        status, _, suggested = self.request("POST", "/v1/custom-field-suggestions", suggestion)
        self.assertEqual(status, 200)
        self.assertEqual(suggested["suggestion"]["state"], "pending")
        change = {"action": "create", "spec": {
            "key": "supplier", "display_name": "供应商", "value_type": "text", "required": True,
            "scopes": [{"record_type": "entry", "entry_types": ["purchase"]}]}}
        status, _, denied = self.request("POST", "/v1/custom-field-changes/preview",
                                         {"workspace_id": "shop", "change": change})
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"]["code"], "confirmation_forbidden")
        status, _, preview = self.request("POST", "/v1/custom-field-changes/preview",
                                          {"workspace_id": "shop", "change": change}, authorization=manager)
        self.assertEqual(status, 200)
        status, _, committed = self.request(
            "POST", "/v1/custom-field-changes/commit",
            {"workspace_id": "shop", "request_id": "approve-supplier",
             "preview_token": preview["preview_token"]}, authorization=manager)
        self.assertEqual(status, 200)
        field_id = committed["field"]["field_id"]
        version = committed["custom_fields_version"]
        text = "阿明配送送来两箱牛奶，采购260元"
        payload = {"workspace_id": "shop", "request_id": "purchase-with-supplier",
                   "text": text, "custom_fields_version": version,
                   "structured": {"entries": [{
                       "date": "2026-10-28", "type": "purchase", "category": "食材成本",
                       "amount": 260, "amount_basis": "gross", "granularity": "transaction",
                       "assertion": "actual", "source_quote": "采购260元",
                       "custom_fields": [{"field_id": field_id, "value": "阿明配送",
                                          "source_quote": "阿明配送"}]}],
                       "sales": [], "observations": [], "leftovers": [], "thoughts": []}}
        status, _, saved = self.request("POST", "/v1/captures", payload)
        self.assertEqual(status, 200)
        self.assertEqual(saved["entries"][0]["amount"], 260)
        self.assertEqual(saved["custom_fields"]["written"], 1)
        self.assertEqual(saved["custom_fields"]["missing_required"], [])
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            values = CustomFieldStore(store).values_for("shop", "entry", saved["entries"][0]["id"])
        self.assertEqual(values[0]["value"], "阿明配送")
        missing = copy.deepcopy(payload)
        missing["request_id"] = "purchase-missing-supplier"
        missing["structured"]["entries"][0]["custom_fields"] = []
        status, _, incomplete = self.request("POST", "/v1/captures", missing)
        self.assertEqual(status, 200)
        self.assertEqual(incomplete["status"], "parsed")
        self.assertEqual(incomplete["custom_fields"]["missing_required"][0]["display_name"], "供应商")
        status, _, replay = self.request("POST", "/v1/captures", payload)
        self.assertEqual(replay, {**saved, "duplicate": True})

    def test_agent_confirmed_flag_does_not_create_field(self):
        agent = Principal("contract-agent", "owner")
        manager = Principal("local-manager", "owner", "manager")
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            service = AgentService(store)
            suggestion = service.submit_custom_field_suggestion(
                agent, "shop", "same-suggestion", {"suggestion_type": "field",
                                                   "payload": {"display_name": "付款账户"}, "evidence": {}})
            again = service.submit_custom_field_suggestion(
                agent, "shop", "same-suggestion-again",
                {"suggestion_type": "field", "payload": {"display_name": "付款账户"}, "evidence": {}})
            self.assertEqual(again["suggestion"]["suggestion_id"], suggestion["suggestion"]["suggestion_id"])
            with self.assertRaises(Exception) as caught:
                service.commit_custom_field_change(agent, "shop", "agent-confirms", "missing")
            self.assertEqual(caught.exception.code, "confirmation_forbidden")
            preview = service.preview_custom_field_change(manager, "shop", {"action": "create", "spec": {
                "key": "payment_account", "display_name": "付款账户", "value_type": "text",
                "scopes": [{"record_type": "entry"}]}})
            first = service.commit_custom_field_change(
                manager, "shop", "approve-account", preview["preview_token"])
            replay = service.commit_custom_field_change(
                manager, "shop", "approve-account", preview["preview_token"])
            self.assertEqual(replay, {**first, "duplicate": True})
            self.assertEqual(len(service.list_custom_fields(agent, "shop")["fields"]), 1)

    def test_agent_creates_new_record_field_without_user_confirmation(self):
        status, _, created = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "create-supplier", "action": "create",
            "change": {"spec": {"key": "supplier", "display_name": "供应商", "value_type": "text",
                                 "scopes": [{"record_type": "entry", "entry_types": ["purchase"]}]}}})
        self.assertEqual(status, 200)
        field_id = created["field"]["field_id"]
        status, _, replay = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "create-supplier", "action": "create",
            "change": {"spec": {"key": "supplier", "display_name": "供应商", "value_type": "text",
                                 "scopes": [{"record_type": "entry", "entry_types": ["purchase"]}]}}})
        self.assertEqual(replay, {**created, "duplicate": True})
        status, _, conflict = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "create-supplier", "action": "create",
            "change": {"spec": {"key": "vendor", "display_name": "供货商", "value_type": "text",
                                 "scopes": [{"record_type": "entry"}]}}})
        self.assertEqual((status, conflict["error"]["code"]), (409, "idempotency_conflict"))
        status, _, renamed = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "rename-supplier", "action": "update",
            "change": {"field_id": field_id, "changes": {"display_name": "供货商"}}})
        self.assertEqual(renamed["field"]["field_id"], field_id)
        self.assertEqual(renamed["field"]["display_name"], "供货商")
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            store.ensure_shop("other", owner_id="someone-else")
        status, _, forbidden = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "other", "request_id": "cross-workspace", "action": "update",
            "change": {"field_id": field_id, "changes": {"display_name": "越权"}}})
        self.assertEqual(forbidden["error"]["code"], "workspace_forbidden")
        status, _, fields = self.request("GET", "/v1/custom-fields?workspace_id=shop")
        self.assertEqual(len(fields["fields"]), 1)

    def test_query_group_and_export_do_not_duplicate_amounts(self):
        manager = "Bearer fake-manager-token"
        change = {"action": "create", "spec": {
            "key": "activity", "display_name": "活动", "value_type": "text",
            "scopes": [{"record_type": "entry"}, {"record_type": "sale"}]}}
        _, _, preview = self.request("POST", "/v1/custom-field-changes/preview",
                                     {"workspace_id": "shop", "change": change}, authorization=manager)
        _, _, committed = self.request("POST", "/v1/custom-field-changes/commit",
            {"workspace_id": "shop", "request_id": "approve-activity",
             "preview_token": preview["preview_token"]}, authorization=manager)
        field_id = committed["field"]["field_id"]
        version = committed["custom_fields_version"]
        text = "今天营业额1300元，新品活动卖了300元"
        payload = {"workspace_id": "shop", "request_id": "revenue-and-sale", "text": text,
                   "custom_fields_version": version, "structured": {
                       "entries": [{"date": "2026-10-28", "type": "revenue", "category": "营业额",
                                    "amount": 1300, "amount_basis": "gross", "granularity": "daily",
                                    "assertion": "actual", "source_quote": "今天营业额1300元"}],
                       "sales": [{"date": "2026-10-28", "product_name": "南瓜甜品", "qty": 3,
                                  "amount": 300, "unit": "份", "amount_basis": "gross",
                                  "granularity": "transaction", "assertion": "actual",
                                  "source_quote": "新品活动卖了300元",
                                  "custom_fields": [{"field_id": field_id, "value": "=新品活动",
                                                     "source_quote": "新品活动"}]}],
                       "observations": [], "thoughts": [], "leftovers": []}}
        status, _, saved = self.request("POST", "/v1/captures", payload)
        self.assertEqual((status, saved.get("error")), (200, None))
        sale_id = saved["sales"][0]["id"]
        status, _, summary = self.request("GET", "/v1/summary?workspace_id=shop&month=2026-10")
        self.assertEqual(summary["summary"]["revenue"], 1300)
        status, _, grouped = self.request("POST", "/v1/custom-field-records/group", {
            "workspace_id": "shop", "record_type": "sale", "field_id": field_id, "metric": "count"})
        self.assertEqual(status, 200)
        self.assertEqual(sum(group["count"] for group in grouped["groups"]), 1)
        status, _, revenue = self.request("POST", "/v1/custom-field-records/group", {
            "workspace_id": "shop", "record_type": "entry", "field_id": field_id, "metric": "revenue"})
        self.assertEqual(revenue["revenue"]["total"], 1300)
        self.assertEqual(revenue["revenue"]["status"], "not_attributable")
        self.assertEqual(revenue["revenue"]["tagged_sale_amount"], 300)
        self.assertNotEqual(revenue["revenue"]["total"], 1600)
        status, _, exported = self.request("POST", "/v1/custom-field-records/export", {
            "workspace_id": "shop", "record_type": "sale"})
        self.assertEqual(exported["rows"][0][-1], "'=新品活动")
        self.assertIn("'=新品活动", exported["csv"])
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            stored = CustomFieldStore(store).values_for("shop", "sale", sale_id)
        self.assertEqual(stored[0]["value"], "=新品活动")
        status, _, page = self.request("POST", "/v1/custom-field-records/query", {
            "workspace_id": "shop", "record_type": "sale", "page_size": 1,
            "filters": [{"field_id": field_id, "op": "eq", "value": "=新品活动"}]})
        self.assertEqual(status, 200)
        self.assertEqual(page["items"][0]["id"], sale_id)
        self.assertEqual(len(page["items"]), 1)
        observations = {
            "workspace_id": "shop", "request_id": "two-observations", "text": "来了12人，又来了8人",
            "structured": {"entries": [], "sales": [], "leftovers": [], "thoughts": [],
                           "observations": [
                               {"date": "2026-10-28", "kind": "客流", "measure": "人数",
                                "value_num": 12, "unit": "人", "assertion": "actual",
                                "source_quote": "来了12人"},
                               {"date": "2026-10-27", "kind": "客流", "measure": "人数",
                                "value_num": 8, "unit": "人", "assertion": "actual",
                                "source_quote": "又来了8人"}]}}
        status, _, observed = self.request("POST", "/v1/captures", observations)
        self.assertEqual(status, 200)
        status, _, cursor_page = self.request("POST", "/v1/custom-field-records/query", {
            "workspace_id": "shop", "record_type": "observation", "page_size": 1})
        self.assertEqual(len(cursor_page["items"]), 1)
        self.assertIsNotNone(cursor_page["next_before"])
        status, _, stale = self.request("POST", "/v1/custom-field-records/query", {
            "workspace_id": "shop", "record_type": "sale", "before": cursor_page["next_before"]})
        self.assertEqual(len(observed["observations"]), 2)
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"]["code"], "cursor_stale")

    def test_agent_alias_mapping_and_backfill_do_not_rewrite_finance(self):
        status, _, created = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "create-supplier", "action": "create",
            "change": {"spec": {"key": "supplier", "display_name": "供应商", "value_type": "text",
                                 "scopes": [{"record_type": "entry", "entry_types": ["purchase"]}]}}})
        self.assertEqual(status, 200)
        field_id = created["field"]["field_id"]
        status, _, alias = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "alias-monin", "action": "alias",
            "change": {"field_id": field_id, "source_text": "Monin糖浆", "target": "Monin 糖浆"}})
        self.assertEqual(status, 200)
        self.assertEqual(alias["alias"]["target"], "Monin 糖浆")
        status, _, folded = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "alias-monin-lower", "action": "alias",
            "change": {"field_id": field_id, "source_text": "monin糖浆", "target": "另一家"}})
        self.assertEqual(status, 200)
        self.assertNotEqual(folded["alias"]["alias_id"], alias["alias"]["alias_id"])
        status, _, mapped = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "map-supplier", "action": "import_mapping",
            "change": {"source_header": "商家", "target": {"kind": "custom", "field_id": field_id}}})
        self.assertEqual(mapped["mapping"]["field_id"], field_id)
        csv = "日期,类型,分类,金额,商家\n2026-10-28,进货,食材成本,80,Monin糖浆\n"
        status, _, preview = self.request("POST", "/v1/imports/preview", {
            "workspace_id": "shop", "csv": csv})
        self.assertEqual(preview["preview"]["rows"][0]["custom_fields"],
                         [{"field_id": field_id, "value": "Monin糖浆"}])
        status, _, committed = self.request("POST", "/v1/imports/commit", {
            "workspace_id": "shop", "csv": csv, "request_id": "import-supplier",
            "confirmed": True, "preview_token": preview["preview_token"]})
        self.assertEqual(committed["result"]["imported"], 1)
        entry_id = None
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            entries = store.list_entries("shop", "2026-10-28", "2026-10-28")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["amount"], 80)
            entry_id = entries[0]["id"]
            stored = CustomFieldStore(store).values_for("shop", "entry", entry_id)
        self.assertEqual(stored[0]["value"], "Monin 糖浆")
        status, _, again = self.request("POST", "/v1/imports/preview", {
            "workspace_id": "shop", "csv": csv})
        status, _, replay = self.request("POST", "/v1/imports/commit", {
            "workspace_id": "shop", "csv": csv, "request_id": "import-supplier-again",
            "confirmed": True, "preview_token": again["preview_token"]})
        self.assertEqual(replay["result"]["imported"], 0)
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            self.assertEqual(len(store.list_entries("shop", "2026-10-28", "2026-10-28")), 1)
        status, _, listed = self.request("GET", "/v1/custom-fields?workspace_id=shop")
        version = listed["custom_fields_version"]
        revision = {"record_type": "entry", "custom_fields_version": version, "changes": [
            {"target_id": entry_id, "field_id": field_id, "op": "fill", "value": "不该覆盖"}]}
        status, _, filled = self.request("POST", "/v1/custom-field-values/backfill", {
            "workspace_id": "shop", "request_id": "fill-existing", "revision": revision})
        self.assertEqual(filled["changed"], 0)
        revision["changes"][0]["op"] = "replace"
        status, _, replaced = self.request("POST", "/v1/custom-field-values/backfill", {
            "workspace_id": "shop", "request_id": "replace-supplier", "revision": revision})
        self.assertEqual(replaced["changed"], 1)
        status, _, same = self.request("POST", "/v1/custom-field-values/backfill", {
            "workspace_id": "shop", "request_id": "replace-supplier", "revision": revision})
        self.assertEqual(same, {**replaced, "duplicate": True})
        revision["changes"][0]["value"] = "换一家"
        status, _, conflict = self.request("POST", "/v1/custom-field-values/backfill", {
            "workspace_id": "shop", "request_id": "replace-supplier", "revision": revision})
        self.assertEqual((status, conflict["error"]["code"]), (409, "idempotency_conflict"))
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            entry = store.get_entry(entry_id)
            stored = CustomFieldStore(store).values_for("shop", "entry", entry_id)
        self.assertEqual(entry["amount"], 80)
        self.assertEqual(stored[0]["value"], "不该覆盖")
        status, _, disabled = self.request("POST", "/v1/custom-field-maintenance", {
            "workspace_id": "shop", "request_id": "disable-supplier", "action": "update",
            "change": {"field_id": field_id, "changes": {"enabled": False}}})
        self.assertEqual(status, 200)
        status, _, refused = self.request("POST", "/v1/custom-field-values/backfill", {
            "workspace_id": "shop", "request_id": "backfill-disabled", "revision": {
                "record_type": "entry", "custom_fields_version": disabled["custom_fields_version"],
                "changes": [{"target_id": entry_id, "field_id": field_id, "op": "clear"}]}})
        self.assertEqual(refused["error"]["code"], "field_disabled")

    def test_xlsx_upload_keeps_formula_cache_and_sheet_identity(self):
        content = _workbook({
            "1": _sheet((
                ("日期", "类型", "分类", "金额", "商家"),
                ("2026-10-28", "进货", "食材成本", "80", "阿明配送"),
            )),
            "2": _sheet((
                ("日期", "类型", "分类", "金额"),
                ("2026-10-28", "支出", "房租", None),
            ), formula_cell=("D", "2", "A2+B2")),
        })
        body, content_type = _multipart({"workspace_id": "shop", "sheet_index": "1"}, content)
        status, _, preview = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertEqual(status, 200)
        self.assertEqual(preview["preview"]["sheet"], "2")
        self.assertEqual(preview["preview"]["rows"], [])
        self.assertEqual(preview["preview"]["skipped"][0]["raw"]["金额"], "待核对")
        body, content_type = _multipart({"workspace_id": "shop"}, content)
        status, _, first = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertEqual(first["preview"]["rows"][0]["custom_fields"], [])
        commit = {"workspace_id": "shop", "request_id": "xlsx-sheet-one", "confirmed": True,
                  "filename": "book.xlsx", "preview_token": first["preview_token"]}
        status, _, committed = self.request("POST", "/v1/imports/xlsx-commit", commit)
        self.assertEqual(status, 200)
        self.assertEqual(committed["result"]["imported"], 1)
        status, _, replay = self.request("POST", "/v1/imports/xlsx-commit", commit)
        self.assertEqual(replay, {**committed, "duplicate": True})
        status, _, wrong_route = self.request("POST", "/v1/imports/commit", {
            **commit, "csv": "日期,金额\n2026-10-28,1\n"})
        self.assertEqual(wrong_route["error"]["code"], "preview_mismatch")
        body, content_type = _multipart({"workspace_id": "shop", "sheet_index": "1"}, content)
        status, _, second = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertNotEqual(second["sha256"], first["sha256"])
        status, _, conflict = self.request("POST", "/v1/imports/xlsx-commit", {
            "workspace_id": "shop", "request_id": "xlsx-sheet-one", "confirmed": True,
            "filename": "book.xlsx", "preview_token": second["preview_token"]})
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        with closing(SqliteStore(str(self.server.db_path), migrate=False)) as store:
            self.assertEqual(len(store.list_entries("shop", "2026-10-28", "2026-10-28")), 1)
        huge = b"x" * (10 * 1024 * 1024 + 1)
        body, content_type = _multipart({"workspace_id": "shop"}, huge)
        status, _, rejected = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertEqual((status, rejected["error"]["code"]), (413, "file_too_large"))
        status, _, json_limit = self.request("POST", "/v1/captures", raw_body=b"{" * 40000)
        self.assertEqual((status, json_limit["error"]["code"]), (413, "body_too_large"))
        body, content_type = _multipart({"workspace_id": "shop", "sheet_index": "bad"}, content)
        status, _, bad_sheet = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertEqual((status, bad_sheet["error"]["code"]), (400, "invalid_sheet"))

    def test_xlsx_boundaries_keep_source_and_reject_active_content(self):
        cached = _workbook({"7": _sheet(
            (("日期", "金额", "金额", ""), ("2026-10-28", "99", "12", "备注")),
            formula_cell=("B", "2", "1+1"), cached="99")})
        body, content_type = _multipart({"workspace_id": "shop"}, cached + b"\r\n")
        status, _, preview = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["rows"][0]["amount"], 99)
        self.assertIn("金额（第2列）", preview["preview"]["headers"])
        self.assertIn("列4", preview["preview"]["headers"])
        self.assertTrue(any(cell["state"] == "unverified"
                            for cell in preview["preview"]["rows"][0]["source"]["cells"]))
        duplicate = _workbook({"7": _sheet(
            (("商品", "数量", "金额", "日期"), ("牛肉面", "2", "30", "2026-10-28")))})
        body, content_type = _multipart({"workspace_id": "shop"}, duplicate)
        status, _, direction = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertTrue(direction["preview"]["direction_required"])
        self.assertEqual(direction["preview"]["kind"], "entries")
        foreign = _workbook({"1": _sheet(
            (("日期", "金额"), ("2026-10-28", "10")),
            formula_cell=("B", "2", "[Other.xlsx]Sheet1!A1"), cached="10")})
        body, content_type = _multipart({"workspace_id": "shop"}, foreign)
        status, _, rejected = self.request(
            "POST", "/v1/imports/xlsx-preview", raw_body=body, content_type=content_type)
        self.assertEqual(rejected["error"]["code"], "invalid_xlsx")
        self.assertIn("外部公式", rejected["error"]["message"])


def _workbook(sheets):
    book = io.BytesIO()
    relationships = []
    entries = []
    for index, (sheet_id, xml) in enumerate(sheets.items(), start=1):
        relationships.append(
            f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>')
        entries.append(f'<sheet name="S{index}" sheetId="{sheet_id}" r:id="rId{index}"/>')
    workbook = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{ "".join(entries) }</sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationships) + "</Relationships>")
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        for index, xml in enumerate(sheets.values(), start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)
    return book.getvalue()


def _sheet(rows, formula_cell=None, cached=None):
    body = []
    for index, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row):
            ref = chr(ord("A") + column) + str(index)
            if formula_cell and ref == formula_cell[0] + formula_cell[1]:
                value_xml = f"<v>{cached}</v>" if cached is not None else ""
                cells.append(f'<c r="{ref}"><f>{formula_cell[2]}</f>{value_xml}</c>')
            elif value is None:
                cells.append(f'<c r="{ref}"/>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
        body.append(f'<row r="{index}">' + "".join(cells) + "</row>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>" + "".join(body) + "</sheetData></worksheet>")


def _multipart(fields, content):
    boundary = "xlsx-boundary"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"book.xlsx\"\r\n"
        "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n")
    raw = "".join(parts).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return raw, f"multipart/form-data; boundary={boundary}"


if __name__ == "__main__":
    unittest.main()
