"""第一版核算回归：临时 SQLite、真实导入路径，不碰用户试记库。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.store import SqliteStore
from engine.observe import ingest
from engine.cost import set_recipe, product_cost, usage_variance
from engine.importer import plan_import, commit_import
from engine.accounting import sales_analysis
from engine.model import today_iso

DAY = '2026-10-23'


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(':memory:')
        self.addCleanup(self.store.close)
        self.store.ensure_shop('s')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def purchase(self, amount=680, qty=50, unit='斤', date='2026-10-01', item='牛肉'):
        self.store.add_entries([dict(shop_id='s', date=date, type='purchase', category='食材成本',
                                     amount=amount, item=item, qty=qty, unit=unit)])

    def recipe(self, text='牛肉80克', **kwargs):
        return set_recipe(self.store, 's', '牛肉面', text, price=12,
                          effective_date=kwargs.pop('effective_date', '2026-10-01'), **kwargs)

    def cost(self, date=DAY):
        return product_cost(self.store, 's', self.store.product_find('s', '牛肉面'), as_of=date)

    def sale(self, **kwargs):
        return self.store.add_sales('s', [dict(date=DAY, product_name='牛肉面', qty=30, amount=300, **({'granularity': 'transaction'} | kwargs))])

    def total(self, amount, **kwargs):
        self.store.add_entries([dict(shop_id='s', date=DAY, type='revenue', category='营业额', amount=amount, **kwargs)])

    def summary(self):
        return self.store.summary('s', DAY, DAY)

    def plan(self, text):
        path = Path(self.tmp.name) / 'input.csv'
        path.write_text(text, encoding='utf-8')
        return plan_import(str(path))

    def test_recipe_equivalent_units_and_yield(self):
        self.purchase()
        for text in ('牛肉80克', '牛肉0.08公斤', '牛肉0.16斤', '牛肉80g'):
            self.recipe(text)
            self.assertAlmostEqual(self.cost()['unit_cost'], 2.176)
        self.recipe('牛肉800克', units=10)
        self.assertEqual(self.cost()['lines'][0]['per_portion_qty'], 80)
        self.assertAlmostEqual(self.cost()['lines'][0]['per_portion_cost'], 2.176)

    def test_invalid_recipe_rolls_back_product_and_ingredients(self):
        self.purchase()
        self.recipe()
        before = self.store.export_all('s')
        for text in ('牛肉2瓶', '牛肉80克，坏片段', '牛肉80', '牛肉-80克', '牛肉80克，牛肉20克'):
            with self.assertRaises(ValueError):
                self.recipe(text, category='错误修改')
            self.assertEqual(self.store.export_all('s'), before)

    def test_month_is_not_money_and_attribution_differs_from_payment(self):
        r = ingest(self.store, 's', '今天付了11月房租8000', base=dt.date.fromisoformat(DAY))
        self.assertEqual(len(r['entry_ids']), 1)
        row = self.store.get_entry(r['entry_ids'][0])
        self.assertEqual((row['date'], row['period_month'], row['amount']), (DAY, '2026-11', 8000))
        oct = self.store.summary('s', '2026-10-01', '2026-10-31')
        nov = self.store.summary('s', '2026-11-01', '2026-11-30')
        self.assertEqual((oct['expense'], oct['accrued_expense']), (8000, 0))
        self.assertEqual((nov['expense'], nov['accrued_expense']), (0, 8000))

    def test_platform_product_and_category_persist(self):
        self.recipe(category='面食')
        ingest(self.store, 's', '今天美团外卖牛肉面卖了30份，每份12元，饿了么牛肉面卖了20份', base=dt.date.fromisoformat(DAY))
        rows = self.store.list_sales('s', DAY, DAY)
        self.assertEqual([r['product_name'] for r in rows], ['牛肉面', '牛肉面'])
        self.assertEqual([r['platform'] for r in rows], ['美团', '饿了么'])
        self.assertEqual(rows[0]['channel'], '配送')
        self.assertEqual(rows[0]['product_category'], '面食')
        self.assertEqual(rows[0]['unit'], '份')
        self.assertEqual(len(self.store.product_list('s')), 1)

    def test_historical_recipe_price_and_missing_price(self):
        self.purchase()
        self.recipe()
        self.purchase(amount=1000, date='2026-11-01')
        self.recipe('牛肉160克', effective_date='2026-11-01')
        self.assertAlmostEqual(self.cost()['unit_cost'], 2.176)
        self.assertAlmostEqual(self.cost('2026-11-02')['unit_cost'], 6.4)
        self.recipe('牛肉80克，酱料10克')
        result = self.cost()
        self.assertFalse(result['complete'])
        self.assertIsNone(result['unit_cost'])
        self.assertIsNone(result['total'])
        self.assertIsNone(result['gross'])
        self.assertIsNone(result['actual_cost'])
        self.assertGreater(result['known_subtotal'], 0)

    def test_exact_ingredient_matching_and_missing_units(self):
        self.purchase(item='牛肉丸')
        self.recipe()
        self.assertFalse(self.cost()['complete'])
        self.purchase(unit='')
        self.assertFalse(self.cost()['complete'])

    def test_daily_total_and_details_not_added(self):
        self.sale(platform='美团')
        self.assertEqual(self.summary()['revenue'], 300)
        self.total(300)
        self.assertEqual(self.summary()['revenue'], 300)
        self.assertFalse(self.summary()['daily_revenue'][0]['warnings'])
        self.total(350)
        self.assertEqual(self.summary()['revenue'], 350)
        self.assertTrue(self.summary()['daily_revenue'][0]['warnings'])
        self.assertEqual(len(self.store.list_entries('s', DAY, DAY)), 2)

    def test_partial_platform_coverage_and_ambiguous_scopes(self):
        self.total(300, platform='美团')
        self.sale(platform='美团')
        self.sale(platform='饿了么')
        self.assertEqual(self.summary()['revenue'], 600)
        self.sale()  # 未标平台，不能断言这是另一笔范围外收入
        self.assertIsNone(self.summary()['revenue'])

    def test_actual_sales_amount_and_mixed_missing_amount(self):
        self.purchase()
        self.recipe()
        self.sale()
        self.store.add_sales('s', [dict(date=DAY, product_name='牛肉面', qty=10, amount=None, granularity='transaction')])
        result = sales_analysis(self.store, 's', DAY, DAY)[0]
        self.assertEqual(result['amount'], 420)
        self.assertTrue(result['estimated'])
        self.assertEqual(result['standard_gross'], 332.96)
        self.assertEqual(result['standard_cost'], 87.04)

    def test_settlement_does_not_deduct_fee_twice(self):
        self.sale(amount_basis='settlement')
        self.store.add_entries([dict(shop_id='s', date=DAY, type='expense', category='平台佣金', amount=20)])
        self.assertEqual(self.summary()['revenue'], 300)
        self.assertIsNone(self.summary()['cash_balance'])
        self.assertIsNone(self.summary()['sales_analysis'][0]['standard_gross'])

    def test_csv_dimensions_same_file_and_overlapping_files(self):
        header = '日期,平台,渠道,商品品类,商品,数量,销售额\n'
        a = '2026-10-23,美团,配送,面食,牛肉面,30,360\n'
        b = '2026-10-23,饿了么,配送,面食,牛肉面,20,240\n'
        one = self.plan(header + a)
        self.assertEqual(one['skipped'], [])
        commit_import(self.store, 's', one, 'a.csv', 'hash-a')
        before = self.store.export_all('s')
        self.assertEqual(commit_import(self.store, 's', one, 'renamed.csv', 'hash-a')['imported'], 0)
        self.assertEqual(self.store.export_all('s'), before)
        result = commit_import(self.store, 's', self.plan(header + a + b), 'b.csv', 'hash-b')
        self.assertEqual((result['imported'], result['duplicates']), (1, 1))
        rows = self.store.list_sales('s', DAY, DAY)
        self.assertEqual(sum(r['qty'] for r in rows), 50)
        self.assertEqual(sum(r['amount'] for r in rows), 600)
        self.assertEqual(self.summary()['revenue'], 600)
        self.assertEqual(rows[0]['product_category'], '面食')
        self.assertTrue(any('美团' in r['raw_text'] for r in self.store.record_list('s', '0000', '9999')))
        self.assertEqual(commit_import(self.store, 'other', one, 'a.csv', 'hash-a')['imported'], 1)

    def test_import_conflict_and_projection_failure_are_atomic(self):
        header = '日期,平台,商品,数量,销售额\n'
        first = self.plan(header + '2026-10-23,美团,牛肉面,30,360\n')
        commit_import(self.store, 's', first, 'a.csv', 'a')
        before = self.store.export_all('s')
        changed = self.plan(header + '2026-10-23,饿了么,米饭,10,20\n2026-10-23,美团,牛肉面,31,372\n')
        with self.assertRaises(ValueError):
            commit_import(self.store, 's', changed, 'b.csv', 'b')
        self.assertEqual(self.store.export_all('s'), before)
        self.assertIsNone(self.store.batch_find_by_hash('s', 'b'))
        with patch('engine.observe.project_ledger_row', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                commit_import(self.store, 's', self.plan(header + '2026-10-24,门店,米饭,10,20\n'), 'fail.csv', 'fail')
        self.assertEqual(self.store.export_all('s'), before)
        self.assertIsNone(self.store.batch_find_by_hash('s', 'fail'))

    def test_transaction_ids_preserve_distinct_sales_reject_daily_overlap(self):
        header = '日期,平台,商品,数量,销售额,明细ID\n'
        first = self.plan(header + '2026-10-23,美团,牛肉面,1,12,a\n2026-10-23,美团,牛肉面,1,12,b\n')
        commit_import(self.store, 's', first, 'a.csv', 'a')
        self.assertEqual(len(self.store.list_sales('s', DAY, DAY)), 2)
        daily = self.plan('日期,平台,商品,数量,销售额\n2026-10-23,美团,牛肉面,2,24\n')
        with self.assertRaises(ValueError):
            commit_import(self.store, 's', daily, 'daily.csv', 'daily')
        self.assertEqual(self.summary()['revenue'], 24)

    def test_purchase_import_and_expense_period(self):
        p = self.plan('日期,类型,商品,数量,单位,金额\n2026-10-23,进货,牛肉,0.125,公斤,3.4\n')
        self.assertEqual(p['kind'], 'entries')
        self.assertEqual(p['rows'][0]['qty'], .125)
        commit_import(self.store, 's', p, 'p.csv', 'p')
        e = self.plan('日期,类型,分类,金额,费用归属月份\n2026-10-23,支出,房租,8000,2026-11\n')
        commit_import(self.store, 's', e, 'e.csv', 'e')
        self.assertEqual(self.store.summary('s', '2026-11-01', '2026-11-30')['accrued_expense'], 8000)

    def test_missing_date_and_refund_are_not_silently_imported(self):
        p = self.plan('日期,商品,数量,销售额\n,牛肉面,1,12\n2026-10-23,牛肉面,-1,-12\n')
        self.assertEqual(p['row_count'], 0)
        self.assertEqual(len(p['skipped']), 2)

    def test_v2_migration_preserves_all_existing_columns(self):
        import sqlite3
        from engine.store import _SCHEMA
        path = Path(self.tmp.name) / 'v2.db'
        conn = sqlite3.connect(str(path))
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO meta VALUES('schema_version','2')")
        conn.execute("INSERT INTO entry(id,shop_id,date,type,category,amount,source,raw_text,created_at) VALUES('old','s','2026-10-01','expense','房租',8000,'chat','旧原话','2026-10-01')")
        conn.execute("INSERT INTO record(id,shop_id,raw_text,source,dedup_ref,received_at,parse_status,ledger_refs,created_at) VALUES('r','s','旧原话','chat','msg:old','2026-10-01','parsed','[\"entry:old\"]','2026-10-01')")
        old = tuple(conn.execute('SELECT * FROM entry').fetchone())
        conn.commit(); conn.close()
        upgraded = SqliteStore(str(path))
        self.assertEqual(tuple(upgraded.conn.execute('SELECT * FROM entry').fetchone())[:len(old)], old)
        self.assertEqual(upgraded.record_find('s', 'msg:old')['raw_text'], '旧原话')
        before = upgraded.export_all('s')
        upgraded.close()
        reopened = SqliteStore(str(path))
        self.assertEqual(reopened.export_all('s'), before)
        reopened.close()

    def test_future_version_is_rejected_without_ddl_changes(self):
        import sqlite3
        path = Path(self.tmp.name) / 'future.db'
        conn = sqlite3.connect(str(path))
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        conn.execute("INSERT INTO meta VALUES('schema_version','99')")
        conn.commit()
        with self.assertRaises(ValueError):
            SqliteStore(str(path))
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('meta',)])
        conn.close()

    def test_month_before_amount_not_confused_with_date(self):
        for text in ('10月8000元房租', '10月8千房租', '2026年10月房租8000', '十月房租8000'):
            with self.subTest(text=text):
                result = ingest(self.store, 's', text, base=dt.date.fromisoformat(DAY))
                rows = result['parsed']['entries']
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['amount'], rows[0]['period_month']), (8000, '2026-10'))

    def test_income_transactions_sum_and_daily_total_covers_them(self):
        p = self.plan('日期,类型,金额,交易ID,平台\n2026-10-23,收入,100,a,美团\n2026-10-23,收入,200,b,美团\n')
        commit_import(self.store, 's', p, 'a.csv', 'income')
        self.assertEqual(self.summary()['revenue'], 300)
        self.total(300, platform='美团')
        self.assertEqual(self.summary()['revenue'], 300)

    def test_import_then_chat_subtotal_does_not_duplicate_qty_or_cost(self):
        self.purchase()
        self.recipe()
        p = self.plan('日期,商品,数量,销售额\n2026-10-23,牛肉面,10,100\n')
        commit_import(self.store, 's', p, 'a.csv', 'a')
        ingest(self.store, 's', '美团牛肉面卖了5份，一份10元', base=dt.date.fromisoformat(DAY))
        result = self.summary()
        self.assertEqual(result['revenue'], 100)
        self.assertEqual(sum(r['qty'] for r in result['sales_analysis']), 10)
        self.assertEqual(result['standard_consumption'], 21.76)
        self.assertTrue(result['daily_revenue'][0]['warnings'])

    def test_alias_cannot_bypass_daily_import_dedup(self):
        self.store.product_upsert('s', '牛肉面', aliases=['牛面'])
        for i, name in enumerate(('牛肉面', '牛面')):
            p = self.plan(f'日期,商品,数量,销售额\n2026-10-23,{name},10,100\n')
            result = commit_import(self.store, 's', p, 'a.csv', str(i))
            self.assertEqual(result['imported'], 1 if i == 0 else 0)
        self.assertEqual(self.summary()['revenue'], 100)

    def test_cross_day_gross_and_settlement_not_combined(self):
        self.total(100)
        self.store.add_entries([dict(shop_id='s', date='2026-10-24', type='revenue', category='营业额', amount=80, amount_basis='settlement')])
        result = self.store.summary('s', '2026-10-01', '2026-10-31')
        self.assertIsNone(result['revenue'])
        self.assertIsNone(result['cash_balance'])
        self.assertEqual(result['amount_basis'], 'mixed')

    def test_quantity_variance_normalizes_before_sum_and_uses_yield(self):
        self.purchase(qty=1, amount=20, unit='kg')
        self.purchase(qty=2, amount=20, unit='斤')
        self.recipe('牛肉800克', units=10)
        self.sale()
        result = usage_variance(self.store, 's', '2026-10-01', DAY)[0]
        self.assertEqual(result['theoretical'], 2.4)
        self.assertEqual(result['actual'], 2)


if __name__ == '__main__':
    unittest.main()
