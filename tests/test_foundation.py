"""个人事业基础架构：工作空间、行业 profile 和 Agent 能力契约。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from engine.capabilities import product_capabilities
from engine.profile import IndustryProfile, ProfileRegistry
from engine.observe import ingest
from engine.store import SqliteStore
from engine.web import make_server


class FoundationStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)

    def test_multiple_workspaces_keep_owner_and_profile_isolated(self):
        personal = self.store.ensure_shop(
            "personal", "个人事业", owner_id="owner-1", profile_id="general")
        shop = self.store.ensure_shop(
            "shop-a", "糖水铺", owner_id="owner-1", profile_id="restaurant")
        other = self.store.ensure_shop(
            "other", "另一用户", owner_id="owner-2", profile_id="retail")

        self.assertEqual(personal["profile_id"], "general")
        self.assertEqual(shop["profile_id"], "restaurant")
        self.assertEqual([r["id"] for r in self.store.workspace_list("owner-1")],
                         ["personal", "shop-a"])
        self.assertIsNone(self.store.workspace_get("shop-a", "owner-2"))
        with self.assertRaises(ValueError):
            self.store.ensure_shop("shop-a", owner_id="owner-2")

        switched = self.store.workspace_set_profile("shop-a", "retail", "owner-1")
        self.assertEqual(switched["profile_id"], "retail")
        self.assertEqual(self.store.workspace_get("other", "owner-2")["profile_id"], "retail")

    def test_invalid_profile_does_not_leave_orphan_workspace(self):
        with self.assertRaises(ValueError):
            self.store.ensure_shop("invalid", "错误行业", owner_id="owner", profile_id="missing")
        self.store.ensure_shop("valid", "正常店", owner_id="owner")
        self.assertEqual(self.store.export_all("invalid")["tables"]["shop"], [])
        self.assertIsNone(self.store.workspace_get("invalid"))

    def test_record_keeps_routing_metadata_without_changing_ledger_semantics(self):
        self.store.ensure_shop("personal", "个人事业", profile_id="general")
        record = self.store.record_add(
            "personal", "想做一个新的产品", source="agent", dedup_ref="msg:1",
            context="产品想法", intent="idea", routing_status="needs_confirmation")
        self.assertEqual(record["profile_id"], "general")
        self.assertEqual(record["routing_status"], "needs_confirmation")
        self.assertTrue(self.store.record_set_routing(
            record["id"], intent="note", routing_status="classified"))
        detail = self.store.record_detail("personal", record["id"])
        self.assertEqual(detail["record"]["intent"], "note")
        self.assertEqual(detail["entries"], [])
        self.assertEqual(detail["sales"], [])

    def test_legacy_shop_export_remains_scoped(self):
        self.store.ensure_shop("a", "A", owner_id="owner-1", profile_id="general")
        self.store.ensure_shop("b", "B", owner_id="owner-1", profile_id="restaurant")
        export = self.store.export_all("a")
        self.assertEqual(export["shop_id"], "a")
        self.assertEqual([row["id"] for row in export["tables"]["shop"]], ["a"])
        self.assertEqual(export["tables"]["entry"], [])

    def test_nonrestaurant_profile_stops_legacy_parser_at_confirmation_gate(self):
        self.store.ensure_shop("project", "项目事业", profile_id="project")
        result = ingest(self.store, "project", "新品报价30元，预计下周交付")
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["entry_ids"], [])
        self.assertEqual(result["sale_ids"], [])
        self.assertEqual(self.store.list_entries("project", "0000", "9999"), [])
        self.assertEqual(self.store.list_sales("project", "0000", "9999"), [])
        self.assertEqual(self.store.observation_list("project", "0000", "9999"), [])
        record = self.store.record_list("project", "0000", "9999")[0]
        self.assertEqual(record["routing_status"], "needs_confirmation")


class ProfileAndCapabilityTests(unittest.TestCase):
    def test_profile_registry_is_declared_and_rejects_unknown_ids(self):
        registry = ProfileRegistry()
        registry.register(IndustryProfile("salon", "美容服务", "测试行业"))
        self.assertEqual(registry.get("salon").label, "美容服务")
        with self.assertRaises(ValueError):
            registry.get("missing")
        with self.assertRaises(ValueError):
            IndustryProfile("Bad ID", "坏", "坏")

    def test_agent_contract_explains_capability_limits_and_questions(self):
        contract = product_capabilities()
        by_id = {item["id"]: item for item in contract["capabilities"]}
        self.assertTrue(contract["agent_policy"]["read_before_action"])
        self.assertTrue(contract["agent_policy"]["ask_before_unsupported"])
        self.assertEqual(by_id["task.reminder"]["status"], "planned")
        self.assertEqual(by_id["wechat.personal"]["status"], "unsupported")
        self.assertTrue(any("实际发生" in item for item in contract["agent_policy"]["never_guess"]))
        self.assertTrue(any(p["id"] == "restaurant" for p in contract["profiles"]))
        preparation = contract["agent_policy"]["input_preparation"]
        self.assertTrue(preparation["user_speech_may_be_messy"])
        self.assertEqual(preparation["responsible_party"], "calling_agent")
        self.assertEqual(preparation["preferred_payload_field"], "structured")
        self.assertTrue(preparation["preserve_verbatim_text"])
        self.assertEqual(by_id["capture.structured"]["status"], "available")
        ilink = contract["agent_policy"]["ilink_integration"]
        self.assertFalse(ilink["builtin_worker_uses_llm"])
        self.assertTrue(ilink["single_poller_per_bot_account"])
        self.assertTrue(ilink["external_agent_should_keep_builtin_worker_disabled"])
        result_policy = contract["agent_policy"]["capture_result_policy"]
        self.assertFalse(result_policy["http_200_means_financial_write"])
        self.assertEqual(result_policy["report_financial_rows_from"], ["entries", "sales"])
        self.assertEqual(result_policy["report_unparsed_fragments_from"], "parsed.leftovers")
        self.assertTrue(result_policy["partial_may_include_financial_rows"])
        self.assertTrue(result_policy["preserve_original_text"])
        self.assertTrue(result_policy["do_not_rewrite_and_resubmit_to_force_parsing"])


class FoundationHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(0, Path(self.tmp.name) / "foundation.db")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        status = response.status
        conn.close()
        return status, json.loads(body)

    def test_capabilities_and_workspaces_are_read_only_json_endpoints(self):
        status, contract = self.request("/api/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(contract["contract_version"], "foundation-1")
        status, workspaces = self.request("/api/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual(workspaces, {"workspaces": []})


if __name__ == "__main__":
    unittest.main()
