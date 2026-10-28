"""随口记引擎测试。

跑：cd 随口记经营助手 && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import parser, sheet  # noqa: E402
from engine.cli import main as cli_main  # noqa: E402
from engine.cost import parse_recipe_text, product_cost, set_recipe, usage_variance  # noqa: E402
from engine.importer import commit_import, plan_import  # noqa: E402
from engine.model import convert, parse_date, to_grams  # noqa: E402
from engine.reconcile import scan  # noqa: E402
from engine.store import SqliteStore, sha256_file  # noqa: E402


# ---------------------------------------------------------------- 工具


def make_xlsx(path: str, rows: list) -> None:
    """手写一个最小 xlsx，用来测读取（不依赖 openpyxl）。"""
    shared, sheet_rows = [], []

    def ref(r, c):
        s, c = "", c + 1
        while c:
            c, rem = divmod(c - 1, 26)
            s = chr(65 + rem) + s
        return f"{s}{r}"

    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, val in enumerate(row):
            shared.append(str(val))
            cells.append(f'<c r="{ref(ri, ci)}" t="s"><v>{len(shared) - 1}</v></c>')
        sheet_rows.append(f'<row r="{ri}">' + "".join(cells) + "</row>")

    sst = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           + "".join(f"<si><t>{v}</t></si>" for v in shared) + "</sst>")
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 "<sheetData>" + "".join(sheet_rows) + "</sheetData></worksheet>")
    workbook = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
            'Target="sharedStrings.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types></Types>")
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def run_cli(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_main(list(argv))
    return buf.getvalue()


class TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()


# ---------------------------------------------------------------- 中文数字


class TestChineseNumbers(unittest.TestCase):
    def test_oral_ellipsis(self):
        # 口语省略是中文记账最容易错的地方
        self.assertEqual(parser.cn_to_number("一千二"), 1200)
        self.assertEqual(parser.cn_to_number("三百五"), 350)
        self.assertEqual(parser.cn_to_number("一万二"), 12000)

    def test_plain(self):
        self.assertEqual(parser.cn_to_number("五十"), 50)
        self.assertEqual(parser.cn_to_number("十五"), 15)
        self.assertEqual(parser.cn_to_number("两千"), 2000)
        self.assertEqual(parser.cn_to_number("三百零五"), 305)


class TestUnits(unittest.TestCase):
    def test_weight_conversion(self):
        self.assertEqual(to_grams(1, "斤"), 500)
        self.assertEqual(to_grams(1, "公斤"), 1000)
        self.assertEqual(convert(1, "斤", "g"), 500)
        self.assertEqual(convert(500, "g", "斤"), 1)
        self.assertEqual(convert(2, "kg", "斤"), 4)

    def test_non_convertible(self):
        # 份和斤不是同族，不能硬换
        self.assertIsNone(convert(1, "份", "斤"))

    def test_same_unit_passthrough(self):
        self.assertEqual(convert(3, "份", "份"), 3)


class TestDate(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("2026-10-23"), "2026-10-23")
        self.assertEqual(parse_date("2026/10/23"), "2026-10-23")
        self.assertEqual(parse_date("20261023"), "2026-10-23")

    def test_bad(self):
        self.assertIsNone(parse_date("不是日期"))
        self.assertIsNone(parse_date("2026-13-45"))


# ---------------------------------------------------------------- 解析


class TestParser(unittest.TestCase):
    def test_multi_clause(self):
        r = parser.parse("今天卖了1280，买了50斤牛肉花了680，交房租8000")
        self.assertEqual(r["intent"], "entries")
        self.assertEqual(len(r["entries"]), 3)
        kinds = {e["type"]: e for e in r["entries"]}
        self.assertEqual(kinds["revenue"]["amount"], 1280)
        self.assertEqual(kinds["purchase"]["amount"], 680)
        self.assertEqual(kinds["purchase"]["qty"], 50)
        self.assertEqual(kinds["purchase"]["unit"], "斤")
        self.assertEqual(kinds["purchase"]["item"], "牛肉")
        self.assertEqual(kinds["expense"]["category"], "房租")

    def test_type_switch_in_one_clause(self):
        r = parser.parse("昨天营业额1200成本350")
        self.assertEqual(len(r["entries"]), 2)
        # 后半段不带日期，必须继承前半段的「昨天」
        self.assertEqual({e["date"] for e in r["entries"]}, {r["entries"][0]["date"]})
        self.assertEqual(r["entries"][0]["date"], parser.shift_days(-1))

    def test_quantity_not_mistaken_for_amount(self):
        # 50 是数量不是金额，不能因此把句子切成两笔
        r = parser.parse("买了三箱啤酒花了240")
        self.assertEqual(len(r["entries"]), 1)
        self.assertEqual(r["entries"][0]["amount"], 240)
        self.assertEqual(r["entries"][0]["qty"], 3)
        self.assertEqual(r["entries"][0]["unit"], "箱")
        self.assertEqual(r["entries"][0]["type"], "purchase")

    def test_sales_detected(self):
        r = parser.parse("牛肉面卖了30份，一份12块")
        self.assertEqual(r["entries"], [])
        self.assertEqual(len(r["sales"]), 1)
        self.assertEqual(r["sales"][0]["product_name"], "牛肉面")
        self.assertEqual(r["sales"][0]["qty"], 30)
        self.assertEqual(r["sales"][0]["amount"], 360)

    def test_sales_without_price(self):
        for text in ("今天牛肉面卖了30份", "卖了30份牛肉面"):
            r = parser.parse(text)
            self.assertEqual(len(r["sales"]), 1, text)
            self.assertEqual(r["sales"][0]["product_name"], "牛肉面", text)
            self.assertEqual(r["sales"][0]["qty"], 30, text)

    def test_sale_total_is_not_a_second_expense(self):
        # 「30份300元」的 300 是这笔销量的合计，不能再记一笔支出
        for text in ("牛肉面卖了30份300元", "卖了30份牛肉面300元", "牛肉面卖了30份，300元",
                     "牛肉面卖了30份，总计300元", "牛肉面卖了30份，合计是300元",
                     "卖了30份牛肉面三百元", "卖了30份牛肉面300 元"):
            with self.subTest(text=text):
                r = parser.parse(text)
                self.assertEqual(r["entries"], [])
                self.assertEqual(len(r["sales"]), 1)
                self.assertEqual(r["sales"][0]["product_name"], "牛肉面")
                self.assertEqual(r["sales"][0]["qty"], 30)
                self.assertEqual(r["sales"][0]["amount"], 300)

    def test_sale_total_does_not_swallow_spending_after_amount(self):
        for text, expected in (("牛肉面卖了30份，300元成本", "purchase"),
                               ("牛肉面卖了30份，500块交房租", "expense"),
                               ("牛肉面卖了30份，300元买菜", "purchase")):
            with self.subTest(text=text):
                r = parser.parse(text)
                self.assertIsNone(r["sales"][0]["amount"])
                self.assertEqual(r["entries"][0]["type"], expected)

    def test_quantity_first_sale_accepts_unit_price_and_preserves_prior_total(self):
        r = parser.parse("卖了30份牛肉面，一份卖12块")
        self.assertEqual(r["entries"], [])
        self.assertEqual(r["sales"][0]["amount"], 360)
        r = parser.parse("今天营业额1280 牛肉面卖了30份300元")
        self.assertEqual(r["entries"][0]["amount"], 1280)
        self.assertEqual(r["sales"][0]["amount"], 300)

    def test_no_space_accounting_prefix_preserves_sale_span(self):
        for prefix in ("营业额", "销售额", "流水"):
            text = f"今天{prefix}1280牛肉面卖了30份"
            with self.subTest(prefix=prefix):
                r = parser.parse(text)
                self.assertEqual([(e["type"], e["amount"]) for e in r["entries"]],
                                 [("revenue", 1280)])
                self.assertEqual([(s["product_name"], s["qty"], s["amount"])
                                  for s in r["sales"]], [("牛肉面", 30, None)])
                sale = r["sales"][0]
                self.assertEqual(text[sale["start"]:sale["end"]], "牛肉面卖了30份")
        text = "今天营业额1280牛肉面卖了30份300元"
        r = parser.parse(text)
        self.assertEqual(r["entries"][0]["amount"], 1280)
        self.assertEqual(r["sales"][0]["amount"], 300)
        sale = r["sales"][0]
        self.assertEqual(text[sale["start"]:sale["end"]], "牛肉面卖了30份300元")

    def test_amount_after_other_words_still_belongs_to_the_expense(self):
        r = parser.parse("牛肉面卖了30份，买了牛肉300元")
        self.assertEqual(r["sales"][0]["amount"], None)
        self.assertEqual([(e["type"], e["item"], e["amount"]) for e in r["entries"]],
                         [("purchase", "牛肉", 300)])

    def test_unseparated_purchase_and_sale_keep_both_spans(self):
        r = parser.parse("买了50斤牛肉花了680牛肉面卖了30份")
        self.assertEqual([(e["type"], e["item"], e["qty"], e["unit"], e["amount"])
                          for e in r["entries"]],
                         [("purchase", "牛肉", 50, "斤", 680)])
        self.assertEqual([(s["product_name"], s["qty"], s["amount"])
                          for s in r["sales"]],
                         [("牛肉面", 30, None)])

    def test_revenue_not_mistaken_for_sale(self):
        # 「卖了1280」是营业额，不是 1280 份
        r = parser.parse("今天卖了1280")
        self.assertEqual(r["sales"], [])
        self.assertEqual(r["entries"][0]["type"], "revenue")
        self.assertEqual(r["entries"][0]["amount"], 1280)

    def test_query(self):
        self.assertEqual(parser.parse("这个月赚了多少")["period"], "month")
        self.assertEqual(parser.parse("今天怎么样")["period"], "today")
        self.assertEqual(parser.parse("这周毛利多少")["period"], "week")

    def test_unknown(self):
        self.assertEqual(parser.parse("嗯好的")["intent"], "unknown")
        self.assertEqual(parser.parse("")["intent"], "unknown")

    def test_non_money_count_is_not_an_amount(self):
        """「客流120人」不能记成一笔 120 元的支出。宁可记不下，不可记错。"""
        r = parser.parse("今天客流120人")
        self.assertEqual(r["intent"], "unknown")
        self.assertEqual(r["entries"], [])

    def test_money_and_headcount_in_one_sentence(self):
        r = parser.parse("今天营业额1280，客流120人")
        self.assertEqual(len(r["entries"]), 1)
        self.assertEqual(r["entries"][0]["amount"], 1280)
        # 「客流120人」这段没被吞掉，而是明说是没看懂的片段
        self.assertTrue(any("客流" in s for s in r["leftovers"]))

    def test_people_count_still_works_for_labour(self):
        r = parser.parse("今天请了2个人发工资600")
        self.assertEqual(len(r["entries"]), 1)
        self.assertEqual(r["entries"][0]["amount"], 600)
        self.assertEqual(r["entries"][0]["category"], "人工")

    def test_chinese_amount(self):
        r = parser.parse("交房租一千二")
        self.assertEqual(r["entries"][0]["amount"], 1200)
        self.assertEqual(r["entries"][0]["category"], "房租")

    def test_fractional_money_does_not_merge_adjacent_amounts(self):
        self.assertEqual([a["value"] for a in parser.find_amounts("12元3元")], [12, 3])
        self.assertEqual([a["value"] for a in parser.find_amounts("12元三百元")], [12, 300])
        self.assertEqual([a["value"] for a in parser.find_amounts("12元3千")], [12, 3000])
        self.assertEqual(parser.find_amounts("花了一块五")[0]["value"], 1.5)
        self.assertEqual(parser.find_amounts("花了十二元五角")[0]["value"], 12.5)


# ---------------------------------------------------------------- 存储


class TestStore(TempDb):
    def test_summary(self):
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        store.add_entries([
            {"shop_id": "s", "date": "2026-10-01", "type": "revenue",
             "category": "营业额", "amount": 1000},
            {"shop_id": "s", "date": "2026-10-01", "type": "purchase",
             "category": "食材成本", "amount": 400},
            {"shop_id": "s", "date": "2026-10-01", "type": "expense",
             "category": "房租", "amount": 200},
        ])
        s = store.summary("s", "2026-10-01", "2026-10-01")
        self.assertEqual(s["revenue"], 1000)
        self.assertEqual(s['cash_balance'], 400)
        self.assertIsNone(s['actual_cost'])
        self.assertIsNone(s['gross'])
        self.assertIsNone(s['net'])
        self.assertIsNone(s['margin'])
        self.assertEqual(s['fixed'], 200)
        self.assertIsNone(s['break_even'])

    def test_no_revenue_gives_no_break_even(self):
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        store.add_entries([{"shop_id": "s", "date": "2026-10-01", "type": "expense",
                            "category": "房租", "amount": 200}])
        self.assertIsNone(store.summary("s", "2026-10-01", "2026-10-01")["break_even"])


# ---------------------------------------------------------------- 成本


class TestCost(TempDb):
    def _shop_with_data(self):
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        # 买 50 斤牛肉 680 元 -> 13.6 元/斤 = 0.0272 元/克
        store.add_entries([{"shop_id": "s", "date": "2026-10-01", "type": "purchase",
                            "category": "食材成本", "amount": 680,
                            "item": "牛肉", "qty": 50, "unit": "斤"}])
        return store

    def test_recipe_text(self):
        lines = parse_recipe_text("牛肉80克，面条120克，汤料20克")
        self.assertEqual([(l["name"], l["qty"], l["unit"]) for l in lines],
                         [("牛肉", 80, "g"), ("面条", 120, "g"), ("汤料", 20, "g")])

    def test_unit_cost_across_units(self):
        store = self._shop_with_data()
        set_recipe(store, "s", "牛肉面", "牛肉80克", price=12)
        product = store.product_find("s", "牛肉面")
        result = product_cost(store, "s", product)
        # 680/50 斤 = 13.6 元/斤；80 克 = 0.16 斤 -> 2.176 元
        self.assertAlmostEqual(result["unit_cost"], 2.18, places=2)
        self.assertAlmostEqual(result["gross"], 9.82, places=2)
        self.assertTrue(result["complete"])

    def test_missing_price_is_reported_not_guessed(self):
        store = self._shop_with_data()
        set_recipe(store, "s", "牛肉面", "牛肉80克，神秘酱料10克", price=12)
        result = product_cost(store, "s", store.product_find("s", "牛肉面"))
        self.assertFalse(result["complete"])
        self.assertEqual([m["name"] for m in result["missing"]], ["神秘酱料"])
        # 缺价的原料成本为 None，不能当成 0 混进总成本
        self.assertIsNone(result["lines"][1]["cost"])

    def test_usage_variance(self):
        store = self._shop_with_data()
        set_recipe(store, "s", "牛肉面", "牛肉80克", price=12)
        p = store.product_find("s", "牛肉面")
        store.add_sales("s", [{"date": "2026-10-01", "product_id": p["id"],
                               "product_name": "牛肉面", "qty": 100, "amount": 1200}])
        rows = usage_variance(store, "s", "2026-10-01", "2026-10-01")
        self.assertEqual(len(rows), 1)
        # 卖 100 份 × 80 克 = 8000 克 = 16 斤；实际买了 50 斤 -> 多 34 斤
        self.assertAlmostEqual(rows[0]["theoretical"], 16, places=2)
        self.assertAlmostEqual(rows[0]["actual"], 50, places=2)
        self.assertGreater(rows[0]["variance"], 0.15)


# ---------------------------------------------------------------- 导入


class TestSheet(unittest.TestCase):
    def test_csv_gbk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.csv")
            with open(path, "wb") as fh:
                fh.write("日期,金额\n2026-10-01,1234\n".encode("gb18030"))
            rows = sheet.read_csv(path)
            self.assertEqual(rows[0], ["日期", "金额"])
            self.assertEqual(rows[1], ["2026-10-01", "1234"])

    def test_xlsx(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.xlsx")
            make_xlsx(path, [["日期", "金额"], ["2026-10-01", "1234"]])
            rows = sheet.read_xlsx(path)[0]
            self.assertEqual(rows[0], ["日期", "金额"])
            self.assertEqual(rows[1], ["2026-10-01", "1234"])

    def test_xls_rejected_with_advice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.xls")
            open(path, "wb").close()
            with self.assertRaises(ValueError) as ctx:
                sheet.read_table(path)
            self.assertIn("另存为", str(ctx.exception))

    def test_entity_and_oversized_column_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entity.xlsx")
            _package(path, {"xl/workbook.xml": b"<!DOCTYPE x [<!ENTITY a 'x'>]><workbook>&a;</workbook>"})
            with self.assertRaises(ValueError) as ctx:
                sheet.read_xlsx(path)
            self.assertIn("实体", str(ctx.exception))
            path = os.path.join(tmp, "wide.xlsx")
            _package(path, {"xl/worksheets/sheet1.xml": b'<worksheet><sheetData><row r="1"><c r="XFE1"><v>1</v></c></row></sheetData></worksheet>'})
            with self.assertRaises(ValueError) as ctx:
                sheet.read_xlsx(path)
            self.assertIn("XFD", str(ctx.exception))


def _package(path, members):
    workbook = ('<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("xl/workbook.xml", members.get("xl/workbook.xml", workbook.encode()))
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", members.get("xl/worksheets/sheet1.xml", b"<worksheet></worksheet>"))


class TestImport(TempDb):
    def _csv(self, text, name="s.csv"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_sales_table(self):
        path = self._csv("日期,商品,数量,金额\n2026-10-01,牛肉面,30,360\n"
                         "2026-10-01,小份牛肉面,12,108\n")
        plan = plan_import(path)
        self.assertEqual(plan["kind"], "sales")
        self.assertEqual(plan["row_count"], 2)
        self.assertEqual(plan["rows"][0]["product_name"], "牛肉面")
        self.assertEqual(plan["rows"][0]["qty"], 30)

    def test_entries_table_with_type(self):
        path = self._csv("日期,类型,分类,金额\n2026-10-01,支出,房租,8000\n"
                         "2026-10-01,收入,营业额,1280\n")
        plan = plan_import(path)
        self.assertEqual(plan["kind"], "entries")
        self.assertEqual(plan["rows"][0]["type"], "expense")
        self.assertEqual(plan["rows"][1]["type"], "revenue")

    def test_amount_formats(self):
        path = self._csv("日期,类型,金额\n2026-10-01,支出,\"¥1,234.50\"\n")
        plan = plan_import(path)
        self.assertEqual(plan["rows"][0]["amount"], 1234.50)

    def test_bad_rows_reported_not_dropped_silently(self):
        path = self._csv("日期,类型,金额\n不是日期,支出,100\n2026-10-01,支出,200\n")
        plan = plan_import(path)
        self.assertEqual(plan["row_count"], 1)
        self.assertEqual(len(plan["skipped"]), 1)
        self.assertEqual(plan["skipped"][0]["row"], 2)

    def test_duplicate_import_blocked(self):
        path = self._csv("日期,商品,数量,金额\n2026-10-01,牛肉面,30,360\n")
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        plan = plan_import(path)
        sha = sha256_file(path)
        first = commit_import(store, "s", plan, "s.csv", sha)
        second = commit_import(store, "s", plan, "s.csv", sha)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["imported"], 0)
        self.assertEqual(len(store.sales_by_product("s", "2026-10-01", "2026-10-01")), 1)


# ---------------------------------------------------------------- 对账


class TestReconcile(TempDb):
    def test_finds_missing_recipe_and_variance(self):
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        store.add_entries([
            {"shop_id": "s", "date": "2026-10-01", "type": "purchase",
             "category": "食材成本", "amount": 680, "item": "牛肉", "qty": 50, "unit": "斤"},
        ])
        set_recipe(store, "s", "牛肉面", "牛肉80克", price=12)
        p = store.product_find("s", "牛肉面")
        # 凉拌黄瓜建了商品但没配方 -> missing_recipe；
        # 完全没建过的商品名才会报 unmapped_product
        store.product_upsert("s", "凉拌黄瓜", price=8)
        store.add_sales("s", [
            {"date": "2026-10-01", "product_id": p["id"], "product_name": "牛肉面",
             "qty": 100, "amount": 1200},
            {"date": "2026-10-01", "product_id": None, "product_name": "凉拌黄瓜",
             "qty": 10, "amount": 80},
        ])
        findings = scan(store, "s", "2026-10-01", "2026-10-01")
        kinds = {f["kind"] for f in findings}
        self.assertIn("usage_variance", kinds)      # 牛肉用超了
        self.assertIn("missing_recipe", kinds)      # 凉拌黄瓜没配方

    def test_unbuilt_product_reported_as_unmapped(self):
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        store.add_sales("s", [{"date": "2026-10-01", "product_id": None,
                               "product_name": "凉拌黄瓜", "qty": 10, "amount": 80}])
        kinds = {f["kind"] for f in scan(store, "s", "2026-10-01", "2026-10-01")}
        self.assertIn("unmapped_product", kinds)

    def test_clean_data_raises_nothing(self):
        store = SqliteStore(self.db)
        store.ensure_shop("s")
        store.add_entries([
            {"shop_id": "s", "date": "2026-10-01", "type": "revenue",
             "category": "营业额", "amount": 1200},
            {"shop_id": "s", "date": "2026-10-01", "type": "purchase",
             "category": "食材成本", "amount": 680, "item": "牛肉", "qty": 16, "unit": "斤"},
        ])
        set_recipe(store, "s", "牛肉面", "牛肉80克", price=12)
        p = store.product_find("s", "牛肉面")
        store.add_sales("s", [{"date": "2026-10-01", "product_id": p["id"],
                               "product_name": "牛肉面", "qty": 100, "amount": 1200}])
        self.assertEqual(scan(store, "s", "2026-10-01", "2026-10-01"), [])


# ---------------------------------------------------------------- 端到端


class TestCliEndToEnd(TempDb):
    def _args(self, *rest):
        return ("--db", self.db, "--shop", "s") + rest

    def test_record_then_report(self):
        out = run_cli(*self._args("record", "今天卖了1280，买了50斤牛肉花了680，交房租8000"))
        self.assertIn("已记账", out)
        self.assertIn("牛肉", out)

        today = run_cli(*self._args("today"))
        self.assertIn("1,280", today)
        self.assertIn("680", today)
        self.assertIn("8,000", today)

    def test_record_sales_then_cost(self):
        run_cli(*self._args("record", "买了50斤牛肉花了680"))
        run_cli(*self._args("recipe", "牛肉面", "牛肉80克", "--price", "12"))
        run_cli(*self._args("record", "牛肉面卖了100份"))
        out = run_cli(*self._args("cost", "牛肉面"))
        self.assertIn("牛肉", out)
        self.assertIn("2.18", out)      # 单品成本

    def test_unparsed_does_not_write_and_hands_off(self):
        before = run_cli(*self._args("entries"))
        out = run_cli(*self._args("record", "嗯好的"))
        self.assertIn("未入账", out)            # 用业务语言说明结果和核对方式
        self.assertNotIn("add-json", out)
        after = run_cli(*self._args("entries"))
        self.assertEqual(before, after)         # 没有偷偷写入

    def test_add_json_path(self):
        payload = json.dumps([{"date": "2026-10-01", "type": "purchase",
                               "amount": 680, "item": "牛肉", "qty": 50, "unit": "斤"}])
        out = run_cli(*self._args("add-json", payload))
        self.assertIn("已记账", out)
        self.assertIn("牛肉", run_cli(*self._args("entries", "--date", "2026-10-01")))

    def test_add_json_rejects_bad_type(self):
        out = run_cli(*self._args("add-json", '[{"date":"2026-10-01","type":"乱写","amount":1}]'))
        self.assertIn("type", out)

    def test_undo(self):
        run_cli(*self._args("record", "今天卖了1280"))
        listing = run_cli(*self._args("entries"))
        entry_id = listing.split("`")[1]
        run_cli(*self._args("undo", entry_id))
        self.assertIn("没有记录", run_cli(*self._args("entries")))

    def test_check_and_answer(self):
        run_cli(*self._args("record", "买了50斤牛肉花了680"))
        run_cli(*self._args("recipe", "牛肉面", "牛肉80克", "--price", "12"))
        run_cli(*self._args("record", "牛肉面卖了100份"))
        out = run_cli(*self._args("check", "--period", "today"))
        self.assertIn("多", out)
        disc_id = out.split("`")[1]
        run_cli(*self._args("answer", disc_id, "是损耗"))
        after = run_cli(*self._args("questions"))
        self.assertIn("没有", after)

    def test_import_then_commit(self):
        path = os.path.join(self.tmp.name, "sales.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("日期,商品,数量,金额\n2026-10-01,牛肉面,30,360\n")

        # 表里的日期是 10-01，报表要按整月看才盖得住
        month = ("report", "product", "--period", "month")
        preview = run_cli(*self._args("import", path))
        self.assertIn("销售明细", preview)
        self.assertIn("--commit", preview)          # 预览阶段不入库
        self.assertIn("没有销售明细", run_cli(*self._args(*month)))

        committed = run_cli(*self._args("import", path, "--commit"))
        self.assertIn("已入库", committed)
        self.assertIn("牛肉面", run_cli(*self._args(*month)))

    def test_seed_and_month(self):
        run_cli(*self._args("seed"))
        out = run_cli(*self._args("month"))
        self.assertIn("毛利率", out)
        self.assertIn("保本点", out)
        cost = run_cli(*self._args("cost"))
        self.assertIn("牛肉面", cost)

    def test_every_read_command_can_emit_json(self):
        """机器人通道靠 JSON 拿数据，不能有命令偷偷只吐 Markdown。"""
        run_cli(*self._args("seed"))
        commands = [
            ("today",), ("week",), ("month",), ("cost",), ("entries",),
            ("questions",), ("product", "list"),
            ("report", "day"), ("report", "product", "--period", "month"),
        ]
        for argv in commands:
            with self.subTest(cmd=argv):
                out = run_cli(*self._args(*argv, "--json"))
                self.assertIsInstance(json.loads(out), dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
