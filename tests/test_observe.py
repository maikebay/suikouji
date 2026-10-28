"""分层记录的集成回归：用真实 SQLite 和 CLI，不接网络。"""
import contextlib
import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engine import cli, observe
from engine.store import SCHEMA_VERSION, SqliteStore, _SCHEMA

DAY = "2026-10-23"
BASE = dt.date(2026, 10, 23)


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)

    def ingest(self, text, **kwargs):
        return observe.ingest(self.store, "s", text, base=BASE, **kwargs)

    def observations(self):
        return self.store.observation_list("s", "0000", "9999")

    def test_new_types_never_write_any_ledger_table(self):
        for text in ("今天客流120人", "今天下雨", "小王今天请假", "评分4.8", "差评0个"):
            result = self.ingest(text)
            self.assertEqual(result["status"], "parsed", text)
            self.assertEqual(result["entry_ids"], [], text)
            self.assertEqual(result["sale_ids"], [], text)
        tables = self.store.export_all("s")["tables"]
        for name in ("entry", "sale", "product", "ingredient", "recipe_line"):
            self.assertEqual(tables[name], [], name)
        self.assertEqual(len(tables["record"]), 5)
        self.assertEqual(len(tables["observation"]), 5)

    def test_mixed_message_writes_money_once(self):
        raw = "  昨天营业额1280，今天客流120人，买了50斤牛肉花了680，评分4.8  "
        result = self.ingest(raw)
        records = self.store.record_list("s", "0000", "9999")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["raw_text"], raw)
        self.assertEqual(len(self.observations()), 5)
        self.assertEqual(self.store.summary("s", "0000", "9999")["expense"], 0)
        revenue = next(o for o in self.observations() if o["kind"] == "营业额")
        visitors = next(o for o in self.observations() if o["kind"] == "客流")
        self.assertEqual(revenue["occurred_at"], "2026-10-22")
        self.assertEqual(visitors["occurred_at"], DAY)
        self.assertEqual(len(result["entry_ids"]), 2)

    def test_weather_narrative_keeps_asserted_daily_revenue_once(self):
        raw = ('今天是周末，但是天气不好，阴天，天气也越来越冷了。'
               '外面还有跑马拉松的，可能影响生意。今天的营业额很差，只有1300。')
        base = dt.date(2026, 10, 25)
        result = observe.ingest(self.store, 's', raw, base=base, dedup_ref='weather-revenue')
        self.assertEqual([(e['type'], e['amount']) for e in result['parsed']['entries']],
                         [('revenue', 1300)])
        self.assertEqual(len(result['entry_ids']), 1)
        self.assertEqual(result['sale_ids'], [])
        record = self.store.record_find('s', 'weather-revenue')
        self.assertEqual((record['raw_text'], record['occurred_at']), (raw, '2026-10-25'))
        rows = self.observations()
        money = [o for o in rows if o['kind'] == '营业额']
        self.assertEqual([(o['value_num'], o['occurred_at']) for o in money],
                         [(1300, '2026-10-25')])
        self.assertTrue(any(o['kind'] == '天气' for o in rows))
        self.assertTrue(all(o['record_id'] == record['id'] for o in rows))
        summary = self.store.summary('s', '2026-10-25', '2026-10-25')
        self.assertEqual((summary['count'], summary['revenue'], summary['expense']), (1, 1300, 0))
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(any('营业额' in s or '1300' in s for s in result['parsed']['leftovers']))
        before = self.store.export_all('s')
        duplicate = observe.ingest(self.store, 's', '营业额9999', base=base,
                                   dedup_ref='weather-revenue')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(self.store.export_all('s'), before)

    def test_revenue_continuation_preserves_dates_scope_and_summary(self):
        raw = ('  昨天美团外卖营业额很差，只有1300；'
               '今天饿了么自取营业额一般，合计1400；今天下雨，客流10人  ')
        result = self.ingest(raw, dedup_ref='revenue-scopes')
        self.assertEqual(result['status'], 'parsed')
        self.assertEqual(len(result['entry_ids']), 2)
        self.assertEqual(result['sale_ids'], [])
        record = self.store.record_find('s', 'revenue-scopes')
        self.assertEqual(record['raw_text'], raw)
        entries = self.store.list_entries('s', '0000', '9999')
        self.assertEqual(sorted((e['date'], e['amount'], e['platform'], e['channel'])
                                for e in entries),
                         [('2026-10-22', 1300, '美团', '配送'),
                          ('2026-10-23', 1400, '饿了么', '自取')])
        rows = self.observations()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(o['record_id'] == record['id'] for o in rows))
        self.assertEqual(sorted((o['occurred_at'], o['value_num'])
                                for o in rows if o['kind'] == '营业额'),
                         [('2026-10-22', 1300), ('2026-10-23', 1400)])
        summary = self.store.summary('s', '0000', '9999')
        self.assertEqual((summary['count'], summary['revenue'], summary['expense']),
                         (2, 2700, 0))

    def test_revenue_continuation_does_not_cross_masked_observations(self):
        for index, raw in enumerate((
                '今天营业额很差，只有1300客流10人',
                '今天营业额很差，只有1300差评0个',
                '今天营业额很差，只有1300阴天',
                '如果今天阴天，今天营业额很差，只有1300',
                '今天营业额很差，只有1300吗？今天阴天')):
            with self.subTest(raw=raw):
                result = self.ingest(raw, dedup_ref='guard:' + str(index))
                self.assertEqual(result['entry_ids'], [])
                self.assertEqual(result['sale_ids'], [])
                self.assertFalse(any(o['kind'] == '营业额' for o in result['observations']))
                self.assertTrue(result['parsed']['leftovers'])
                self.assertEqual(result['record']['raw_text'], raw)
        self.assertEqual(self.store.summary('s', '0000', '9999')['count'], 0)

    def test_duplicate_message_and_legitimate_repetition(self):
        self.ingest("营业额1200，客流120人", dedup_ref="msg:1")
        duplicate = self.ingest("营业额9999", dedup_ref="msg:1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 1200)
        self.ingest("营业额1200，客流120人", dedup_ref="msg:2")
        # 新来源保留两条原始记录；同日同范围总额不重复累计。
        self.assertEqual(len(self.store.list_entries('s', '0000', '9999')), 2)
        summary = self.store.summary('s', '0000', '9999')
        self.assertEqual(summary['revenue'], 1200)
        self.assertTrue(summary['daily_revenue'][0]['warnings'])
        self.assertEqual(len(self.observations()), 4)

    def test_unparsed_and_partial_keep_verbatim_source(self):
        self.ingest("  外面还有跑马拉松的  ")
        partial = self.ingest("今天下雨，可能影响生意")
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["parsed"]["leftovers"], ["可能影响生意"])
        self.assertEqual([o["kind"] for o in partial["observations"]], ["天气"])
        rows = self.store.record_list("s", DAY, DAY, "unparsed")
        self.assertEqual([r["raw_text"] for r in rows], ["  外面还有跑马拉松的  "])
        self.assertEqual(self.store.summary("s", "0000", "9999")["count"], 0)

    def test_qualitative_traffic_and_business_do_not_invent_numbers(self):
        phrases = {
            "老顾客非常的多": ("客流", "情况", "老客多"),
            "老客多": ("客流", "情况", "老客多"),
            "回头客很多": ("客流", "情况", "老客多"),
            "人很多": ("客流", "情况", "人多"),
            "客人多": ("客流", "情况", "人多"),
            "客人少": ("客流", "情况", "客人少"),
            "客流少": ("客流", "情况", "客人少"),
            "客流一般": ("客流", "情况", "客流一般"),
            "生意还可以": ("生意", "好坏", "还可以"),
            "生意不错": ("生意", "好坏", "不错"),
            "生意很好": ("生意", "好坏", "不错"),
            "生意一般": ("生意", "好坏", "一般"),
            "生意不好": ("生意", "好坏", "不好"),
            "生意有点差": ("生意", "好坏", "不好"),
            "生意差": ("生意", "好坏", "不好"),
        }
        for text, expected in phrases.items():
            with self.subTest(text=text):
                result = self.ingest("今天" + text, dedup_ref=text)
                self.assertEqual(result["status"], "parsed", result["parsed"]["leftovers"])
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["sale_ids"], [])
                self.assertEqual(result["parsed"]["leftovers"], [])
                row = result["observations"][0]
                self.assertEqual((row["kind"], row["measure"], row["value_text"]), expected)
                self.assertIsNone(row["value_num"])
                self.assertEqual(row["unit"], "")
                self.assertEqual(row["source_span"], text)
        self.assertEqual(self.store.summary("s", "0000", "9999")["count"], 0)
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 0)

    def test_numeric_traffic_stays_numeric_beside_qualitative_words(self):
        result = self.ingest("今天客流120人，老客多，生意还可以，营业额1300元")
        self.assertEqual(result["status"], "parsed")
        self.assertEqual([(e["type"], e["amount"]) for e in result["parsed"]["entries"]],
                         [("revenue", 1300)])
        labels = {(o["kind"], o["measure"]): (o["value_num"], o["value_text"])
                  for o in result["observations"]}
        self.assertEqual(labels[("客流", "人数")], (120, None))
        self.assertEqual(labels[("客流", "情况")], (None, "老客多"))
        self.assertEqual(labels[("生意", "好坏")], (None, "还可以"))
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 1300)

    def test_qualitative_patterns_do_not_guess_nearby_narratives(self):
        for text in ("可能影响生意", "生意差不多", "生意好像很难", "如果生意还可以",
                     "天气很好", "营业额一般", "客人不少"):
            with self.subTest(text=text):
                result = self.ingest(text, dedup_ref=text)
                self.assertEqual(result["observations"], [])
                self.assertEqual(result["entry_ids"], [])
                self.assertEqual(result["sale_ids"], [])

    def test_chinese_zero_and_count_units(self):
        self.ingest("客流零人，上座20桌，外卖30单，差评三个")
        values = {(o["measure"], o["unit"]): o["value_num"] for o in self.observations()}
        self.assertEqual(values, {("人数", "人"): 0, ("桌数", "桌"): 20,
                                  ("单量", "单"): 30, ("差评数", "个"): 3})
        self.assertEqual(self.store.summary("s", "0000", "9999")["count"], 0)

    def test_attendance_whole_span_and_weather_longest_match(self):
        self.ingest("小王今天请假，今天下大雨")
        rows = self.observations()
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(o for o in rows if o["kind"] == "排班")["subject"], "小王")
        self.assertEqual(next(o for o in rows if o["kind"] == "天气")["value_text"], "雨")

    def test_new_registry_type_uses_existing_schema_and_analysis(self):
        rule = {"pattern": r"报损(?P<value>\d+)斤", "measure": "重量", "unit": "斤"}
        with patch.dict(observe.RECORD_TYPES, {"损耗": [rule]}):
            self.ingest("今天报损3斤")
        series = self.store.observation_series("s", DAY, DAY, "损耗", "重量")
        self.assertEqual(series[0]["total"], 3)
        self.assertEqual(self.store.summary("s", "0000", "9999")["count"], 0)

    def test_aliases_normalize_exactly_and_merge_as_lists(self):
        self.store.dimension_upsert("s", "subject", "牛肉", ["黄牛肉"])
        row = self.store.dimension_upsert("s", "subject", "牛肉", ["牛肉类"])
        self.assertEqual(set(row["aliases"]), {"黄牛肉", "牛肉类"})
        self.assertEqual(self.store.dimension_resolve("s", "subject", "牛"), "牛")
        self.ingest("买了50斤黄牛肉花了680")
        self.assertTrue(all(o["subject"] == "牛肉" for o in self.observations()))
        self.assertEqual(self.store.dimension_resolve("other", "subject", "黄牛肉"), "黄牛肉")
        with self.assertRaises(ValueError):
            self.store.dimension_upsert("s", "subject", "猪肉", ["黄牛肉"])

    def test_text_groups_and_units_are_not_summed_together(self):
        self.ingest("今天下雨")
        self.ingest("今天晴天")
        self.ingest("今天下雨")
        self.ingest("买了1斤牛肉100，买了1公斤牛肉200")
        weather = [r for r in self.store.observation_summary("s", DAY, DAY) if r["kind"] == "天气"]
        self.assertEqual({r["value_text"]: r["n"] for r in weather}, {"雨": 2, "晴": 1})
        self.assertTrue(all(r["total"] is None for r in weather))
        series = self.store.observation_series("s", DAY, DAY, "进货", "数量")
        self.assertEqual({r["unit"]: r["total"] for r in series}, {"斤": 1, "kg": 1})

    def test_update_and_undo_sync_observations_keep_source(self):
        result = self.ingest("营业额1000，客流100人")
        entry_id = result["entry_ids"][0]
        self.store.update_entry(entry_id, amount=2000)
        money = next(o for o in self.observations() if o["kind"] == "营业额")
        self.assertEqual(money["value_num"], 2000)
        self.store.delete_entry(entry_id)
        self.assertEqual([o["kind"] for o in self.observations()], ["客流"])
        self.assertEqual(self.store.record_list("s", DAY, DAY)[0]["raw_text"], "营业额1000，客流100人")
        observe.backfill_records(self.store, "s")
        self.assertEqual([o["kind"] for o in self.observations()], ["客流"])

    def test_backfill_after_ingestion_is_idempotent(self):
        self.ingest("营业额1000，买了50斤牛肉680，牛肉面卖了30份")
        before = self.store.export_all("s")
        first = observe.backfill_records(self.store, "s")
        second = observe.backfill_records(self.store, "s")
        self.assertEqual(first["added"], 0)
        self.assertEqual(second["observations"], 0)
        self.assertEqual(before, self.store.export_all("s"))

    def test_ingestion_failure_rolls_back_all_projections_and_allows_retry(self):
        with patch.object(self.store, "add_entries", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                self.ingest("营业额1000，客流100人", dedup_ref="msg:retry")
        pending = self.store.record_find("s", "msg:retry")
        self.assertEqual(pending["raw_text"], "营业额1000，客流100人")
        self.assertEqual(pending["parse_status"], "pending")
        self.assertEqual(self.observations(), [])
        self.ingest("营业额1000，客流100人", dedup_ref="msg:retry")
        self.assertEqual(len(self.observations()), 2)

    def test_pending_raw_record_can_resume(self):
        record = self.store.record_add("s", "营业额1000", dedup_ref="msg:pending", occurred_at=DAY)
        result = self.ingest("被替换的正文9999", dedup_ref="msg:pending")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["record"]["id"], record["id"])
        self.assertEqual(self.store.summary("s", "0000", "9999")["revenue"], 1000)

    def test_observations_reject_wrong_shop_and_rollback_batch(self):
        rec = self.store.record_add("s", "客流1人", occurred_at=DAY)
        good = {"record_id": rec["id"], "occurred_at": DAY, "kind": "客流", "measure": "人数",
                "value_num": 1, "dedup_key": "one"}
        with self.assertRaises(ValueError):
            self.store.observation_add("other", [good])
        for bad in ({**good, "dedup_key": "two", "value_num": float("nan")},
                    {**good, "dedup_key": "two", "value_text": "one"}):
            with self.assertRaises(ValueError):
                self.store.observation_add("s", [good, bad])
            self.assertEqual(self.observations(), [])

    def test_pending_retry_keeps_original_relative_date_context(self):
        observe.observe(self.store, "s", "昨天客流120人，营业额100", base=BASE,
                        dedup_ref="msg:earlier")
        observe.ingest(self.store, "s", "ignored", base=BASE + dt.timedelta(days=1),
                       dedup_ref="msg:earlier")
        rows = self.observations()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["occurred_at"] == "2026-10-22" for r in rows))

    def test_rebuild_keeps_original_record_link(self):
        result = self.ingest("营业额100，牛肉面卖了3份", dedup_ref="msg:rebuild")
        rec = result["record"]["id"]
        self.store.observation_clear("s", rec)
        self.store.update_entry(result["entry_ids"][0], amount=200)
        observe.backfill_records(self.store, "s")
        self.assertEqual(len(self.store.record_list("s", "0000", "9999")), 1)
        self.assertTrue(all(r["record_id"] == rec for r in self.observations()))

    def test_unknown_numbers_and_nonassertions_do_not_become_expenses(self):
        for text in ("气温28度", "评分-1分", "今天没有下雨", "今天客流120人吗", "如果下雨"):
            result = self.ingest(text)
            self.assertEqual(result["entry_ids"], [], text)
            self.assertEqual(result["observations"], [], text)
        self.assertEqual(self.observations(), [])

    def test_iso_date_is_not_money(self):
        self.ingest("2026-10-20客流120人")
        self.assertEqual(self.store.summary("s", "0000", "9999")["count"], 0)
        self.assertEqual(self.observations()[0]["occurred_at"], "2026-10-20")

    def test_observation_dedup_includes_date(self):
        row = {"record_id": "r", "kind": "客流", "measure": "人数", "value_num": 10, "unit": "人"}
        today = observe.dedup_keys([{**row, "occurred_at": DAY}])[0]
        yesterday = observe.dedup_keys([{**row, "occurred_at": "2026-10-22"}])[0]
        self.assertNotEqual(today["dedup_key"], yesterday["dedup_key"])


class MigrationAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "old.db")

    def old_db(self):
        conn = sqlite3.connect(self.path)
        # V1 所有原表原样建库，刻意不经过新版 Store 的写入钩子。
        from pathlib import Path
        conn.executescript((Path(__file__).parent / 'fixtures' / 'schema-v1.sql').read_text())
        conn.execute("INSERT INTO meta VALUES('schema_version','1')")
        for eid, type_, amount in (("e1", "revenue", 1000), ("e2", "purchase", 200)):
            conn.execute("INSERT INTO entry(id,shop_id,date,type,category,amount,source,raw_text,batch_id,created_at)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", (eid, "s", "2026-01-01", type_, "分类", amount,
                         "import", "营业额1000，进货200", "batch-old", "2026-01-02T12:00:00"))
        conn.execute("INSERT INTO sale(id,shop_id,date,product_name,qty,source,created_at)"
                     " VALUES('sale1','s','2026-01-01','牛肉面',3,'import','2026-01-02T12:00:00')")
        conn.execute("INSERT INTO entry(id,shop_id,date,type,category,amount,source,raw_text,created_at)"
                     " VALUES('other','other','2026-01-01','revenue','营业额',999,'chat','营业额999','2026-01-02')")
        conn.commit()
        schema = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        old_rows = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in ("entry", "sale")}
        conn.close()
        return schema, old_rows

    def test_v1_upgrade_preserves_ledger_and_backfills_once(self):
        schema, old_rows = self.old_db()
        store = SqliteStore(self.path)
        for name, sql in schema:
            if name not in ('entry', 'sale', 'product'):
                self.assertEqual(store.conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0], sql)
        for table, rows in old_rows.items():
            self.assertEqual([tuple(r)[:len(rows[0])] for r in store.conn.execute(f'SELECT * FROM {table}')], rows)
        self.assertEqual(store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(record)")}
        self.assertLessEqual({'correction_of', 'correction_detail'}, columns)
        records = store.record_list("s", "0000", "9999")
        self.assertEqual(len(records), 3)
        entries = [r for r in records if r["dedup_ref"].startswith("entry:")]
        self.assertTrue(all(r["raw_text"] == "营业额1000，进货200" for r in entries))
        self.assertTrue(all(r["source"] == "import" and r["batch_id"] == "batch-old" for r in entries))
        self.assertTrue(all(r["received_at"] == "2026-01-02T12:00:00" for r in entries))
        legacy_sale = next(r for r in records if r["dedup_ref"].startswith("sale:"))
        self.assertEqual(legacy_sale["raw_text"], "")
        self.assertEqual(legacy_sale["parse_status"], "legacy_structured")
        obs = store.observation_list("s", "0000", "9999")
        self.assertEqual(len(obs), 3)
        self.assertEqual(sum(r["value_num"] for r in obs if r["measure"] == "金额"), 1200)
        self.assertEqual(len(store.observation_list("other", "0000", "9999")), 1)
        before = store.export_all("s")
        store.close()
        again = SqliteStore(self.path)
        self.addCleanup(again.close)
        observe.backfill_records(again, "s")
        self.assertEqual(again.export_all("s"), before)

    def test_cli_undo_respects_shop_and_structured_raw_text(self):
        payload = json.dumps({"date": DAY, "type": "revenue", "amount": 100,
                              "raw_text": "营业额100"}, ensure_ascii=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--db", self.path, "--shop", "a", "add-json", payload]), 0)
        store = SqliteStore(self.path)
        self.addCleanup(store.close)
        entry = store.list_entries("a", "0000", "9999")[0]
        self.assertEqual(entry["raw_text"], "营业额100")
        self.assertEqual(store.record_list("a", DAY, DAY)[0]["raw_text"], "营业额100")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--db", self.path, "--shop", "b", "undo", entry["id"]]), 1)
        self.assertEqual(len(store.list_entries("a", "0000", "9999")), 1)

    def test_cli_nonfinancial_and_new_read_commands(self):
        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["--db", self.path, "--shop", "s", "--json", *args])
            return code, json.loads(out.getvalue())
        code, result = run("record", "今天客流120人，评分4.8", "--dedup-ref", "msg:cli")
        self.assertEqual(code, 0)
        self.assertEqual(result["recorded"], [])
        self.assertEqual(len(result["observations"]), 2)
        self.assertTrue(run("record", "今天客流120人，评分4.8", "--dedup-ref", "msg:cli")[1]["duplicate"])
        for args in (("records",), ("observations", "summary"), ("observations", "kinds"),
                     ("observations", "series", "--kind", "客流", "--measure", "人数"),
                     ("dimension", "set", "subject", "牛肉", "--aliases", "黄牛肉"),
                     ("dimension", "list"), ("backfill",), ("export",)):
            self.assertEqual(run(*args)[0], 0, args)
        self.assertEqual(run("record", "嗯好的")[0], 3)


if __name__ == "__main__":
    unittest.main()
