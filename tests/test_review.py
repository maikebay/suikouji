"""补充与修正回归：写回原账目、保留来源、同一请求只生效一次。"""
import json
import unittest
from unittest.mock import patch

from engine.review import apply_review, correction_history, review_items
from engine.store import SqliteStore

DAY = '2026-10-23'


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(':memory:')
        self.addCleanup(self.store.close)
        self.store.ensure_shop('s', '测试店')

    def purchase(self, **kw):
        row = dict(shop_id='s', date=DAY, type='purchase', category='食材成本', amount=680.0,
                   item='牛肉', qty=None, unit='', source='chat')
        row.update(kw)
        return self.store.add_entries([row], raw_text=row.pop('raw_text', '买了牛肉680'))[0]

    def expense(self, **kw):
        row = dict(shop_id='s', date=DAY, type='expense', category='房租', amount=8000.0,
                   item=None, source='chat')
        row.update(kw)
        return self.store.add_entries([row], raw_text=row.pop('raw_text', '付了房租8000'))[0]

    def revenue(self, amount=1000.0, **kw):
        row = dict(shop_id='s', date=DAY, type='revenue', category='营业额', amount=amount,
                   item=None, source='chat')
        row.update(kw)
        return self.store.add_entries([row], raw_text=row.pop('raw_text', '营业额1000'))[0]

    def sale(self, **kw):
        row = dict(date=DAY, product_name='牛肉面', qty=30, amount=None, granularity='transaction')
        row.update(kw)
        return self.store.add_sales('s', [row])[0]

    def items(self):
        return {item['id']: item for item in review_items(self.store, 's')}

    def observations(self, target_id):
        return self.store.observation_origin('s', f'{target_id}:')


class ChecklistTests(ReviewTests):
    def test_checklist_lists_only_gaps_that_change_the_numbers(self):
        purchase = self.purchase()
        expense = self.expense()
        sale = self.sale()
        complete = self.revenue()
        self.purchase(item='面粉', qty=25, unit='公斤', amount=100.0)
        items = self.items()
        self.assertEqual(set(items), {f'entry:{purchase}', f'entry:{expense}', f'sale:{sale}'})
        self.assertIn('采购还缺数量', items[f'entry:{purchase}']['reasons'][0])
        self.assertIn('费用还缺归属月份', items[f'entry:{expense}']['reasons'][0])
        self.assertEqual(items[f'sale:{sale}']['reasons'],
                         ['销量还缺金额', '销量还缺平台'])
        self.assertNotIn(f'entry:{complete}', items)
        self.assertEqual(items[f'entry:{purchase}']['label'], '牛肉')
        self.assertIn('qty', items[f'entry:{purchase}']['fields'])

    def test_supplement_writes_back_to_the_same_row_and_keeps_provenance(self):
        entry_id = self.purchase()
        before_records = len(self.store.record_list('s', '0000', '9999'))
        result = apply_review(self.store, 's', 'req-1', f'entry:{entry_id}',
                              {'qty': 50, 'unit': '斤'}, '牛肉是50斤')
        self.assertFalse(result['duplicate'])
        self.assertEqual(result['changed'], {'qty': 50.0, 'unit': '斤'})
        self.assertEqual(len(self.store.list_entries('s', '0000', '9999')), 1)
        entry = self.store.get_entry(entry_id)
        self.assertEqual((entry['qty'], entry['unit'], entry['amount']), (50.0, '斤', 680.0))
        record = result['record']
        self.assertEqual((record['source'], record['parse_status']), ('review', 'correction'))
        self.assertEqual(record['correction_of'], f'entry:{entry_id}')
        self.assertEqual(record['raw_text'], '牛肉是50斤')
        self.assertEqual(record['dedup_ref'], 'review:req-1')
        # 原始原话没有被改写，仍然指向这行账目。
        source = self.store.record_for_origin('s', f'entry:{entry_id}')
        self.assertEqual(source['raw_text'], '买了牛肉680')
        self.assertEqual(len(self.store.record_list('s', '0000', '9999')), before_records + 1)
        history = correction_history(self.store, 's', f'entry:{entry_id}')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['before'], {'qty': None, 'unit': ''})
        self.assertEqual(history[0]['after'], {'qty': 50.0, 'unit': '斤'})
        self.assertEqual(history[0]['raw_text'], '牛肉是50斤')
        self.assertNotIn(f'entry:{entry_id}', self.items())
        quantities = [o for o in self.observations(f'entry:{entry_id}') if o['measure'] == '数量']
        self.assertEqual([o['value_num'] for o in quantities], [50.0])

    def test_same_request_id_identical_retry_is_duplicate(self):
        entry_id = self.purchase()
        first = apply_review(self.store, 's', 'req-same', f'entry:{entry_id}',
                             {'qty': 50}, '补数量')
        again = apply_review(self.store, 's', 'req-same', f'entry:{entry_id}',
                             {'qty': 50}, '补数量')
        self.assertFalse(first['duplicate'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['record']['id'], first['record']['id'])
        self.assertEqual(len(self.store.corrections_for('s', f'entry:{entry_id}')), 1)

    def test_same_request_id_applies_once_even_with_different_values(self):
        entry_id = self.purchase()
        first = apply_review(self.store, 's', 'req-retry', f'entry:{entry_id}',
                             {'qty': 50, 'unit': '斤'}, '牛肉是50斤')
        records = len(self.store.record_list('s', '0000', '9999'))
        again = apply_review(self.store, 's', 'req-retry', f'entry:{entry_id}',
                             {'qty': 999, 'unit': '公斤'}, '改错了重来')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['record']['id'], first['record']['id'])
        self.assertEqual(self.store.get_entry(entry_id)['qty'], 50.0)
        self.assertEqual(len(self.store.record_list('s', '0000', '9999')), records)

    def test_correcting_amount_moves_summary_without_double_booking(self):
        entry_id = self.revenue(1000.0)
        self.assertEqual(self.store.summary('s', DAY, DAY)['revenue'], 1000.0)
        apply_review(self.store, 's', 'req-amount', f'entry:{entry_id}',
                     {'amount': 1200.0}, '当天其实是1200')
        summary = self.store.summary('s', DAY, DAY)
        self.assertEqual(summary['revenue'], 1200.0)
        self.assertEqual(summary['count'], 1)
        self.assertEqual(len(self.store.list_entries('s', DAY, DAY)), 1)

    def test_correcting_date_moves_the_entry_to_the_right_day(self):
        entry_id = self.revenue(1000.0)
        apply_review(self.store, 's', 'req-date', f'entry:{entry_id}',
                     {'date': '2026-10-22'}, '这笔记的是昨天')
        self.assertEqual(self.store.list_entries('s', DAY, DAY), [])
        self.assertEqual(len(self.store.list_entries('s', '2026-10-22', '2026-10-22')), 1)

    def test_sale_supplement_reprojects_observations_without_duplicates(self):
        sale_id = self.sale()
        self.assertEqual(len(self.observations(f'sale:{sale_id}')), 1)
        apply_review(self.store, 's', 'req-sale', f'sale:{sale_id}',
                     {'amount': 300.0, 'platform': '美团'}, '美团卖了30份共300')
        first = self.observations(f'sale:{sale_id}')
        self.assertEqual(len(first), 2)
        self.assertEqual(sorted(o['measure'] for o in first), ['数量', '金额'])
        self.assertEqual({o['value_num'] for o in first if o['measure'] == '金额'}, {300.0})
        sale = self.store.get_sale(sale_id)
        self.assertEqual((sale['amount'], sale['platform']), (300.0, '美团'))
        apply_review(self.store, 's', 'req-sale-2', f'sale:{sale_id}',
                     {'amount': 320.0}, '后来又补了20')
        self.assertEqual(len(self.observations(f'sale:{sale_id}')), 2)
        self.assertEqual(self.store.get_sale(sale_id)['amount'], 320.0)
        self.assertNotIn(f'sale:{sale_id}', self.items())

    def test_sale_product_name_correction_resolves_alias(self):
        product = self.store.product_upsert('s', '牛肉面', aliases=['牛面'], category='面食')
        sale_id = self.sale(product_name='牛肉面条')
        apply_review(self.store, 's', 'req-alias', f'sale:{sale_id}',
                     {'product_name': '牛面'}, '写的是牛肉面')
        sale = self.store.get_sale(sale_id)
        self.assertEqual((sale['product_name'], sale['product_id'], sale['product_category']),
                         ('牛肉面', product['id'], '面食'))
        detail = json.loads(self.store.record_find('s', 'review:req-alias')['correction_detail'])
        self.assertEqual(detail['after']['product_name'], '牛肉面')
        self.assertEqual(detail['side_effects']['product_category']['after'], '面食')

    def test_unknown_product_name_drops_the_old_product_link(self):
        product = self.store.product_upsert('s', '牛肉面', category='面食')
        sale_id = self.sale(product_name='牛肉面')
        self.assertEqual(self.store.get_sale(sale_id)['product_id'], product['id'])
        apply_review(self.store, 's', 'req-rename', f'sale:{sale_id}',
                     {'product_name': '牛肉粉'}, '其实卖的是牛肉粉')
        sale = self.store.get_sale(sale_id)
        self.assertEqual(sale['product_name'], '牛肉粉')
        self.assertIsNone(sale['product_id'])


class RejectionTests(ReviewTests):
    def test_wrong_shop_or_bad_field_writes_nothing(self):
        entry_id = self.purchase()
        self.store.ensure_shop('other', '别人的店')
        baseline = (self.store.get_entry(entry_id),
                    len(self.store.record_list('s', '0000', '9999')))
        cases = [
            ('other', f'entry:{entry_id}', {'qty': 50}, '越权修改', '不属于当前账本'),
            ('s', 'entry:不存在', {'qty': 50}, '改不存在的行', '不属于当前账本'),
            ('s', 'unknown:1', {'qty': 50}, '类型不对', '不存在'),
            ('s', f'entry:{entry_id}', {'qty': 50, 'source': 'import'}, '越权字段', '不能修正字段'),
            ('s', f'entry:{entry_id}', {'qty': -5}, '负数', '非负数'),
            ('s', f'entry:{entry_id}', {'qty': '五十斤'}, '文字当数量', '要填数字'),
            ('s', f'entry:{entry_id}', {'date': '2026/10/23'}, '日期写法', 'YYYY-MM-DD'),
            ('s', f'entry:{entry_id}', {'period_month': '2026-1'}, '月份写法', 'YYYY-MM'),
            ('s', f'entry:{entry_id}', {'amount_basis': '大概'}, '口径不对', '口径'),
            ('s', f'entry:{entry_id}', {'qty': 50}, '   ', '修正依据'),
            ('s', f'entry:{entry_id}', {'qty': None}, '没有实际改动', '相同'),
        ]
        for shop, target, fields, reason, expected in cases:
            with self.subTest(target=target, fields=fields, reason=reason):
                with self.assertRaises(ValueError) as caught:
                    apply_review(self.store, shop, 'req-bad', target, fields, reason)
                self.assertIn(expected, str(caught.exception))
        self.assertEqual((self.store.get_entry(entry_id),
                          len(self.store.record_list('s', '0000', '9999'))), baseline)
        self.assertIsNone(self.store.record_find('s', 'review:req-bad'))

    def test_blank_request_id_is_rejected(self):
        entry_id = self.purchase()
        with self.assertRaises(ValueError):
            apply_review(self.store, 's', '', f'entry:{entry_id}', {'qty': 50}, '补数量')
        with self.assertRaises(ValueError):
            apply_review(self.store, 's', 'x' * 101, f'entry:{entry_id}', {'qty': 50}, '补数量')

    def test_review_rejects_bool_and_nonfinite_numbers(self):
        entry_id = self.purchase()
        baseline = self.store.get_entry(entry_id)
        for value in (True, float('inf'), float('nan')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    apply_review(self.store, 's', 'bad-number-' + str(len(self.store.record_list('s', '0000', '9999'))),
                                 f'entry:{entry_id}', {'qty': value}, '数字不对')
                self.assertEqual(self.store.get_entry(entry_id), baseline)

    def test_failed_projection_leaves_no_half_correction(self):
        entry_id = self.purchase()
        with patch('engine.observe.project_ledger_row', side_effect=RuntimeError('投影失败')):
            with self.assertRaises(RuntimeError):
                apply_review(self.store, 's', 'req-fail', f'entry:{entry_id}',
                             {'qty': 50, 'unit': '斤'}, '牛肉是50斤')
        self.assertIsNone(self.store.get_entry(entry_id)['qty'])
        self.assertIsNone(self.store.record_find('s', 'review:req-fail'))
        self.assertEqual(self.store.corrections_for('s', f'entry:{entry_id}'), [])


if __name__ == '__main__':
    unittest.main()
