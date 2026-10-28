"""随想校验与共享原生录入回归；仅使用内存 SQLite，无网络和真实账本。"""
import copy
import datetime as dt
import unittest
from unittest.mock import patch

from engine import observe
from engine.store import SqliteStore
from engine.thoughts import normalize_tag, normalize_tags, receipt_date, validate_thought


DAY = "2026-10-26"
BASE = dt.date(2026, 10, 26)


class ThoughtValidationTests(unittest.TestCase):
    def row(self, **fields):
        return {"date": DAY, "body": "可能试试南瓜甜品", "source_quote": "南瓜甜品", **fields}

    def test_detached_normalization_preserves_text_and_optional_title(self):
        row = self.row(body="  可能试试南瓜甜品  ", title="  灵感  ",
                       tags=[" #新品 ", "新品", "café", "café", "New", "new"])
        before = copy.deepcopy(row)
        result = validate_thought(row, "记个想法：南瓜甜品")
        self.assertEqual(row, before)
        self.assertIsNot(row, result)
        self.assertIsNot(row["tags"], result["tags"])
        self.assertEqual(result["tags"], ["新品", "café", "New", "new"])
        self.assertEqual(result["body"], row["body"])
        self.assertEqual(result["title"], row["title"])
        self.assertEqual(validate_thought(self.row(), "南瓜甜品")["tags"], [])
        self.assertNotIn("title", validate_thought(self.row(), "南瓜甜品"))

    def test_tags_validate_input_count_before_dedup(self):
        self.assertEqual(normalize_tags(["新品"] * 20), ["新品"])
        with self.assertRaises(ValueError):
            normalize_tags(["新品"] * 21)
        for tags in (None, {}, "新品", [None], [1]):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                normalize_tags(tags)

    def test_tag_unicode_prefix_whitespace_and_limits(self):
        self.assertEqual(normalize_tag("  #  café  "), "café")
        self.assertEqual(normalize_tag("南" * 32), "南" * 32)
        for tag in ("", "#", "##新品", "新#品", "新 品", "新\t品", "新\n品",
                    "新\x00品", "新​品", "新‮品", "南" * 33, None, 3):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                normalize_tag(tag)

    def test_thought_rejects_financial_assertion_task_and_owned_fields(self):
        for name in ("assertion", "type", "status", "scheduled_at", "due_at", "amount",
                     "id", "workspace_id", "capture_id", "created_at", "updated_at"):
            with self.subTest(field=name), self.assertRaises(ValueError):
                validate_thought(self.row(**{name: "actual"}), "南瓜甜品")
        for required in ("date", "body", "source_quote"):
            row = self.row()
            del row[required]
            with self.assertRaises(ValueError):
                validate_thought(row, "南瓜甜品")

    def test_thought_field_shapes_quotes_dates_and_unicode_limits(self):
        for patch_row in ({"date": "2026-02-30"}, {"date": "20261026"}, {"date": "2026-1-01"},
                          {"date": None}, {"body": " "}, {"body": None}, {"body": "南" * 2001},
                          {"title": None}, {"title": ""}, {"title": "南" * 81},
                          {"source_quote": "南瓜 甜品"}, {"source_quote": ""},
                          {"source_quote": None}, {"source_quote": "南" * 2001}, {"tags": None}):
            with self.subTest(fields=patch_row), self.assertRaises(ValueError):
                validate_thought(self.row(**patch_row), "南瓜甜品")
        row = self.row(body="南" * 2000, title="南" * 80, source_quote="南" * 2000)
        self.assertEqual(validate_thought(row, "南" * 2000)["body"], "南" * 2000)

    def test_thought_text_rejects_nul_without_rewriting_source(self):
        for field in ("body", "title", "source_quote"):
            for value in ("\x00南瓜甜品", "南瓜\x00甜品", "南瓜甜品\x00"):
                with self.subTest(field=field, value=value):
                    row = self.row(**{field: value})
                    before = copy.deepcopy(row)
                    with self.assertRaises(ValueError):
                        validate_thought(row, value if field == "source_quote" else "南瓜甜品")
                    self.assertEqual(row, before)

    def test_receipt_date_uses_configured_timezone_and_legacy_wall_time(self):
        self.assertEqual(receipt_date("2026-10-26T00:30:00+08:00", "America/Los_Angeles"), "2026-10-25")
        self.assertEqual(receipt_date("2026-10-25T23:30:00Z", "Asia/Shanghai"), DAY)
        self.assertEqual(receipt_date("2026-10-26T00:30:00", "America/Los_Angeles"), DAY)
        for stamp, zone in (("bad", "Asia/Shanghai"), (None, "local"), ("2026-10-26", "missing/timezone")):
            with self.assertRaises(ValueError):
                receipt_date(stamp, zone)


class NativeThoughtTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_shop("s", timezone="Asia/Shanghai")

    def ingest(self, text, **fields):
        return observe.ingest(self.store, "s", text, base=BASE, **fields)

    def test_prd_mixed_weather_revenue_thought_and_tags(self):
        raw = "  今天营业额1300元，阴天。记个想法：明天想用南瓜做甜品，可以搭配木薯、银耳、枸杞。#新品 #南瓜  "
        result = self.ingest(raw, dedup_ref="mixed")
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["record"]["raw_text"], raw)
        self.assertEqual(result["parsed"]["raw"], raw)
        self.assertEqual(result["record"]["occurred_at"], DAY)
        self.assertEqual(result["record"]["parser_version"], observe.PARSER_VERSION)
        self.assertNotEqual(observe.PARSER_VERSION, "observe-1")
        self.assertEqual(len(result["entry_ids"]), 1)
        self.assertEqual(result["sale_ids"], [])
        self.assertEqual(result["parsed"]["leftovers"], [])
        thought = result["thoughts"][0]
        self.assertEqual(thought["date"], DAY)
        self.assertEqual(thought["body"], "明天想用南瓜做甜品，可以搭配木薯、银耳、枸杞。")
        self.assertEqual(thought["tags"], ["新品", "南瓜"])
        self.assertIn(thought["source_quote"], raw)
        self.assertNotIn("title", thought)
        self.assertEqual(self.store.thought_for_record("s", result["record"]["id"]), result["thoughts"])
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 1300)
        self.assertEqual([(o["kind"], o["value_text"]) for o in result["observations"]], [("天气", "阴")])

    def test_both_mixed_orders_and_date_isolation(self):
        note = "记个想法：明天预算300元，售价12元，客流100人，2026-02-30试做。"
        for raw in ("昨天营业额1300元。" + note, note + "昨天营业额1300元"):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["status"], "parsed")
                self.assertEqual([(r["date"], r["amount"]) for r in result["parsed"]["entries"]],
                                 [("2026-10-25", 1300)])
                self.assertEqual(result["record"]["occurred_at"], "2026-10-25")
                self.assertEqual(result["thoughts"][0]["date"], DAY)
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["sale_ids"], [])

    def test_hidden_notes_are_hard_boundaries_with_verbatim_leftovers(self):
        for sep in ("\n", "\r\n"):
            for before, after in (("收入", "12"), ("客流", "10人"),
                                  ("牛肉面卖了", "3份")):
                raw = before + sep + "记个想法：明天试新品" + sep + after
                with self.subTest(raw=raw):
                    result = self.ingest(raw)
                    self.assertEqual(result["entry_ids"], [])
                    self.assertEqual(result["sale_ids"], [])
                    self.assertEqual(result["observations"], [])
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["parsed"]["leftovers"], [before, after])
                    detail = self.store.record_detail("s", result["record"]["id"])
                    self.assertEqual(detail["record"]["raw_text"], raw)
                    self.assertEqual(len(detail["thoughts"]), 1)
        raw = "昨天客流10人\n记个想法：2027-01-01试新品\n收入12元，牛肉面卖了3份30元"
        result = self.ingest(raw)
        self.assertEqual(result["status"], "parsed")
        self.assertEqual([(e["date"], e["amount"], e["note"]) for e in result["parsed"]["entries"]],
                         [("2026-10-25", 12, "收入12元")])
        self.assertEqual([(o["occurred_at"], o["source_span"]) for o in result["observations"]],
                         [("2026-10-25", "客流10人")])
        sale = result["parsed"]["sales"][0]
        self.assertEqual((sale["date"], sale["qty"], sale["amount"]), ("2026-10-25", 3, 30))
        self.assertEqual(raw[sale["start"]:sale["end"]], "牛肉面卖了3份30元")

    def test_mixed_observation_leftovers_and_entry_notes_are_source_slices(self):
        raw = "生意还可以客流10人老顾客很多。记个想法：试新品"
        result = self.ingest(raw)
        self.assertEqual(result["parsed"]["leftovers"], [])
        spans = {o["source_span"]: (o["kind"], o["measure"], o["value_num"], o["value_text"])
                 for o in result["observations"]}
        self.assertEqual(spans["客流10人"][0:3], ("客流", "人数", 10))
        self.assertEqual(spans["生意还可以"], ("生意", "好坏", None, "还可以"))
        self.assertEqual(spans["老顾客很多"], ("客流", "情况", None, "老客多"))
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 0)
        raw = "生意还可以\n记个想法：明天试新品\n老顾客非常多"
        result = self.ingest(raw)
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["entry_ids"], [])
        self.assertEqual(result["parsed"]["leftovers"], [])
        self.assertEqual(sorted((o["kind"], o["value_text"], o["value_num"]) for o in result["observations"]),
                         [("客流", "老客多", None), ("生意", "还可以", None)])
        self.assertEqual(len(result["thoughts"]), 1)
        inside = self.ingest("记个想法：生意还可以，老客多")
        self.assertEqual(inside["observations"], [])
        self.assertEqual(inside["parsed"]["leftovers"], [])
        self.assertIn("生意还可以", inside["thoughts"][0]["body"])
        raw = "营业额客流10人1300元。记个想法：试新品"
        result = self.ingest(raw)
        self.assertEqual(result["parsed"]["entries"][0]["note"], "营业额客流10人1300元")
        self.assertEqual(result["parsed"]["entries"][0]["amount"], 1300)

    def test_approximation_tokens_cannot_cross_saved_notes(self):
        raw = "花了两百\n记个想法：明天试新品\n多"
        result = self.ingest(raw)
        before = self.ingest("花了两百")
        self.assertEqual(result["parsed"]["entries"], before["parsed"]["entries"])
        self.assertEqual(result["parsed"]["leftovers"], before["parsed"]["leftovers"] + ["多"])
        self.assertEqual(result["thoughts"][0]["body"], "明天试新品")

    def test_date_tokens_cannot_join_across_hidden_notes(self):
        raw = "2月\n记个想法：试新品\n30日房租800元"
        result = self.ingest(raw)
        self.assertEqual([(e["date"], e["amount"]) for e in result["parsed"]["entries"]],
                         [("2026-10-30", 800)])
        self.assertEqual(result["record"]["occurred_at"], "2026-10-30")
        self.assertEqual(result["parsed"]["leftovers"], ["2月"])

    def test_note_future_dates_cannot_reach_observations_or_finance(self):
        raw = "昨天营业额1300元。记个想法：明天试做3份，卖12元，阴天，2027-01-01再试。客流10人"
        result = self.ingest(raw)
        self.assertEqual([(o["kind"], o["value_num"], o["occurred_at"]) for o in result["observations"]],
                         [("客流", 10, "2026-10-25")])
        self.assertEqual(result["record"]["occurred_at"], "2026-10-25")
        self.assertEqual(len(result["entry_ids"]), 1)
        self.assertEqual(result["sale_ids"], [])

    def test_supported_markers_future_and_reflection_phrases(self):
        for raw in ("记个想法：下周试芋圆新品", "记个想法:预算300元，售价12元",
                    "记个随想：今天感觉一般", "记个随想:昨天想到下周推新品",
                    "明天想试南瓜甜品 #新品", "明天我想做一个新品", "明天试做南瓜甜品",
                    "下周想推新品", "下周想推芋圆新品。#新品", "下周打算试南瓜甜品",
                    "今天想到明天试做南瓜甜品", "昨天想到下周推新品"):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["status"], "parsed")
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["sale_ids"], [])
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["thoughts"][0]["date"], DAY)
                self.assertEqual(result["record"]["occurred_at"], DAY)
                self.assertIn(result["thoughts"][0]["source_quote"], raw)
                self.assertEqual(result["parsed"]["leftovers"], [])

    def test_ascii_sentence_dots_separate_notes_but_not_literals_or_decimals(self):
        for body in ("售价12", "试新品", "试12.5元新品", "[说明](https://example.invalid/a.b)",
                     "https://example.invalid/a.b", "`版本1.2;售价12.5元`"):
            with self.subTest(body=body):
                raw = "记个想法：" + body + ". 今天营业额1300元"
                result = self.ingest(raw)
                self.assertEqual(result["thoughts"][0]["body"], body + ".")
                self.assertEqual(result["thoughts"][0]["source_quote"], body + ".")
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                                 [("revenue", 1300)])
                self.assertEqual(result["status"], "parsed")

    def test_new_sentences_are_independent_and_do_not_swallow_finance(self):
        for sep in ("。", "；", ";", "！", "\n", "\r\n"):
            result = self.ingest("记个想法：明天试南瓜甜品" + sep + "今天营业额1300元")
            self.assertEqual(result["status"], "parsed")
            self.assertEqual(len(result["entry_ids"]), 1)
            self.assertNotIn("营业额", result["thoughts"][0]["body"])
        result = self.ingest("记个想法：明天试南瓜甜品。然后打算用银耳。南瓜可以搭配木薯，预算300元")
        self.assertEqual(len(result["thoughts"]), 1)
        self.assertEqual(result["entry_ids"], [])
        self.assertEqual(result["status"], "partial")
        self.assertIn("预算300元", result["parsed"]["leftovers"])

    def test_prd_qualitative_flow_is_observed_without_inventing_revenue(self):
        raw = ("今天天气很好，生意还可以，老顾客非常的多，大概做了 1300。"
               "明天我想做一个新品，然后打算用南瓜来做一个甜品。"
               "南瓜甜品可以搭配木薯、银耳、枸杞等。")
        result = self.ingest(raw)
        self.assertEqual(result["entry_ids"], [])
        self.assertEqual(result["sale_ids"], [])
        qualitative = {(o["kind"], o["measure"], o["value_text"], o["value_num"], o["source_span"])
                       for o in result["observations"]}
        self.assertIn(("生意", "好坏", "还可以", None, "生意还可以"), qualitative)
        self.assertIn(("客流", "情况", "老客多", None, "老顾客非常的多"), qualitative)
        self.assertTrue(result["thoughts"])
        self.assertIn("明天我想做一个新品", result["thoughts"][0]["body"])
        self.assertEqual(result["thoughts"][0]["date"], DAY)
        leftovers = result["parsed"]["leftovers"]
        self.assertTrue(any("1300" in item for item in leftovers))
        self.assertTrue(any("天气很好" in item for item in leftovers))
        self.assertFalse(any("生意还可以" in item or "老顾客" in item for item in leftovers))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 0)

    def test_unsupported_note_or_ambiguous_prefix_stays_leftover(self):
        for raw in ("如果明天想试南瓜甜品预算300元", "我想试南瓜甜品12元",
                    "可以搭配木薯300元", "记个想法", "记个想法：", "记个随想：#新品",
                    "记个想法预算300元", "我说记个想法：卖12元", "记个想法：下周预算300元 # "):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["thoughts"], [])
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["sale_ids"], [])
                self.assertTrue(result["parsed"]["leftovers"])

    def test_unsupported_plan_and_price_sentences_stay_unresolved(self):
        for sentence in ("明天计划卖12元", "新品售价12元"):
            with self.subTest(sentence=sentence):
                result = self.ingest("记个想法：明天试新品。" + sentence)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(len(result["thoughts"]), 1)
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["sale_ids"], [])
                self.assertEqual(result["parsed"]["leftovers"], [sentence])
                result = self.ingest("记个想法：明天试新品。" + sentence + "。今天营业额1300元")
                self.assertEqual([(e["date"], e["amount"]) for e in result["parsed"]["entries"]],
                                 [(DAY, 1300)])
                self.assertEqual(result["parsed"]["leftovers"], [sentence])

    def test_ascii_boundaries_keep_saved_notes_out_of_adjacent_leftovers(self):
        raw = "记个想法：试新品. 新品售价12元. 今天营业额1300元"
        result = self.ingest(raw)
        self.assertEqual(result["thoughts"][0]["body"], "试新品.")
        self.assertEqual(result["parsed"]["leftovers"], ["新品售价12元"])
        self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                         [("revenue", 1300)])
        raw = "昨天收入1300元. 记个想法：2027-01-01预算300元. 房租800元"
        result = self.ingest(raw)
        self.assertEqual([(e["date"], e["amount"], e["note"]) for e in result["parsed"]["entries"]],
                         [("2026-10-25", 1300, "昨天收入1300元"), ("2026-10-25", 800, "房租800元")])

    def test_archive_instruction_leaves_note_unsaved_and_explains(self):
        for raw in ("这条随想记在昨天：明天试做，预算300元。今天营业额1300元",
                    "记个想法：明天试做。请把这条随想记在昨天。今天营业额1300元",
                    "这条想法归档到2026-02-30。记个想法：明天试做。今天营业额1300元"):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["thoughts"], [])
                self.assertEqual(result["status"], "partial")
                self.assertEqual([e["amount"] for e in result["parsed"]["entries"]], [1300])
                self.assertTrue(any("外部 Agent" in w for w in result["warnings"]))
                self.assertTrue(result["parsed"]["leftovers"])
                self.assertTrue(all(fragment in raw for fragment in result["parsed"]["leftovers"]))

    def test_approximate_amounts_across_lines_never_become_exact_money(self):
        for sep in ("\n", "\r\n"):
            for approximate in ("大概做了" + sep + "1300元", "花了两百" + sep + "多",
                                "可能收入一千" + sep + "左右"):
                with self.subTest(approximate=approximate):
                    result = self.ingest(approximate)
                    self.assertEqual(result["entry_ids"], [])
                    self.assertEqual(result["sale_ids"], [])
                    self.assertEqual(result["status"], "unparsed")
                    self.assertEqual(result["parsed"]["leftovers"], [approximate])
                    result = self.ingest(approximate + sep + "今天营业额1300元")
                    self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                                     [("revenue", 1300)])
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["parsed"]["leftovers"], [approximate])

    def test_outer_dates_survive_approximate_amounts_without_restoring_note_dates(self):
        for prefix in ("", "前天营业额100元，", "记个想法：2026-02-30预算300元。"):
            with self.subTest(prefix=prefix):
                raw = prefix + "昨天花了两百多，房租800元。客流10人，牛肉面卖了3份30元"
                result = self.ingest(raw)
                rent = next(e for e in result["parsed"]["entries"] if e["category"] == "房租")
                self.assertEqual((rent["date"], rent["amount"]), ("2026-10-25", 800))
                self.assertEqual([(o["occurred_at"], o["value_num"]) for o in result["observations"]],
                                 [("2026-10-25", 10)])
                self.assertEqual(result["parsed"]["sales"][0]["date"], "2026-10-25")
                self.assertEqual(result["record"]["occurred_at"], "2026-10-25")
                self.assertEqual(result["parsed"]["leftovers"], ["昨天花了两百多"])
        result = self.ingest("昨天花了两百多。记个想法：明天预算300元。今天房租800元")
        self.assertEqual(result["parsed"]["entries"][0]["date"], DAY)
        self.assertEqual(result["record"]["occurred_at"], DAY)
        with self.assertRaises(ValueError):
            self.ingest("2026-02-30花了两百多。房租800元", dedup_ref="invalid-outer-date")
        record = self.store.record_find("s", "invalid-outer-date")
        self.assertEqual(record["parse_status"], "rejected")
        self.assertEqual(self.store.record_detail("s", record["id"])["entries"], [])

    def test_controlled_observations_survive_nearby_uncertain_money(self):
        for fact, kind, value in (("今天客流10人", "客流", 10), ("今天外卖10单", "订单", 10),
                                 ("评分4.8", "评价", 4.8)):
            for separator in ("，", "\n", "\r\n", ""):
                raw = "昨天大概做了1300" + separator + fact
                with self.subTest(raw=raw):
                    result = self.ingest(raw)
                    self.assertEqual([(o["kind"], o["value_num"]) for o in result["observations"]],
                                     [(kind, value)])
                    expected_date = DAY if fact.startswith("今天") else "2026-10-25"
                    self.assertEqual(result["observations"][0]["occurred_at"], expected_date)
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["entry_ids"], [])
                    self.assertEqual(result["sale_ids"], [])
                    self.assertEqual(result["parsed"]["leftovers"], ["昨天大概做了1300"])
        for raw in ("大概客流10人", "客流10人左右", "可能外卖10单",
                    "今天营业额大概，只有1300元", "记个想法：客流10人预算300元"):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["entry_ids"], [])

    def test_approximate_suffix_stays_with_previous_amount(self):
        for raw, leftover, amount in (
                ("可能收入一千左右今天营业额1300元", "可能收入一千左右", 1300),
                ("收入1左右营收2", "收入1左右", 2),
                ("收入1元上下今天营收2元", "收入1元上下", 2),
                ("收入1左右可能收入2元", "收入1左右", None)):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                expected = [] if amount is None else [("revenue", amount)]
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]], expected)
                self.assertIn(leftover, result["parsed"]["leftovers"])
                self.assertTrue(all(fragment in raw for fragment in result["parsed"]["leftovers"]))
                self.assertEqual(result["status"], "unparsed" if amount is None else "partial")

    def test_cloudy_weather_is_not_an_approximate_amount_suffix(self):
        for raw in ("今天营业额1300元多云", "今天营业额1300元 多云", "今天营业额1300多云"):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                                 [("revenue", 1300)])
                self.assertEqual([(o["kind"], o["value_text"]) for o in result["observations"]],
                                 [("天气", "多云")])
                self.assertEqual(result["status"], "parsed")
                self.assertEqual(result["parsed"]["leftovers"], [])
        for raw in ("营业额1300元多", "营业额1300多元", "花了两百多", "可能收入一千左右"):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["parsed"]["leftovers"], [raw])

    def test_approximate_amounts_and_exact_continuation(self):
        for approximate in ("大概做了1300", "花了两百多", "可能收入一千左右", "营业额大约1300元",
                            "收入1300左右", "采购花了两百多元"):
            with self.subTest(approximate=approximate):
                raw = approximate + "，今天买了南瓜80元。今天的营业额很差，只有1300"
                result = self.ingest(raw)
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                                 [("purchase", 80), ("revenue", 1300)])
                self.assertEqual(result["status"], "partial")
                self.assertIn(approximate, result["parsed"]["leftovers"])

    def test_uncertain_prefix_after_actual_amount_belongs_to_next_fact(self):
        for suffix in ("计划收入100元", "预算房租800元", "打算采购100元"):
            with self.subTest(suffix=suffix):
                result = self.ingest("记个想法：试新品。今天营业额1300元" + suffix)
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                                 [("revenue", 1300)])
                self.assertEqual(result["parsed"]["leftovers"], [suffix])
                self.assertEqual(result["status"], "partial")

    def test_relative_date_only_does_not_end_uncertain_amount(self):
        for separator in ("\n", "\r\n", "，"):
            raw = separator.join(("大概做了", "昨天", "1300元"))
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["status"], "unparsed")
                self.assertTrue(all(value in raw for value in result["parsed"]["leftovers"]))
                actual = self.ingest(separator.join(("大概做了", "昨天", "营业额1300元")))
                self.assertEqual([(e["date"], e["amount"]) for e in actual["parsed"]["entries"]],
                                 [("2026-10-25", 1300)])

    def test_observation_approximate_suffix_does_not_qualify_actual_revenue(self):
        for approximate in ("今天客流10人左右", "外卖10单左右", "评分4.8分左右"):
            with self.subTest(approximate=approximate):
                result = self.ingest(approximate + "收入1300元")
                self.assertEqual(result["observations"], [])
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                                 [("revenue", 1300)])
                self.assertEqual(result["parsed"]["leftovers"], [approximate])

    def test_conflicting_sale_payment_preserves_original_financial_source(self):
        raw = "房租8000牛肉面卖了3份每份10元合计40元真不错。记个想法：新品"
        result = self.ingest(raw)
        self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                         [("expense", 8000)])
        self.assertIn("合计40元", result["parsed"]["leftovers"])
        detail = self.store.record_detail("s", result["record"]["id"])
        self.assertTrue(all(row["note"] in raw for row in detail["entries"]))
        self.assertTrue(all(row["source_span"] in raw for row in detail["observations"]))
        self.assertTrue(all(value in raw for value in result["parsed"]["leftovers"]))

    def test_invalid_tags_keep_whole_note_and_no_partial_tag_write(self):
        raw = "记个想法：南瓜预算300元 #新品 ##南瓜。今天营业额1300元"
        result = self.ingest(raw)
        self.assertEqual(result["thoughts"], [])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["warnings"])
        self.assertEqual(len(result["entry_ids"]), 1)
        self.assertIn("记个想法：南瓜预算300元 #新品 ##南瓜。", result["parsed"]["leftovers"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM thought_tags").fetchone()[0], 0)

    def test_retry_uses_saved_raw_date_and_never_duplicates_notes(self):
        raw = "昨天营业额1300元。记个想法：明天预算300元 #新品"
        with patch.object(self.store, "add_entries", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.ingest(raw, dedup_ref="retry")
        pending = self.store.record_find("s", "retry")
        self.assertEqual(pending["parse_status"], "pending")
        self.assertEqual(self.store.thought_for_record("s", pending["id"]), [])
        result = observe.ingest(self.store, "s", "替换正文收入9000", base=BASE + dt.timedelta(days=1),
                                dedup_ref="retry")
        self.assertEqual(result["record"]["raw_text"], raw)
        self.assertEqual(result["thoughts"][0]["date"], DAY)
        self.assertEqual(result["parsed"]["entries"][0]["date"], "2026-10-25")
        before = self.store.export_all("s")
        duplicate = self.ingest("ignored", dedup_ref="retry")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["thoughts"], result["thoughts"])
        self.assertEqual(before, self.store.export_all("s"))

    def test_workspace_receive_date_persisted_before_midnight_retry(self):
        self.store.ensure_shop("s", timezone="America/Los_Angeles")
        raw = "今天营业额1300元。记个想法：明天试南瓜甜品"
        with patch.object(self.store, "add_entries", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                observe.ingest(self.store, "s", raw, dedup_ref="zone",
                               received_at="2026-10-26T00:30:00+08:00")
        record = self.store.record_find("s", "zone")
        self.assertEqual(record["parse_base_date"], "2026-10-25")
        self.store.ensure_shop("s", timezone="Asia/Shanghai")
        result = observe.ingest(self.store, "s", raw, dedup_ref="zone",
                                received_at="2026-10-27T00:30:00+08:00")
        self.assertEqual(result["thoughts"][0]["date"], "2026-10-25")
        self.assertEqual(result["record"]["received_at"], "2026-10-26T00:30:00+08:00")

    def test_pure_note_event_date_ignores_unresolved_past_prose(self):
        result = self.ingest("记个想法：明天试新品。昨天生意还不错")
        self.assertEqual(result["record"]["occurred_at"], DAY)
        self.assertEqual(result["thoughts"][0]["date"], DAY)
        self.assertEqual(result["status"], "partial")

    def test_appending_note_preserves_legacy_last_explicit_event_date(self):
        for raw in ("今天营业额1300元。昨天买了南瓜80元", "今天营业额1300元。昨天客流10人"):
            with self.subTest(raw=raw):
                before = self.ingest(raw)
                after = self.ingest(raw + "。记个想法：2027-01-01试新品")
                self.assertEqual(before["record"]["occurred_at"], "2026-10-25")
                self.assertEqual(after["record"]["occurred_at"], before["record"]["occurred_at"])
                self.assertEqual(after["parsed"]["entries"], before["parsed"]["entries"])
                detail = self.store.record_detail("s", after["record"]["id"])
                self.assertEqual(detail["record"]["occurred_at"], "2026-10-25")
                timeline = self.store.record_timeline("s", start="2026-10-25", end="2026-10-25")
                self.assertIn(after["record"]["id"], [row["record"]["id"] for row in timeline["items"]])

    def test_archive_reference_after_note_never_defaults_to_today(self):
        result = self.ingest("记个想法：明天试南瓜甜品。请记在昨天。今天营业额1300元")
        self.assertEqual(result["thoughts"], [])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["entry_ids"]), 1)
        self.assertIn("请记在昨天。", result["parsed"]["leftovers"])
        self.assertTrue(result["warnings"])

    def test_approximate_continuation_and_same_clause_exact_anchor(self):
        for raw, expected, leftover in (
                ("今天营业额大概，只有1300元", [], "只有1300元"),
                ("大概做了1300元今天买了南瓜80元", [("purchase", 80)], "大概做了1300元"),
                ("今天买了南瓜80元可能收入一千左右", [("purchase", 80)], "可能收入一千左右"),
                ("记个想法：南瓜甜品。下周预算300元，卖12元", [], "卖12元")):
            with self.subTest(raw=raw):
                result = self.ingest(raw)
                self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]], expected)
                self.assertIn(leftover, result["parsed"]["leftovers"])

    def test_direct_call_without_workspace_metadata_supports_thoughts(self):
        for call in (observe.observe, observe.ingest):
            with self.subTest(call=call.__name__):
                result = call(self.store, call.__name__, "记个想法：明天试南瓜甜品", base=BASE)
                self.assertEqual(result["status"], "parsed")
                self.assertEqual(len(result["thoughts"]), 1)

    def test_direct_observe_then_ingest_reuses_note(self):
        raw = "今天营业额1300元。记个想法：明天试南瓜甜品 #新品"
        first = observe.observe(self.store, "s", raw, base=BASE, dedup_ref="direct")
        self.assertEqual(len(first["thoughts"]), 1)
        result = self.ingest(raw, dedup_ref="direct")
        self.assertEqual(result["thoughts"], first["thoughts"])
        self.assertEqual(len(result["entry_ids"]), 1)

    def test_completed_native_retry_precedes_current_profile_gate(self):
        for call in (observe.observe, observe.ingest):
            with self.subTest(call=call.__name__):
                self.store.workspace_set_profile("s", "restaurant")
                ref = "profile-retry-" + call.__name__
                first = call(self.store, "s", "记个想法：明天试新品", base=BASE, dedup_ref=ref)
                self.store.workspace_set_profile("s", "retail")
                before = self.store.export_all("s")
                retry = call(self.store, "s", "替换正文", base=BASE, dedup_ref=ref)
                self.assertTrue(retry["duplicate"])
                self.assertEqual(retry["status"], "parsed")
                self.assertEqual(retry["thoughts"], first["thoughts"])
                self.assertEqual(self.store.export_all("s"), before)
                fresh = call(self.store, "s", "记个想法：明天试新品", base=BASE)
                self.assertEqual(fresh["status"], "needs_confirmation")
                self.assertEqual(fresh["thoughts"], [])
                for status in ("parsing", "rejected", "pending"):
                    ref = call.__name__ + status
                    self.store.record_add("s", "记个想法：明天试新品", dedup_ref=ref,
                                          occurred_at=DAY, parse_status=status)
                    if status == "rejected":
                        with self.assertRaises(ValueError):
                            call(self.store, "s", "ignored", base=BASE, dedup_ref=ref)
                    else:
                        result = call(self.store, "s", "ignored", base=BASE, dedup_ref=ref)
                        self.assertEqual(result["status"], "parsing" if status == "parsing" else "needs_confirmation")
                    stored = self.store.record_find("s", ref)
                    self.assertEqual(stored["parse_status"], "needs_confirmation" if status == "pending" else status)

    def test_nonrestaurant_gate_applies_to_direct_observe_and_ingest(self):
        self.store.ensure_shop("other", profile_id="retail")
        for call in (observe.observe, observe.ingest):
            result = call(self.store, "other", "记个想法：明天试新品 #新品", base=BASE)
            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["thoughts"], [])
            self.assertEqual(self.store.thought_for_record("other", result["record"]["id"]), [])

    def test_explicit_nonrestaurant_profile_without_metadata_is_gated(self):
        for call in (observe.observe, observe.ingest):
            result = call(self.store, "no-meta", "记个想法：明天试新品", base=BASE,
                          profile_id="retail")
            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["thoughts"], [])
            self.assertIsNone(self.store.workspace_get("no-meta"))

    def test_source_quote_for_multiple_notes_and_literal_markdown(self):
        raw = "记个想法：可能试12.5元的新品，[说明](https://example.invalid/?price=300元)，`预算300元;收入900元`。记个随想：还没确定 #新品"
        result = self.ingest(raw)
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(len(result["thoughts"]), 2)
        self.assertEqual(result["entry_ids"], [])
        self.assertTrue(all(row["source_quote"] in raw for row in result["thoughts"]))
        self.assertIn("https://example.invalid", result["thoughts"][0]["body"])

    def test_thought_failure_rolls_back_observations_and_tags(self):
        original = self.store.thought_add
        def fail_after_write(*args):
            original(*args)
            raise RuntimeError("injected after tags")
        with patch.object(self.store, "thought_add", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                self.ingest("今天阴天，营业额1300元。记个想法：明天试做 #新品", dedup_ref="atomic")
        self.assertEqual(self.store.observation_list("s", "0000", "9999"), [])
        self.assertEqual(self.store.list_entries("s", "0000", "9999"), [])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM thoughts").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM thought_tags").fetchone()[0], 0)
        self.assertEqual(self.store.record_find("s", "atomic")["parse_status"], "pending")


if __name__ == "__main__":
    unittest.main()
