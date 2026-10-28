"""Agent-normalized capture boundaries; only disposable stores are used."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.agent_service import AgentError, AgentService, Principal
from engine.store import SqliteStore


PRINCIPAL = Principal("agent", "owner")
TEXT = ("  今天是周末，但是天气不好，阴天，天气也越来越冷了。\n"
        "外面还有跑马拉松的，可能影响生意。今天的营业额很差，只有1300。  ")
RECEIVED = "2026-10-25T20:00:00+08:00"


def payload():
    return {
        "entries": [{"date": "2026-10-25", "type": "revenue", "category": "营业额",
                     "amount": 1300, "amount_basis": "gross", "granularity": "daily",
                     "source_quote": "今天的营业额很差，只有1300", "assertion": "actual"}],
        "sales": [],
        "observations": [{"date": "2026-10-25", "kind": "天气", "measure": "天气",
                          "value_text": "阴", "source_quote": "阴天", "assertion": "actual"}],
        "leftovers": ["外面还有跑马拉松的，可能影响生意", "天气也越来越冷了"],
    }


def sale():
    return {"date": "2026-10-25", "product_name": "牛肉面", "qty": 2, "amount": 30,
            "unit": "份", "amount_basis": "gross", "granularity": "transaction",
            "source_quote": "牛肉面卖了2份30元", "assertion": "actual"}


class StructuredCaptureTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(lambda: self.store.close())
        self.store.ensure_shop("shop", "测试店", owner_id="owner")
        self.service = AgentService(self.store)

    def capture(self, structured=None, *, text=TEXT, request_id="clean", **kwargs):
        return self.service.capture_text(
            PRINCIPAL, "shop", text, request_id, structured=payload() if structured is None else structured,
            received_at=RECEIVED, **kwargs)

    def assert_empty(self, before=None):
        if before is not None:
            self.assertEqual(self.store.export_all("shop"), before)
        self.assertIsNone(self.store.agent_request_get("agent", "shop", "capture", "clean"))
        self.assertEqual(self.store.record_list("shop", "0000", "9999"), [])
        self.assertEqual(self.store.list_entries("shop", "0000", "9999"), [])
        self.assertEqual(self.store.list_sales("shop", "0000", "9999"), [])
        self.assertEqual(self.store.observation_list("shop", "0000", "9999"), [])

    def test_structured_capture_bypasses_parser_and_preserves_screenshot_narrative(self):
        before = copy.deepcopy(payload())
        with patch("engine.agent_service.ingest", side_effect=AssertionError("legacy ingest")), \
                patch("engine.parser.parse", side_effect=AssertionError("legacy parse")), \
                patch("engine.observe.find_observations", side_effect=AssertionError("raw observation")):
            result = self.capture(source="ilink", peer="sender", context="聊天记录", profile_id="restaurant")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["capture"]["raw_text"], TEXT)
        self.assertEqual(result["capture"]["source"], "ilink")
        self.assertEqual(result["capture"]["peer"], "sender")
        self.assertEqual(result["capture"]["received_at"], RECEIVED)
        self.assertEqual(result["capture"]["occurred_at"], "2026-10-25")
        self.assertEqual(result["capture"]["parser_version"], "agent-structured-2")
        self.assertEqual([(e["type"], e["amount"], e["category"], e["granularity"])
                          for e in result["entries"]], [("revenue", 1300, "营业额", "daily")])
        self.assertEqual(result["entries"][0]["note"], before["entries"][0]["source_quote"])
        self.assertEqual(result["sales"], [])
        self.assertEqual([(o["kind"], o["value_text"]) for o in result["observations"] if o["kind"] == "天气"],
                         [("天气", "阴")])
        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(result["parsed"]["leftovers"], before["leftovers"])
        self.assertEqual(result["normalization"], {**before, "thoughts": []})
        self.assertEqual(payload(), before)
        self.assertEqual(json.loads(result["capture"]["ledger_refs"]), ["entry:" + result["entries"][0]["id"]])
        self.assertEqual(self.service.summary(PRINCIPAL, "shop", "2026-10")["revenue"], 1300)

    def test_replay_is_durable_and_conflicts_on_changed_structured_or_mode(self):
        self.store.close()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "agent.db")
            self.store = SqliteStore(path)
            self.store.ensure_shop("shop", "测试店", owner_id="owner")
            self.service = AgentService(self.store)
            first = self.capture(profile_id="restaurant")
            snapshot = self.store.export_all("shop")
            self.store.workspace_set_profile("shop", "general", "owner")
            self.store.close()
            self.store = SqliteStore(path)
            self.service = AgentService(self.store)
            # Cached responses precede current profile checks.
            self.assertEqual(self.capture(profile_id="restaurant"), {**first, "duplicate": True})
            self.assertEqual(self.store.export_all("shop")["tables"]["entry"], snapshot["tables"]["entry"])
            changed = payload()
            changed["entries"][0]["amount"] = 1301
            for invoke in (
                lambda: self.capture(changed, profile_id="restaurant"),
                lambda: self.service.capture_text(PRINCIPAL, "shop", TEXT, "clean", received_at=RECEIVED,
                                                 profile_id="restaurant"),
            ):
                with self.assertRaises(AgentError) as caught:
                    invoke()
                self.assertEqual((caught.exception.code, caught.exception.status), ("idempotency_conflict", 409))

    def test_raw_first_cannot_write_again_in_structured_mode(self):
        self.service.capture_text(PRINCIPAL, "shop", TEXT, "clean", received_at=RECEIVED)
        before = self.store.export_all("shop")
        with self.assertRaises(AgentError) as caught:
            self.capture()
        self.assertEqual((caught.exception.code, caught.exception.status), ("idempotency_conflict", 409))
        self.assertEqual(self.store.export_all("shop"), before)

    def test_authorization_profile_mismatch_and_unsupported_profile_block_writes(self):
        cases = [(Principal("outsider", "foreign"), "restaurant", None, "workspace_forbidden"),
                 (PRINCIPAL, "restaurant", "general", "profile_mismatch"),
                 (PRINCIPAL, "general", None, "profile_unsupported")]
        for principal, actual_profile, requested_profile, code in cases:
            with self.subTest(code=code):
                self.store.workspace_set_profile("shop", actual_profile, "owner")
                before = self.store.export_all("shop")
                with self.assertRaises(AgentError) as caught:
                    self.service.capture_text(principal, "shop", TEXT, "clean", structured=payload(),
                                              profile_id=requested_profile)
                self.assertEqual(caught.exception.code, code)
                self.assert_empty(before)

    def test_invalid_payloads_leave_no_claim_raw_or_projection(self):
        cases = []
        for value in ([], "clean text", False, 0, {}):
            cases.append(("top-level", value))
        for field in payload():
            data = payload()
            del data[field]
            cases.append(("missing " + field, data))
            data = payload()
            data[field] = None
            cases.append(("null " + field, data))
        cases.extend([
            ("unknown top-level", {**payload(), "id": "injected"}),
            ("empty", {key: [] for key in payload()}),
            ("rows", {**payload(), "entries": payload()["entries"] * 50}),
            ("leftovers count", {**payload(), "leftovers": ["阴天"] * 51}),
            ("leftover not quoted", {**payload(), "leftovers": ["马拉松导致生意差"]}),
            ("blank leftover", {**payload(), "leftovers": [" "]}),
            ("revenue quantity", {**payload(), "entries": [{**payload()["entries"][0], "qty": 2, "unit": "份"}]}),
            ("revenue period", {**payload(), "entries": [{**payload()["entries"][0], "period_month": "2026-10"}]}),
            ("purchase period", {**payload(), "entries": [{**payload()["entries"][0], "type": "purchase",
                                                          "category": "食材成本", "period_month": "2026-10"}]}),
        ])
        entry_fields = {
            "amount": [True, "1300", float("nan"), float("inf"), -1, 10**13, 10**400, None],
            "date": ["2026-02-30", "2026-1-01", "20261025", "明天", None],
            "source_quote": ["营业额1300", "", " "],
            "assertion": ["forecast", "planned", "hypothetical", True],
            "amount_basis": ["net", "", None, []],
            "granularity": ["monthly", "", None],
            "type": ["purchase_other", [], None],
            "category": ["食材成本", "", "a" * 101],
            "qty": [1, False],
            "unit": ["斤"],
            "period_month": ["2026-13", "2026/10", "2026-1", None],
            "item": [False, "x" * 101],
            "platform": [False, "x" * 101],
            "channel": [False, "x" * 101],
            "id": ["e_foreign"], "shop_id": ["foreign"], "source": ["import"], "note": ["fake"],
        }
        for field, values in entry_fields.items():
            for value in values:
                data = payload()
                data["entries"][0][field] = value
                cases.append((f"entry {field}={value}", data))
        for field in payload()["entries"][0]:
            data = payload()
            del data["entries"][0][field]
            cases.append(("entry missing " + field, data))
        for kind, measure, value, unit in (
            ("营业额", "金额", 100, "元"), ("进货", "金额", 100, "元"),
            ("支出", "金额", 100, "元"), ("销量", "数量", 2, "份"),
            ("天气", "金额", 100, "元"), ("客流", "人数", 2, "元"),
            ("订单", "单量", True, "单"), ("评价", "评分", float("inf"), "分"),
        ):
            data = payload()
            data["observations"] = [{"date": "2026-10-25", "kind": kind, "measure": measure,
                                     "value_num": value, "unit": unit, "source_quote": "阴天",
                                     "assertion": "actual"}]
            cases.append((f"observation {kind} {measure} {unit}", data))
        for change in ({"unit": "元"}, {"value_num": 1}, {"value_text": "暴雨"},
                       {"value_text": None}, {"source_quote": "晴天"}, {"assertion": "planned"},
                       {"date": "2026-02-30"}, {"record_id": "rec_injected"}):
            data = payload()
            data["observations"][0].update(change)
            cases.append((f"observation {change}", data))
        for field in ("value_text", "date", "kind", "measure", "source_quote", "assertion"):
            data = payload()
            del data["observations"][0][field]
            cases.append(("observation missing " + field, data))
        for label, data in cases:
            with self.subTest(case=label):
                before = self.store.export_all("shop")
                with self.assertRaises(AgentError) as caught:
                    self.capture(data)
                self.assertEqual(caught.exception.code, "invalid_structured")
                self.assert_empty(before)

    def test_sales_validation_and_product_ownership(self):
        text = "牛肉面卖了2份30元"
        data = {"entries": [], "sales": [sale()], "observations": [], "leftovers": []}
        for field, value in (("qty", True), ("qty", "2"), ("qty", -1), ("qty", float("nan")),
                             ("amount", "30"), ("amount", False), ("unit", ""),
                             ("product_name", ""), ("product_name", " 牛肉面 "),
                             ("product_id", "p_foreign"), ("source", "import")):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(data)
                bad["sales"][0][field] = value
                with self.assertRaises(AgentError) as caught:
                    self.capture(bad, text=text)
                self.assertEqual(caught.exception.code, "invalid_structured")
                self.assert_empty()
        for field in sale():
            bad = copy.deepcopy(data)
            del bad["sales"][0][field]
            with self.subTest(missing=field), self.assertRaises(AgentError):
                self.capture(bad, text=text)
        self.store.ensure_shop("foreign", "另一家店", owner_id="other")
        foreign = self.store.product_upsert("foreign", "牛肉面")
        first = self.capture(data, text=text)
        row = first["sales"][0]
        self.assertNotEqual(row["product_id"], foreign["id"])
        self.assertEqual((row["qty"], row["amount"], row["unit"]), (2, 30, "份"))
        self.assertEqual(first["status"], "parsed")
        self.assertEqual(len(first["observations"]), 2)
        second = self.capture(data, text=text, request_id="other-sale")
        self.assertEqual(second["sales"][0]["product_id"], row["product_id"])

    def test_all_valid_observations_and_purchase_unknown_quantity(self):
        data = {"entries": [{**payload()["entries"][0], "type": "purchase", "category": "食材成本"}],
                "sales": [], "observations": [], "leftovers": []}
        for kind, measure, value, unit in (("客流", "人数", 2, "人"), ("客流", "桌数", 1, "桌"),
                                           ("订单", "单量", 3, "单"), ("评价", "评分", 4.5, "分"),
                                           ("评价", "差评数", 0, "个"), ("排班", "出勤", "请假", "")):
            data["observations"].append({"date": "2026-10-24", "kind": kind, "measure": measure,
                                         "value_num" if isinstance(value, (int, float)) else "value_text": value,
                                         "unit": unit, "subject": "张三", "source_quote": "阴天",
                                         "assertion": "actual"})
        result = self.capture(data)
        self.assertIsNone(result["entries"][0]["qty"])
        self.assertEqual(result["entries"][0]["unit"], "")
        self.assertEqual(result["capture"]["occurred_at"], "2026-10-25")
        self.assertEqual(len(result["observations"]), 7)

    def test_qualitative_observations_reject_invented_numbers_and_keep_summary(self):
        text = ("今天天气很好，生意还可以，老顾客非常的多，大概做了 1300。"
                "明天我想做一个新品，然后打算用南瓜来做一个甜品。"
                "南瓜甜品可以搭配木薯、银耳、枸杞等。")
        thought = "明天我想做一个新品，然后打算用南瓜来做一个甜品"
        data = {
            "entries": [], "sales": [],
            "observations": [
                {"date": "2026-10-26", "kind": "生意", "measure": "好坏", "value_text": "还可以",
                 "assertion": "actual", "source_quote": "生意还可以"},
                {"date": "2026-10-26", "kind": "客流", "measure": "情况", "value_text": "老客多",
                 "assertion": "actual", "source_quote": "老顾客非常的多"},
            ],
            "thoughts": [{"date": "2026-10-26", "title": "南瓜甜品新品",
                          "body": "打算用南瓜做甜品，可搭配木薯、银耳、枸杞。",
                          "tags": ["南瓜", "新品", "甜品"], "source_quote": thought}],
            "leftovers": ["今天天气很好", "大概做了 1300", "南瓜甜品可以搭配木薯、银耳、枸杞等。"],
        }
        result = self.capture(data, text=text, request_id="qualitative")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["entries"], [])
        self.assertEqual([(o["kind"], o["measure"], o["value_text"], o["value_num"])
                          for o in result["observations"]],
                         [("生意", "好坏", "还可以", None), ("客流", "情况", "老客多", None)])
        self.assertEqual(result["parsed"]["leftovers"], data["leftovers"])
        self.assertFalse(any(item in result["parsed"]["leftovers"] for item in ("生意还可以", "老顾客非常的多")))
        self.assertEqual(result["coverage"], {
            "business_points_saved": True, "has_unstructured_fragments": True})
        self.assertEqual([row["tags"] for row in result["thoughts"]], [["南瓜", "新品", "甜品"]])
        self.assertEqual(self.service.summary(PRINCIPAL, "shop", "2026-10")["revenue"], 0)
        guesses = [
            {"kind": "客流", "measure": "情况", "value_num": 30, "unit": "人", "source_quote": "老顾客非常的多"},
            {"kind": "生意", "measure": "好坏", "value_num": 1300, "unit": "元", "source_quote": "生意还可以"},
            {"kind": "客流", "measure": "情况", "value_text": "非常多", "source_quote": "老顾客非常的多"},
            {"kind": "生意", "measure": "好坏", "value_text": "还可以", "value_num": 1, "source_quote": "生意还可以"},
            {"kind": "天气", "measure": "天气", "value_text": "很好", "source_quote": "天气很好"},
        ]
        for guess in guesses:
            bad = {"entries": [], "sales": [], "observations": [
                {"date": "2026-10-26", "assertion": "actual", **guess}], "thoughts": [], "leftovers": []}
            with self.subTest(guess=guess), self.assertRaises(AgentError) as caught:
                self.capture(bad, text=text, request_id="guess")
            self.assertEqual(caught.exception.code, "invalid_structured")
        self.assertEqual(self.service.summary(PRINCIPAL, "shop", "2026-10")["revenue"], 0)

    def test_leftovers_only_are_partial_and_use_received_date(self):
        data = {"entries": [], "sales": [], "observations": [], "leftovers": ["阴天"]}
        result = self.capture(data, text="2099-12-31？阴天")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["capture"]["occurred_at"], "2026-10-25")
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["observations"], [])

    def test_sales_nullable_amount_and_financial_precision_are_preserved(self):
        data = payload()
        data["entries"][0]["amount"] = 0.001
        data["sales"] = [{**sale(), "amount": None}]
        result = self.capture(data, text=TEXT + "牛肉面卖了2份30元")
        self.assertEqual(result["entries"][0]["amount"], 0.001)
        self.assertIsNone(result["sales"][0]["amount"])
        self.assertEqual(result["normalization"]["entries"][0]["amount"], 0.001)

    def test_failures_roll_back_raw_claim_products_and_all_projections(self):
        data = payload()
        data["sales"] = [sale()]
        text = TEXT + "牛肉面卖了2份30元"
        for method in ("add_sales", "observation_add", "agent_request_finish", "record_detail"):
            with self.subTest(method=method):
                before = self.store.export_all("shop")
                with patch.object(self.store, method, side_effect=RuntimeError("injected failure")):
                    with self.assertRaisesRegex(RuntimeError, "injected failure"):
                        self.capture(data, text=text)
                self.assert_empty(before)
                self.assertIsNone(self.store.product_find("shop", "牛肉面"))
        with patch.object(self.store, "agent_request_finish", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "claim lost"):
                self.capture(data, text=text)
        self.assert_empty()
        result = self.capture(data, text=text)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(len(result["sales"]), 1)
        self.assertEqual(self.service.summary(PRINCIPAL, "shop", "2026-10")["revenue"], 1300)

    def test_explicit_observation_failure_after_ledger_writes_rolls_back_everything(self):
        original = self.store.observation_add

        def fail_after_explicit_write(shop_id, rows):
            ids = original(shop_id, rows)
            if any(row["kind"] == "天气" for row in rows):
                raise ValueError("explicit observation failed")
            return ids

        before = self.store.export_all("shop")
        with patch.object(self.store, "observation_add", side_effect=fail_after_explicit_write):
            with self.assertRaisesRegex(ValueError, "explicit observation failed"):
                self.capture()
        self.assert_empty(before)

    def test_quantity_period_and_aliases_use_existing_store_semantics(self):
        product = self.store.product_upsert("shop", "招牌牛肉面", aliases=["牛肉面"])
        data = {"entries": [{**payload()["entries"][0], "type": "purchase", "category": "食材成本",
                             "qty": 2.5, "unit": "公斤", "item": "牛肉", "platform": "美团",
                             "channel": "外卖", "amount_basis": "settlement", "granularity": "transaction"},
                            {**payload()["entries"][0], "type": "expense", "category": "房租",
                             "period_month": "2026-09"}],
                "sales": [sale()], "observations": [], "leftovers": []}
        result = self.capture(data, text=TEXT + "牛肉面卖了2份30元")
        purchase = next(e for e in result["entries"] if e["type"] == "purchase")
        self.assertEqual((purchase["qty"], purchase["unit"], purchase["amount_basis"], purchase["granularity"]),
                         (2.5, "公斤", "settlement", "transaction"))
        self.assertEqual(next(e for e in result["entries"] if e["type"] == "expense")["period_month"], "2026-09")
        self.assertEqual(result["sales"][0]["product_id"], product["id"])
        self.assertEqual(result["sales"][0]["product_name"], "招牌牛肉面")
        quantity = next(o for o in result["observations"] if o["kind"] == "进货" and o["measure"] == "数量")
        self.assertEqual((quantity["value_num"], quantity["unit"]), (2.5, "kg"))
        self.assertEqual(result["normalization"], {**data, "thoughts": []})

    def test_old_terminal_and_pending_evidence_are_never_reprojected(self):
        for status in ("parsed", "partial", "rejected", "parsing", "pending"):
            with self.subTest(status=status):
                dedup = "old-" + status
                record = self.store.record_add("shop", TEXT, source="agent", dedup_ref=dedup,
                                               parse_status=status, received_at=RECEIVED)
                if status == "pending":
                    self.store.add_entries([{**payload()["entries"][0], "shop_id": "shop"}], record_id=record["id"])
                before = self.store.export_all("shop")
                with self.assertRaises(AgentError) as caught:
                    self.capture(request_id=dedup, dedup_ref=dedup)
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.store.export_all("shop"), before)


if __name__ == "__main__":
    unittest.main()
