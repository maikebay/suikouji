"""Keyword lookup of ledger entries: database filter plus server-side totals."""
from __future__ import annotations

import unittest
from pathlib import Path
from urllib.parse import quote

from engine.model import round2
from engine.store import SqliteStore
from tests import test_agent_http_contract as http_contract


ROOT = Path(__file__).resolve().parents[1]


class EntryKeywordQueryTests(http_contract.AgentHttpContractTests):
    def seed(self):
        store = SqliteStore(str(self.db))
        try:
            store.ensure_shop("other", "别的店", owner_id="owner", profile_id="restaurant")
            ids = store.add_entries([
                {"shop_id": "shop", "date": "2025-03-12", "type": "expense", "category": "服务费",
                 "amount": 2000, "item": "代理记账", "note": "2025年代账"},
                {"shop_id": "shop", "date": "2026-03-30", "type": "expense", "category": "服务费",
                 "amount": 2000, "item": "代理记账", "note": "2026年代账"},
                {"shop_id": "shop", "date": "2026-04-01", "type": "expense", "category": "房租",
                 "amount": 2000, "item": "房租", "note": "代理记账"},
                {"shop_id": "shop", "date": "2026-05-02", "type": "expense", "category": "办公",
                 "amount": 500, "item": "记账"},
                {"shop_id": "shop", "date": "2026-06-01", "type": "expense", "category": "代理记账",
                 "amount": 900, "item": "咨询费"},
                {"shop_id": "shop", "date": "2026-08-01", "type": "expense", "category": "服务费",
                 "amount": 10, "item": "Bookkeeping"},
                {"shop_id": "other", "date": "2026-03-30", "type": "expense", "category": "服务费",
                 "amount": 2000, "item": "代理记账"},
            ])
            void_id = store.add_entries([{
                "shop_id": "shop", "date": "2024-06-01", "type": "expense", "category": "服务费",
                "amount": 2000, "item": "代理记账",
            }])[0]
            store.void_entry(void_id, actor="owner", reason="作废样本")
            return ids, void_id
        finally:
            store.close()

    def database_summary(self, extra="", args=()):
        store = SqliteStore(str(self.db))
        try:
            where = "shop_id=? AND voided_at IS NULL"
            params = ["shop", *args]
            if extra:
                where += " AND " + extra
            total = store.conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM entry WHERE {where}",
                params).fetchone()
            years = store.conn.execute(
                f"""SELECT substr(date, 1, 4), COUNT(*), COALESCE(SUM(amount), 0)
                    FROM entry WHERE {where}
                    GROUP BY substr(date, 1, 4) ORDER BY 1""",
                params).fetchall()
            return total, years
        finally:
            store.close()

    def assert_matches_database(self, body, extra="", args=()):
        total, years = self.database_summary(extra, args)
        summary = body["summary"]
        self.assertEqual(summary["count"], total[0])
        self.assertEqual(summary["total"], round2(total[1]))
        self.assertEqual(
            [(row["year"], row["count"], row["total"]) for row in summary["by_year"]],
            [(year, count, round2(amount)) for year, count, amount in years])
        self.assertEqual(summary["count"], len(body["entries"]))
        self.assertEqual(summary["total"], round2(sum(row["amount"] for row in body["entries"])))

    def test_keyword_hits_two_bookkeeping_entries_and_yearly_totals(self):
        ids, void_id = self.seed()
        status, _, body = self.request(
            "GET", "/v1/entries?workspace_id=shop&q=" + quote("代理记账"))
        self.assertEqual(status, 200)
        self.assertEqual(
            [(row["date"], row["item"], row["amount"], row["category"]) for row in body["entries"]],
            [("2025-03-12", "代理记账", 2000, "服务费"),
             ("2026-03-30", "代理记账", 2000, "服务费")])
        self.assertEqual([row["id"] for row in body["entries"]], ids[:2])
        self.assertNotIn(void_id, [row["id"] for row in body["entries"]])
        self.assertEqual(body["summary"]["count"], 2)
        self.assertEqual(body["summary"]["total"], 4000)
        self.assertEqual(body["summary"]["by_year"], [
            {"year": "2025", "count": 1, "total": 2000},
            {"year": "2026", "count": 1, "total": 2000},
        ])
        self.assertEqual(body["summary"]["by_type"], [
            {"type": "expense", "count": 2, "total": 4000},
        ])
        self.assertEqual(body["summary"]["by_category"], [
            {"category": "服务费", "count": 2, "total": 4000},
        ])
        self.assertEqual(body["filters"], {
            "q": "代理记账", "start": None, "end": None, "type": None, "category": None,
        })
        self.assert_matches_database(
            body, "instr(unicode_casefold(item), ?) > 0", ("代理记账".casefold(),))
        # Same amount, the shorter name, a category that only contains the word,
        # a note that contains it, and another workspace stay out of this result.
        self.assertTrue(all(row["item"] == "代理记账" for row in body["entries"]))
        self.assertNotIn("房租", [row["item"] for row in body["entries"]])
        self.assertNotIn("咨询费", [row["item"] for row in body["entries"]])

    def test_time_and_category_filters_change_the_database_total(self):
        self.seed()
        status, _, year = self.request(
            "GET", "/v1/entries?workspace_id=shop&q=" + quote("代理记账")
            + "&start=2026-01-01&end=2026-12-31")
        self.assertEqual(status, 200)
        self.assertEqual(
            [(row["date"], row["amount"]) for row in year["entries"]],
            [("2026-03-30", 2000)])
        self.assertEqual(year["summary"]["total"], 2000)
        self.assertEqual(year["summary"]["by_year"], [
            {"year": "2026", "count": 1, "total": 2000},
        ])
        self.assert_matches_database(
            year,
            "date>=? AND date<=? AND instr(unicode_casefold(item), ?) > 0",
            ("2026-01-01", "2026-12-31", "代理记账".casefold()))

        status, _, named = self.request(
            "GET", "/v1/entries?workspace_id=shop&category=" + quote("代理记账"))
        self.assertEqual(status, 200)
        self.assertEqual(
            [(row["item"], row["amount"]) for row in named["entries"]],
            [("咨询费", 900)])
        self.assertEqual(named["summary"]["total"], 900)
        self.assert_matches_database(named, "category=?", ("代理记账",))

        status, _, empty = self.request(
            "GET", "/v1/entries?workspace_id=shop&q=" + quote("代理记账")
            + "&category=" + quote("房租"))
        self.assertEqual(status, 200)
        self.assertEqual(empty["entries"], [])
        self.assertEqual(empty["summary"]["count"], 0)
        self.assertEqual(empty["summary"]["total"], 0)
        self.assertEqual(empty["summary"]["by_year"], [])

    def test_casefold_and_literal_keyword_do_not_scan_every_row(self):
        self.seed()
        status, _, folded = self.request("GET", "/v1/entries?workspace_id=shop&q=BOOKKEEPING")
        self.assertEqual(status, 200)
        self.assertEqual(
            [(row["item"], row["amount"]) for row in folded["entries"]],
            [("Bookkeeping", 10)])
        self.assertEqual(folded["summary"]["by_year"], [
            {"year": "2026", "count": 1, "total": 10},
        ])
        status, _, literal = self.request(
            "GET", "/v1/entries?workspace_id=shop&q=" + quote("' OR '1'='1"))
        self.assertEqual(status, 200)
        self.assertEqual(literal["entries"], [])
        self.assertEqual(literal["summary"]["total"], 0)

        status, _, everything = self.request("GET", "/v1/entries?workspace_id=shop")
        self.assertEqual(status, 200)
        self.assertGreater(everything["summary"]["count"], 2)
        self.assert_matches_database(everything)
        self.assertNotEqual(everything["summary"]["total"], 4000)

    def test_monthly_report_stays_separate_from_keyword_lookup(self):
        self.seed()
        status, _, report = self.request(
            "GET", "/v1/summary?workspace_id=shop&month=2026-03")
        self.assertEqual(status, 200)
        self.assertEqual(report["summary"]["expense"], 2000)
        self.assertNotIn("entries", report)
        status, _, rejected = self.request(
            "GET", "/v1/summary?workspace_id=shop&month=2026-03&q=" + quote("代理记账"))
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"]["code"], "invalid_query")
        self.assertIn("/v1/entries", rejected["error"]["message"])

    def test_rejects_unknown_duplicate_and_overlong_query(self):
        self.seed()
        status, _, unknown = self.request(
            "GET", "/v1/entries?workspace_id=shop&keyword=" + quote("代理记账"))
        self.assertEqual((status, unknown["error"]["code"]), (400, "invalid_query"))
        status, _, duplicate = self.request(
            "GET", "/v1/entries?workspace_id=shop&q=a&q=b")
        self.assertEqual((status, duplicate["error"]["code"]), (400, "invalid_query"))
        status, _, blank = self.request("GET", "/v1/entries?workspace_id=shop&category=")
        self.assertEqual((status, blank["error"]["code"]), (400, "invalid_query"))
        status, _, long_query = self.request(
            "GET", "/v1/entries?workspace_id=shop&q=" + quote("代" * 201))
        self.assertEqual((status, long_query["error"]["code"]), (400, "invalid_query"))
        status, _, backwards = self.request(
            "GET", "/v1/entries?workspace_id=shop&start=2026-12-31&end=2026-01-01")
        self.assertEqual((status, backwards["error"]["code"]), (400, "invalid_range"))

    def test_capabilities_and_docs_tell_a_fresh_agent_to_query_first(self):
        status, _, caps = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        query = next(item for item in caps["capabilities"] if item["id"] == "ledger.query")
        self.assertEqual(query["status"], "available")
        self.assertIn("数据库", "".join(query["limitations"]))
        self.assertIn("分年", "".join(query["limitations"]))
        rules = next(item for item in caps["capabilities"] if item["id"] == "classification.rules")
        self.assertEqual(rules["status"], "available")
        contract = caps["agent_policy"]["entry_queries"]
        self.assertEqual(contract["path"], "/v1/entries")
        self.assertTrue(contract["executed_in_database"])
        self.assertEqual(contract["returns"], ["entries", "summary", "filters"])
        self.assertIn("item", contract["q"])
        self.assertIn("by_year", contract["summary"])
        self.assertIn("数据库", contract["lookup_strategy"])
        self.assertIn("不要先取出大量账目", contract["lookup_strategy"])
        self.assertIn("相同金额", contract["lookup_strategy"])
        self.assertIn("随想", contract["lookup_strategy"])
        self.assertIn("GET /v1/summary", contract["lookup_strategy"])
        strategy = caps["agent_policy"]["user_presentation"]["query_strategy"]
        self.assertEqual(strategy["path"], "/v1/entries")
        self.assertEqual(strategy["keyword_parameter"], "q")
        self.assertEqual(strategy["filters_executed_in_database"],
                         ["q", "start", "end", "type", "category"])
        self.assertTrue(strategy["report_matches_first"])
        self.assertTrue(strategy["do_not_scan_all_entries"])
        self.assertEqual(strategy["do_not_default_to_cross_check"],
                         ["all_categories", "same_amount", "thoughts"])
        self.assertIn("分年", strategy["guidance"])
        onboarding = caps["agent_policy"]["onboarding"]["guidance"]
        self.assertIn("ledger.query", onboarding)
        self.assertIn("GET /v1/entries", onboarding)
        self.assertIn("数据库", onboarding)
        self.assertIn("不要拉全量", onboarding)
        self.assertEqual(caps["operations"]["list_entries"]["path"], "/v1/entries")
        self.assertIn("summary", caps["operations"]["list_entries"]["returns"])
        self.assertFalse(caps["operations"]["get_summary"]["accepts_keyword"])
        docs = (ROOT / "docs" / "agent-capabilities.md").read_text(encoding="utf-8")
        self.assertIn("## 账目关键词查询", docs)
        self.assertIn("user_presentation.query_strategy", docs)
        self.assertIn("GET /v1/entries?workspace_id={当前账本}&q={名称}", docs)
        spec = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("operationId: listEntries", spec)
        self.assertIn("EntryQuery:", spec)
        self.assertIn("EntryQueryResponse:", spec)
        self.assertIn("by_year:", spec)


if __name__ == "__main__":
    unittest.main()
