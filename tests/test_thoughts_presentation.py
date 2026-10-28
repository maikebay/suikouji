"""随想能力契约与中文回执：仅内存库、临时状态和假通道，无网络。"""
from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from channel.bot import ILinkBotWorker
from channel.ilink import StateStore
from engine.agent_normalization import validate_structured
from engine.agent_service import AgentService, Principal
from engine.capabilities import product_capabilities
from engine.cli import cmd_record
from engine.store import SqliteStore


THOUGHT = {"id": "internal-thought-id", "date": "2026-10-26", "body": "明天试南瓜甜品",
           "tags": [], "source_quote": "明天试南瓜甜品"}
ENTRY = {"id": "internal-entry-id", "date": "2026-10-26", "type": "revenue",
         "category": "营业额", "amount": 1300, "item": "营业额"}


def receipt(*, thoughts=True, status="parsed", **changes):
    result = {
        "status": status, "duplicate": False,
        "record": {"id": "internal-record-id"},
        "entries": [], "entry_ids": [], "sales": [], "observations": [],
        "thoughts": [copy.deepcopy(THOUGHT)] if thoughts else [],
        "parsed": {"intent": "record", "entries": [], "sales": [], "leftovers": [],
                   "thoughts": [copy.deepcopy(THOUGHT)] if thoughts else []},
    }
    result.update(changes)
    return result


class ThoughtsContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = product_capabilities()
        self.policy = self.contract["agent_policy"]
        self.preparation = self.policy["input_preparation"]

    def test_schema_keeps_four_required_arrays_and_independent_thought_fields(self):
        schema = self.preparation["structured_schema"]
        self.assertEqual(set(schema["required"]), {"entries", "sales", "observations", "leftovers"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["thoughts"]["type"], "array")
        self.assertEqual(schema["properties"]["thoughts"]["maxItems"], 50)
        row = schema["properties"]["thoughts"]["items"]
        self.assertEqual(set(row["required"]), {"date", "body", "source_quote"})
        self.assertEqual(set(row["properties"]), {"date", "title", "body", "tags", "source_quote"})
        self.assertFalse(row["additionalProperties"])
        self.assertEqual(row["properties"]["title"]["maxLength"], 80)
        self.assertEqual(row["properties"]["body"]["maxLength"], 2000)
        self.assertEqual(row["properties"]["source_quote"]["maxLength"], 2000)
        self.assertEqual(row["properties"]["tags"]["maxItems"], 20)
        for name in ("entries", "sales", "observations"):
            self.assertEqual(schema["properties"][name]["items"]["properties"]["assertion"]["enum"], ["actual"])

    def test_published_example_validates_unchanged_and_commits_real_rows(self):
        example = copy.deepcopy(self.preparation["request_example"])
        self.assertEqual(validate_structured(example["text"], example["structured"]), example["structured"])
        store = SqliteStore(":memory:")
        self.addCleanup(store.close)
        store.ensure_shop("shop", "临时餐饮账本", owner_id="owner")
        service = AgentService(store)
        result = service.capture_text(Principal("presentation", "owner"), **{**example, "workspace_id": "shop"})
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["capture"]["raw_text"], example["text"])
        self.assertEqual([row["amount"] for row in result["entries"]], [1300])
        self.assertEqual(len(result["thoughts"]), 1)
        self.assertEqual(result["thoughts"][0]["date"], "2026-10-26")
        self.assertEqual(result["thoughts"][0]["tags"], ["新品", "南瓜"])
        qualitative = {(row["kind"], row["measure"], row["value_text"], row["value_num"])
                       for row in result["observations"] if row["measure"] in {"情况", "好坏"}}
        self.assertEqual(qualitative, {("生意", "好坏", "还可以", None), ("客流", "情况", "老客多", None)})
        self.assertEqual(result["normalization"], example["structured"])
        self.assertIn("已保存 1 条随想", ILinkBotWorker.format_reply(result))
        self.assertEqual(service.summary(Principal("presentation", "owner"), "shop", "2026-10")["revenue"], 1300)

    def test_old_arrays_normalize_without_mutating_request(self):
        structured = {"entries": [], "sales": [], "observations": [], "leftovers": ["不确定"]}
        before = copy.deepcopy(structured)
        self.assertEqual(validate_structured("不确定", structured), {**before, "thoughts": []})
        self.assertEqual(structured, before)

    def test_pure_thought_no_tags_needs_no_financial_fact(self):
        thought = {key: THOUGHT[key] for key in ("date", "body", "source_quote")}
        structured = {"entries": [], "sales": [], "observations": [], "leftovers": [], "thoughts": [thought]}
        result = validate_structured(THOUGHT["body"], structured)
        self.assertEqual(result["thoughts"], [{**thought, "tags": []}])
        self.assertNotIn("tags", thought)
        for field in ("assertion", "type", "plan", "task", "status", "due_at", "amount", "id", "workspace_id"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_structured(THOUGHT["body"], {**structured, "thoughts": [{**thought, field: "forbidden"}]})

    def test_available_operations_and_native_boundaries_are_explicit(self):
        capabilities = {item["id"]: item for item in self.contract["capabilities"]}
        for name in ("thought.capture", "thought.list"):
            self.assertEqual(capabilities[name]["status"], "available")
        self.assertIn("餐饮", "".join(capabilities["thought.capture"]["limitations"]))
        operations = AgentService.capabilities()["operations"]
        self.assertEqual(operations["list_thoughts"]["path"], "/v1/thoughts")
        native = self.preparation["native_thought_rules"]
        self.assertEqual(native["markers"], ["记个想法：", "记个想法:", "记个随想：", "记个随想:"])
        self.assertFalse(native["uses_model"])
        self.assertTrue(native["explicit_archive_requires_agent"])
        dates = self.preparation["thought_date_policy"]
        self.assertFalse(dates["body_time_changes_archive_date"])
        self.assertFalse(dates["native_explicit_archive_supported"])
        self.assertEqual(dates["default"], "first_received_date_in_workspace_timezone")

    def test_onboarding_is_short_and_only_for_connected_external_agent(self):
        onboarding = self.policy["onboarding"]
        intro = onboarding["introduction_template"].format(workspace_name="糖水铺")
        self.assertGreaterEqual(len(intro), 120)
        self.assertLessEqual(len(intro), 220)
        self.assertEqual(onboarding["audience"], "external_agent")
        self.assertEqual(onboarding["requires_capabilities"], ["thought.capture", "thought.list"])
        self.assertFalse(onboarding["builtin_worker_auto_welcome"])
        self.assertFalse(onboarding["ask_for_tags"])
        for promise in ("模型", "提醒", "撤销", "改名", "画像", "固定报表", "库存"):
            self.assertNotIn(promise, intro)
        ilink = self.policy["ilink_integration"]
        for flag in ("builtin_worker_uses_llm", "builtin_worker_query_dialogue", "builtin_worker_auto_welcome",
                     "builtin_worker_followup_state"):
            self.assertFalse(ilink[flag])

    def test_receipt_policy_does_not_equate_thoughts_with_financial_writes(self):
        policy = self.policy["capture_result_policy"]
        self.assertEqual(policy["report_thought_rows_from"], "thoughts")
        self.assertEqual(policy["report_financial_rows_from"], ["entries", "sales"])
        self.assertFalse(policy["http_200_means_financial_write"])
        self.assertTrue(policy["missing_thoughts_means_empty"])
        self.assertTrue(policy["historical_cached_receipts_unchanged"])
        self.assertTrue(policy["new_normalization_includes_thoughts"])
        self.assertTrue(policy["unknown_result_retry_same_request"])
        self.assertEqual(policy["unknown_result_text"], "暂时没拿到结果，正在核对。")
        qualitative = self.preparation["qualitative_observations"]
        self.assertTrue(qualitative["no_numeric_inference"])
        self.assertEqual(qualitative["traffic"]["value_text"], ["老客多", "人多", "客人少", "客流一般"])
        self.assertEqual(qualitative["business"]["value_text"], ["还可以", "不错", "一般", "不好"])
        self.assertIn("value_num", qualitative["forbid"])
        schema = self.preparation["structured_schema"]["properties"]["observations"]["items"]
        self.assertIn("生意", schema["properties"]["kind"]["enum"])
        self.assertIn("情况", schema["properties"]["measure"]["enum"])
        self.assertIn("好坏", schema["properties"]["measure"]["enum"])
        self.assertEqual(policy["coverage_field"], "coverage")
        self.assertIn("observations", policy["business_points_saved_from"])


class ReadableReplyAssertions:
    def assert_readable(self, text):
        for internal in ("internal-", "profile", "parsed", "partial", "leftovers", "None", "JSON", "/v1/", "/srv/"):
            self.assertNotIn(internal, text)


class ThoughtsReplyTests(ReadableReplyAssertions, unittest.TestCase):
    def test_pure_thought_counts_public_rows_without_claiming_income_or_asking_tags(self):
        data = receipt()
        data["parsed"]["thoughts"] *= 5
        text = ILinkBotWorker.format_reply(data)
        self.assertIn("已保存 1 条随想", text)
        self.assertIn("2026-10-26", text)
        for phrase in ("已入账", "账目 1", "标签", "提醒", "撤销"):
            self.assertNotIn(phrase, text)
        self.assert_readable(text)

    def test_saved_points_and_bare_fragments_use_different_wording(self):
        saved = receipt(thoughts=False, status="partial",
                        observations=[{"kind": "生意", "value_text": "还可以"}])
        saved["parsed"]["leftovers"] = ["今天天气很好"]
        text = ILinkBotWorker.format_reply(saved)
        self.assertIn("经营观测 1 条", text)
        self.assertIn("经营要点已记下", text)
        self.assertIn("未结构化", text)
        self.assertIn("今天天气很好", text)
        bare = receipt(thoughts=False, status="unparsed")
        bare["parsed"]["leftovers"] = ["大概做了1300"]
        text = ILinkBotWorker.format_reply(bare)
        self.assertNotIn("经营要点已记下", text)
        self.assertIn("尚未结构化", text)
        self.assertIn("金额未入账", text)
        self.assert_readable(text)

    def test_partial_separates_saved_thoughts_and_unposted_amount(self):
        data = receipt(status="partial", entries=[ENTRY])
        data["parsed"]["leftovers"] = ["大概做了1300", "天气很好"]
        text = ILinkBotWorker.format_reply(data)
        for phrase in ("账目 1 条", "已保存 1 条随想", "经营要点已记下", "未结构化", "金额未入账", "大概做了1300", "不要重发整段原话"):
            self.assertIn(phrase, text)
        self.assertNotIn("全部", text)
        self.assert_readable(text)

    def test_historical_receipt_without_thoughts_stays_compatible(self):
        data = receipt(thoughts=False, entries=[ENTRY], parsed=None)
        del data["thoughts"]
        before = copy.deepcopy(data)
        text = ILinkBotWorker.format_reply(data)
        self.assertIn("账目 1 条", text)
        self.assertNotIn("条随想", text)
        self.assertEqual(data, before)
        self.assert_readable(text)

    def test_duplicate_partial_keeps_saved_count_and_pending_boundary(self):
        text = ILinkBotWorker.format_reply(receipt(status="partial", duplicate=True))
        self.assertIn("没有重复入账", text)
        self.assertIn("已保存 1 条随想", text)
        self.assertIn("未处理部分涉及的金额未入账", text)
        self.assert_readable(text)

    def test_unknown_result_does_not_invent_success_failure_or_draft(self):
        for status in (None, "pending", "parsing", "unexpected"):
            with self.subTest(status=status):
                text = ILinkBotWorker.format_reply(receipt(status=status))
                self.assertEqual(text, "暂时没拿到结果，正在核对。")

    def test_profile_rejection_and_warnings_never_echo_internal_details(self):
        for status in ("needs_confirmation", "rejected"):
            text = ILinkBotWorker.format_reply(receipt(status=status, warnings=["/srv/private/db JSON /v1/captures"]))
            self.assert_readable(text)
            self.assertNotIn("已保存 1 条随想", text)
        text = ILinkBotWorker.format_reply(receipt(warnings=["/srv/private/db JSON /v1/captures"]))
        self.assertIn("待核对", text)
        self.assert_readable(text)

    def test_explicit_archive_warning_names_external_assistant_requirement(self):
        data = receipt(thoughts=False, status="unparsed",
                       warnings=["原生录入暂不支持指定随想归档日，需要外部 Agent 整理。"])
        text = ILinkBotWorker.format_reply(data)
        self.assertIn("指定随想归档日需要外部助手整理", text)
        self.assertIn("相关随想尚未保存", text)
        self.assert_readable(text)


class ThoughtsCliTests(ReadableReplyAssertions, unittest.TestCase):
    def run_cli(self, data, *, as_json=False, error=None):
        output = io.StringIO()
        args = SimpleNamespace(db=":memory:", shop="test-workspace", text="测试原话",
                               dedup_ref="stable-message", json=as_json)
        with patch("engine.cli.SqliteStore") as store, \
                patch("engine.cli.ingest", return_value=data, side_effect=error), redirect_stdout(output):
            status = cmd_record(args)
        store.assert_called_once_with(":memory:")
        return status, output.getvalue()

    def test_cli_pure_thought_and_machine_payload(self):
        data = receipt()
        data["parsed"]["thoughts"] *= 4
        status, text = self.run_cli(data)
        self.assertEqual(status, 0)
        self.assertIn("已保存 1 条随想", text)
        self.assertNotIn("已记账", text)
        self.assertNotIn("标签", text)
        self.assert_readable(text)
        _, output = self.run_cli(data, as_json=True)
        payload = json.loads(output)
        self.assertEqual(payload["thoughts"], data["thoughts"])
        self.assertFalse(payload["duplicate"])

    def test_cli_mixed_partial_has_actual_financial_rows_and_pending_fragments(self):
        data = receipt(status="partial", entry_ids=["internal-entry-id"])
        data["parsed"]["entries"] = [ENTRY]
        data["parsed"]["leftovers"] = ["花了两百多"]
        status, text = self.run_cli(data)
        self.assertEqual(status, 0)
        self.assertIn("已记账", text)
        self.assertIn("已保存 1 条随想", text)
        self.assertIn("花了两百多", text)
        self.assertIn("经营要点已记下", text)
        self.assertIn("未结构化", text)
        self.assertIn("金额未入账", text)
        self.assertNotIn("撤销", text)
        self.assert_readable(text)

    def test_cli_empty_summary_and_zero_sales_are_readable(self):
        data = receipt(entry_ids=["internal-entry-id"])
        data["parsed"]["entries"] = [{**ENTRY, "item": None}]
        data["parsed"]["sales"] = [{"date": "2026-10-26", "product_name": "试吃", "qty": 1,
                                    "unit": "份", "amount": 0}]
        _, text = self.run_cli(data)
        self.assertIn("| 营业额 | — |", text)
        self.assertIn("| 试吃 | 1份 | 0.00 |", text)
        self.assert_readable(text)

    def test_cli_old_cache_without_thoughts_or_parsed_and_new_duplicate(self):
        data = receipt(duplicate=True, parsed=None)
        data.pop("thoughts")
        status, text = self.run_cli(data)
        self.assertEqual(status, 0)
        self.assertIn("没有重复入库", text)
        self.assertNotIn("条随想", text)
        self.assert_readable(text)
        new = receipt(duplicate=True, status="partial")
        _, text = self.run_cli(new)
        self.assertIn("已保存 1 条随想", text)
        self.assertIn("金额未入账", text)
        _, output = self.run_cli(new, as_json=True)
        self.assertTrue(json.loads(output)["duplicate"])
        self.assertEqual(json.loads(output)["thoughts"], new["thoughts"])

    def test_cli_unparsed_never_instructs_json_resubmission(self):
        data = receipt(thoughts=False, status="unparsed")
        data["parsed"]["leftovers"] = ["天气很好"]
        status, text = self.run_cli(data)
        self.assertEqual(status, 3)
        self.assertIn("金额未入账", text)
        self.assertIn("天气很好", text)
        self.assertIn("不要重发整段原话", text)
        self.assertNotIn("add-json", text)
        self.assert_readable(text)

    def test_cli_profile_pending_and_rejected_do_not_claim_thoughts_saved(self):
        for status in ("needs_confirmation", "pending", "parsing", "rejected"):
            with self.subTest(status=status):
                code, text = self.run_cli(receipt(status=status, duplicate=True))
                self.assertEqual(code, 3)
                self.assertNotIn("已保存 1 条随想", text)
                self.assert_readable(text)
        code, text = self.run_cli({}, error=ValueError("/srv/private/db JSON internal-error"))
        self.assertEqual(code, 3)
        self.assert_readable(text)

    def test_cli_archive_instruction_requires_external_assistant(self):
        data = receipt(thoughts=False, status="unparsed", warnings=["归档日需要外部Agent"])
        code, text = self.run_cli(data)
        self.assertEqual(code, 3)
        self.assertIn("指定随想归档日需要外部助手整理", text)
        self.assertIn("相关随想尚未保存", text)
        self.assert_readable(text)


class ThoughtsWorkerRecoveryTests(unittest.TestCase):
    def test_send_failure_replays_same_identity_without_advancing_cursor(self):
        msg = {"message_id": "stable", "sender_id": "peer", "message_type": 1, "message_state": 2,
               "context_token": "fake-context", "item_list": [{"type": 1, "text_item": {"text": "明天想试南瓜甜品"}}]}
        batch = {"msgs": [msg], "get_updates_buf": "next"}
        service = Mock()
        service.capture_text.side_effect = [receipt(), receipt(duplicate=True)]
        client = Mock()
        client.get_updates.return_value = batch
        client.send_long_text.side_effect = [OSError("synthetic send failure"), None]
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(str(Path(tmp) / "fake-state.json"))
            worker = ILinkBotWorker(client, Mock(), state, principal=Principal("fake", "owner"),
                                    workspace_id="shop", allowed_peers={"peer": "shop"},
                                    service=service, sleep=lambda _: None)
            self.assertEqual(client.send_long_text.call_count, 0)
            with self.assertRaises(OSError):
                worker.run_once()
            self.assertEqual(state.sync_buf, "")
            self.assertEqual(state.dispositions, {})
            worker.run_once()
            self.assertEqual(service.capture_text.call_args_list[0], service.capture_text.call_args_list[1])
            self.assertEqual(state.sync_buf, "next")
            self.assertIn("已保存 1 条随想", client.send_long_text.call_args.args[1])
            self.assertIn("没有重复入账", client.send_long_text.call_args.args[1])

    def test_unknown_capture_result_retries_same_identity_without_false_reply(self):
        msg = {"message_id": "stable", "sender_id": "peer", "message_type": 1, "message_state": 2,
               "item_list": [{"type": 1, "text_item": {"text": "明天想试南瓜甜品"}}]}
        service = Mock()
        service.capture_text.side_effect = [TimeoutError("synthetic timeout"), receipt(duplicate=True)]
        client = Mock()
        client.get_updates.return_value = {"msgs": [msg], "get_updates_buf": "next"}
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(str(Path(tmp) / "fake-state.json"))
            worker = ILinkBotWorker(client, Mock(), state, principal=Principal("fake", "owner"),
                                    workspace_id="shop", allowed_peers={"peer": "shop"},
                                    service=service, sleep=lambda _: None)
            with self.assertRaises(TimeoutError):
                worker.run_once()
            client.send_long_text.assert_not_called()
            self.assertEqual(state.sync_buf, "")
            worker.run_once()
            self.assertEqual(service.capture_text.call_args_list[0], service.capture_text.call_args_list[1])
            self.assertIn("已保存 1 条随想", client.send_long_text.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
