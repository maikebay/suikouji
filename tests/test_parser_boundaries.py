"""连续口语的金额归属回归；固定日期，临时库，不接触试记数据。"""
import contextlib
import datetime as dt
import itertools
import unittest
from unittest.mock import patch

from engine import parser
from engine.observe import ingest
from engine.store import SqliteStore

BASE = dt.date(2026, 10, 24)


class BoundaryTests(unittest.TestCase):
    def parse(self, text):
        return parser.parse(text, BASE)

    def assert_sales(self, text, expected):
        result = self.parse(text)
        self.assertEqual([(s['product_name'], s['qty'], s['amount'])
                          for s in result['sales']], expected)
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['leftovers'], [])
        for previous, current in zip(result['sales'], result['sales'][1:]):
            self.assertLessEqual(previous['end'], current['start'])
        return result

    def test_daily_revenue_amount_can_continue_after_one_comma(self):
        examples = (
            ('营业额，只有1300', 1300),
            ('今天的营业额很差，只有1300', 1300),
            ('今天的营业额很差，只有1300。', 1300),
            ('今天的营业额不好,仅有1300元', 1300),
            ('今日营收很低，仅1300块', 1300),
            ('昨天流水一般，才1300', 1300),
            ('营业额不错，一共1300', 1300),
            ('销售额很好，总共是1300元', 1300),
            ('营收比较差，合计1300', 1300),
            ('流水不多，总计是1300', 1300),
            ('营业额很少，共计1300', 1300),
            ('营业额有点差，共1300', 1300),
            ('营业额很高，只有一千三百元', 1300),
            ('营业额很差，只有1,300元', 1300),
            ('营业额很差，只有1.3千', 1300),
            ('营业额很差，只有1300.50元', 1300.5),
        )
        for text, amount in examples:
            for extra_mask in (None, []):
                with self.subTest(text=text, layered=extra_mask is not None):
                    result = parser.parse(text, BASE, extra_mask=extra_mask)
                    self.assertEqual([(e['type'], e['granularity'], e['amount'])
                                      for e in result['entries']],
                                     [('revenue', 'daily', amount)])
                    self.assertEqual(result['raw'], text)
                    self.assertEqual(result['sales'], [])
                    self.assertEqual(result['leftovers'], [])

    def test_revenue_continuation_keeps_local_dates_dimensions_and_sale_spans(self):
        text = ('昨天美团外卖营业额很差，只有1300；'
                '今天饿了么自取的销售额一般，合计1400；'
                '牛肉面卖了2份20元；房租8000')
        result = self.parse(text)
        self.assertEqual([(e['date'], e['type'], e['amount'], e['platform'], e['channel'])
                          for e in result['entries']],
                         [('2026-10-23', 'revenue', 1300, '美团', '配送'),
                          ('2026-10-24', 'revenue', 1400, '饿了么', '自取'),
                          ('2026-10-24', 'expense', 8000, '', '')])
        self.assertEqual([e['note'] for e in result['entries'][:2]], text.split('；')[:2])
        sale = result['sales'][0]
        self.assertEqual((sale['date'], sale['platform'], sale['channel'], sale['amount']),
                         ('2026-10-24', '', '', 20))
        self.assertEqual(text[sale['start']:sale['end']], '牛肉面卖了2份20元')
        self.assertEqual(result['leftovers'], [])
        inherited = self.parse('昨天房租8000；营业额很差，只有1300')
        self.assertEqual([(e['date'], e['amount']) for e in inherited['entries']],
                         [('2026-10-23', 8000), ('2026-10-23', 1300)])

    def test_revenue_continuation_does_not_promote_nonassertions_or_ambiguous_amounts(self):
        examples = (
            '预计今天营业额很差，只有1300',
            '今天营业额预计很差，只有1300',
            '计划今天营业额很差，只有1300',
            '今天营业额目标很低，只有1300',
            '明天营业额很差，只有1300',
            '2027年1月1日营业额很差，只有1300',
            '明天房租8000；营业额很差，只有1300',
            '如果今天营业额很差，只有1300',
            '假如今天营业额很差，只有1300',
            '如果下雨，今天营业额很差，只有1300',
            '今天营业额会不会很差，只有1300',
            '今天营业额很差，只有1300吗',
            '今天营业额很差，只有1300？',
            '今天营业额很差，只有1300?',
            '今天营业额很差，只有1300！？',
            '今天营业额很差，只有1300!?',
            '今天营业额很差，只有1300!？',
            '今天营业额很差，只有1300\n人',
            '预计\n今天营业额很差，只有1300',
            '今天营业额很差，只有1300\n还是1400？',
            '今天营业额很差，只有1300元\n成本',
            '今天没有营业额，只有1300',
            '今天营业额不是很差，只有1300',
            '今天营业额很差，不是只有1300',
            '今天营业额很差，只有-1300',
            '今天营业额很差，只有负一千三百',
            '今天营业额很差，可能只有1300',
            '今天营业额很差，大概只有1300',
            '今天营业额很差，只有1300左右',
            '今天营业额很差，只有1300或1400',
            '今天营业额很差，只有1300/1400',
            '今天营业额很差，只有1300到1400',
            '今天营业额很差，只有1300 1400',
            '今天营业额很差，只有1300，合计1400',
            '今天营业额1200，只有1300',
            '今天营业额很差，只有1300，错了是1400',
            '今天营业额很差，只有1300元成本',
            '今天营业额很差，只有1300元房租',
            '今天营业额很差，只有1300元买了牛肉',
            '今天营业额很差，房租只有1300',
            '今天营业额和房租很差，只有1300',
            '今天营业额很差，昨天只有1300',
            '今天营业额很差，10月23日只有1300',
            '今天营业额很差，只有1300昨天',
            '今天营业额很差，美团只有1300',
            '今天营业额很差，只有1300饿了么外卖',
            '今天美团饿了么营业额很差，只有1300',
            '今天营业额很差，只有1300人',
            '今天营业额很差，只有1300单',
            '今天营业额很差，只有1300份',
            '今天营业额很差，只有1300桌',
            '今天营业额很差，只有1300个',
            '今天营业额很差，只有1300斤',
            '今天营业额很差，只有一千三百人',
            '今天营业额很差，只有1300元/单',
            '今天营业额很差，只有1300客流10人',
            '今天营业额很差，只有1300差评0个',
            '今天营业额很差，1300',
            '今天生意很差，只有1300',
            '今天营业额很差。只有1300',
            '今天营业额很差；只有1300',
            '今天营业额很差、只有1300',
            '今天营业额很差，，只有1300',
        )
        for text in examples:
            with self.subTest(text=text):
                result = parser.parse(text, BASE, extra_mask=[])
                self.assertFalse(any(e['type'] == 'revenue' and e['granularity'] == 'daily'
                                     and e['amount'] == 1300 for e in result['entries']), result)
                self.assertTrue(result['leftovers'])
                # 拒绝扩展的句子须保持旧路径结果；不在此修复旧的交易级分类语义。
                with patch.object(parser, '_daily_revenue_continuations', return_value=iter(())):
                    unchanged = parser.parse(text, BASE, extra_mask=[])
                self.assertEqual(result, unchanged)

    def test_consecutive_quantity_first_sales(self):
        self.assert_sales('卖了30份牛肉面卖了20份炒饭',
                          [('牛肉面', 30, None), ('炒饭', 20, None)])
        self.assert_sales('卖了三十份牛肉面三百元卖了二十份炒饭二百元',
                          [('牛肉面', 30, 300), ('炒饭', 20, 200)])

    def test_consecutive_product_first_and_mixed_sales(self):
        for text in ('牛肉面卖了30份300元炒饭卖了20份200元',
                     '牛肉面卖了30份300元卖了20份炒饭200元',
                     '卖了30份牛肉面300元炒饭卖了20份200元'):
            with self.subTest(text=text):
                self.assert_sales(text, [('牛肉面', 30, 300), ('炒饭', 20, 200)])

    def test_each_sale_keeps_its_price_and_exact_source_span(self):
        text = '牛肉面卖了30份每份12元炒饭卖了20份每份10元'
        result = self.assert_sales(text, [('牛肉面', 30, 360), ('炒饭', 20, 200)])
        self.assertEqual([text[s['start']:s['end']] for s in result['sales']],
                         ['牛肉面卖了30份每份12元', '炒饭卖了20份每份10元'])

    def test_sales_do_not_inherit_another_sales_platform_or_date(self):
        result = self.parse('昨天美团外卖牛肉面卖了30份300元今天饿了么自取炒饭卖了20份200元')
        self.assertEqual([(s['date'], s['platform'], s['channel'], s['product_name'])
                          for s in result['sales']],
                         [('2026-10-23', '美团', '配送', '牛肉面'),
                          ('2026-10-24', '饿了么', '自取', '炒饭')])
        self.assertEqual(result['entries'], [])
        self.assertEqual(result['leftovers'], [])

    def test_quantity_first_platform_suffix_belongs_to_next_sale(self):
        text = '美团卖了30份牛肉面饿了么自取卖了20份炒饭'
        result = self.assert_sales(text, [('牛肉面', 30, None), ('炒饭', 20, None)])
        self.assertEqual([(s['platform'], s['channel']) for s in result['sales']],
                         [('美团', ''), ('饿了么', '自取')])
        self.assertEqual([text[s['start']:s['end']] for s in result['sales']],
                         ['美团卖了30份牛肉面', '饿了么自取卖了20份炒饭'])

    def test_explicit_dates_are_not_money_or_product_names(self):
        for first, second in (('2026-10-23', '2026-10-24'), ('10月23日', '10月24日'),
                              ('23号', '24号')):
            with self.subTest(first=first):
                text = first + '美团牛肉面卖了30份300元' + second + '炒饭卖了20份200元'
                result = self.assert_sales(text, [('牛肉面', 30, 300), ('炒饭', 20, 200)])
                self.assertEqual([s['date'] for s in result['sales']],
                                 ['2026-10-23', '2026-10-24'])
        result = self.parse('2026-10-23房租8000元2026-10-24水电200元')
        self.assertEqual([(e['date'], e['category'], e['amount']) for e in result['entries']],
                         [('2026-10-23', '房租', 8000), ('2026-10-24', '水电', 200)])
        self.assertEqual(result['leftovers'], [])

    def test_incomplete_sale_keeps_its_quantity_out_of_the_ledger(self):
        result = self.parse('卖了30份卖了20份炒饭')
        self.assertEqual([(s['product_name'], s['qty']) for s in result['sales']], [('炒饭', 20)])
        self.assertEqual(result['entries'], [])
        self.assertIn('卖了30份', result['leftovers'])
        result = self.parse('卖了30份，炒饭卖了20份')
        self.assertEqual([(s['product_name'], s['qty']) for s in result['sales']], [('炒饭', 20)])
        self.assertEqual(result['entries'], [])
        self.assertIn('卖了30份', result['leftovers'])

    def test_quantity_first_total_markers_do_not_become_product_names(self):
        for marker in ('共', '合计是', '总计是', '共计', '拢共', '统共'):
            with self.subTest(marker=marker):
                self.assert_sales('卖了30份牛肉面' + marker + '300元卖了20份炒饭200元',
                                  [('牛肉面', 30, 300), ('炒饭', 20, 200)])

    def test_quantity_first_money_uses_complete_decimal_and_grouped_tokens(self):
        for token, amount in (('12.5元', 12.5), ('12.0元', 12), ('1.2万', 12000),
                              ('12,500元', 12500), ('1,200元', 1200)):
            with self.subTest(token=token):
                text = '卖了30份牛肉面' + token
                result = self.assert_sales(text, [('牛肉面', 30, amount)])
                sale = result['sales'][0]
                self.assertEqual(text[sale['start']:sale['end']], text)

    def test_quantity_first_explicit_unit_price_accepts_filler_verb(self):
        for marker in ('一份卖', '每份卖', '单价是', '每个是'):
            with self.subTest(marker=marker):
                result = self.assert_sales('卖了30份牛肉面' + marker + '12元', [('牛肉面', 30, 360)])
                self.assertEqual(result['sales'][0]['unit_price'], 12)

    def test_quantity_first_metadata_after_anchor_is_preserved(self):
        result = self.assert_sales('卖了30份美团外卖牛肉面卖了20份饿了么自取炒饭',
                                   [('牛肉面', 30, None), ('炒饭', 20, None)])
        self.assertEqual([(s['platform'], s['channel']) for s in result['sales']],
                         [('美团', '配送'), ('饿了么', '自取')])

    def test_unknown_quantity_suffix_does_not_hide_expense_amount(self):
        for text, category, amount, leftover in (('房租8000人手600', '房租', 8000, '600'),
                                                 ('水电200包80', '水电', 200, '80')):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual([(e['category'], e['amount']) for e in result['entries']],
                                 [(category, amount)])
                self.assertIn(leftover, result['leftovers'])

    def test_ambiguous_mixed_product_boundary_is_not_guessed(self):
        result = self.parse('卖了30份牛肉面炒饭卖了20份')
        self.assertEqual(result['sales'], [])
        self.assertEqual(result['entries'], [])
        self.assertTrue(any('牛肉面炒饭' in s for s in result['leftovers']))
        for boundary in ('，', '今天', '美团'):
            with self.subTest(boundary=boundary):
                self.assert_sales('卖了30份牛肉面' + boundary + '炒饭卖了20份',
                                  [('牛肉面', 30, None), ('炒饭', 20, None)])

    def test_adjacent_category_has_own_amount_but_payment_tail_is_ambiguous(self):
        result = self.parse('牛肉面卖了30份300元房租8000水电200')
        self.assertEqual(result['sales'][0]['amount'], 300)
        self.assertEqual([(e['category'], e['amount']) for e in result['entries']],
                         [('房租', 8000), ('水电', 200)])
        self.assertEqual(result['leftovers'], [])
        result = self.parse('300元交房租8000')
        self.assertEqual([(e['category'], e['amount'], e['item']) for e in result['entries']],
                         [('房租', 8000, '房租')])
        self.assertIn('300元', result['leftovers'])

    def test_consecutive_expense_categories_and_repetitions(self):
        for text, expected in (
            ('房租8000水电200人工600', [('房租', 8000), ('水电', 200), ('人工', 600)]),
            ('交房租8000付水电200发工资600', [('房租', 8000), ('水电', 200), ('人工', 600)]),
            ('房租8000房租9000', [('房租', 8000), ('房租', 9000)]),
            ('房租八千水电二百', [('房租', 8000), ('水电', 200)]),
        ):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual([(e['category'], e['amount']) for e in result['entries']], expected)
                self.assertEqual(result['sales'], [])
                self.assertEqual(result['leftovers'], [])

    def test_entry_dates_and_expense_months_are_local(self):
        result = self.parse('昨天付11月房租8000今天交10月水电200')
        self.assertEqual([(e['date'], e['period_month'], e['category'], e['amount'])
                          for e in result['entries']],
                         [('2026-10-23', '2026-11', '房租', 8000),
                          ('2026-10-24', '2026-10', '水电', 200)])

    def test_month_amount_category_order_keeps_each_expense_local(self):
        for first, second, sep in itertools.product(
                ('10月8000元房租', '10月8000房租', '十月八千房租', '10月房租8000'),
                ('11月9000元水电', '11月9000水电', '十一月九千水电', '11月水电9000'),
                ('', '，', '；')):
            text = first + sep + second
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual([(e['date'], e['period_month'], e['category'], e['amount'])
                                  for e in result['entries']],
                                 [('2026-10-24', '2026-10', '房租', 8000),
                                  ('2026-10-24', '2026-11', '水电', 9000)])
                self.assertEqual(result['sales'], [])
                self.assertEqual(result['leftovers'], [])

    def test_invalid_explicit_dates_cannot_fall_back_to_today_or_day_only(self):
        for date in ('2月29日', '2月31日', '2026年2月29日', '10月32日', '10月99日',
                     '2026-02-30', '2026-13-01', '32号'):
            for suffix in ('房租8000', '牛肉面卖了30份300元', '客流120人'):
                with self.subTest(date=date, suffix=suffix):
                    with self.assertRaisesRegex(ValueError, '日期'):
                        self.parse(date + suffix)

    def test_valid_leap_dates_use_explicit_or_base_year(self):
        for date, base in (('2024年2月29日', BASE), ('2024-02-29', BASE),
                           ('2月29日', dt.date(2024, 10, 24))):
            with self.subTest(date=date, base=base):
                result = parser.parse(date + '牛肉面卖了30份300元房租8000', base)
                self.assertEqual([(s['date'], s['product_name'], s['amount']) for s in result['sales']],
                                 [('2024-02-29', '牛肉面', 300)])
                self.assertEqual([(e['date'], e['amount']) for e in result['entries']],
                                 [('2024-02-29', 8000)])
                self.assertEqual(result['leftovers'], [])

    def test_ingest_valid_dates_keep_source_observations_and_ledger_aligned(self):
        for date, base, expected in (
                ('2024年2月29日', BASE, '2024-02-29'),
                ('2025年3月1日', BASE, '2025-03-01'),
                ('2024-02-29', BASE, '2024-02-29'),
                ('2月29日', dt.date(2024, 10, 24), '2024-02-29'),
                ('23号', BASE, '2026-10-23')):
            with self.subTest(date=date, base=base), contextlib.closing(SqliteStore(':memory:')) as store:
                text = date + '客流10人牛肉面卖了30份300元房租8000'
                result = ingest(store, 's', text, base=base, dedup_ref='aligned-date')
                self.assertEqual(result['status'], 'parsed')
                record = store.record_find('s', 'aligned-date')
                self.assertEqual((record['raw_text'], record['occurred_at'], record['parse_base_date']),
                                 (text, expected, base.isoformat()))
                entries = store.list_entries('s', '0000', '9999')
                sales = store.list_sales('s', '0000', '9999')
                rows = store.observation_list('s', '0000', '9999')
                self.assertEqual([(e['date'], e['amount']) for e in entries], [(expected, 8000)])
                self.assertEqual([(s['date'], s['amount']) for s in sales], [(expected, 300)])
                self.assertEqual(len(rows), 4)
                self.assertTrue(all(o['occurred_at'] == expected and o['record_id'] == record['id']
                                    for o in rows))
                for table, row in (('entry', entries[0]), ('sale', sales[0])):
                    self.assertEqual(store.record_for_origin('s', f"{table}:{row['id']}")['id'],
                                     record['id'])
                before = store.export_all('s')
                duplicate = ingest(store, 's', '今天营业额9999', base=dt.date(2028, 1, 1),
                                   dedup_ref='aligned-date')
                self.assertTrue(duplicate['duplicate'])
                self.assertEqual(store.export_all('s'), before)

    def test_ingest_observation_dates_follow_only_preceding_date_tokens(self):
        text = ('客流1人；2024年2月29日客流10人房租8000；'
                '2025年3月1日客流20人水电200；今天客流30人工资600')
        with contextlib.closing(SqliteStore(':memory:')) as store:
            result = ingest(store, 's', text, base=BASE, dedup_ref='local-dates')
            self.assertEqual(result['status'], 'parsed')
            self.assertEqual(result['record']['occurred_at'], BASE.isoformat())
            rows = store.observation_list('s', '0000', '9999')
            self.assertEqual({o['value_num']: o['occurred_at'] for o in rows if o['kind'] == '客流'},
                             {1: '2026-10-24', 10: '2024-02-29',
                              20: '2025-03-01', 30: '2026-10-24'})
            self.assertEqual([(e['date'], e['category'], e['amount'])
                              for e in result['parsed']['entries']],
                             [('2024-02-29', '房租', 8000), ('2025-03-01', '水电', 200),
                              ('2026-10-24', '人工', 600)])

    def test_ingest_expense_month_is_not_observation_payment_date(self):
        text = '2026年10月23日客流10人11月8000元房租'
        with contextlib.closing(SqliteStore(':memory:')) as store:
            result = ingest(store, 's', text, base=BASE, dedup_ref='payment-date')
            self.assertEqual(result['status'], 'parsed')
            self.assertEqual(result['record']['occurred_at'], '2026-10-23')
            entry = result['parsed']['entries'][0]
            self.assertEqual((entry['date'], entry['period_month'], entry['amount']),
                             ('2026-10-23', '2026-11', 8000))
            self.assertTrue(all(o['occurred_at'] == '2026-10-23'
                                for o in store.observation_list('s', '0000', '9999')))

    def test_ingest_invalid_dates_preserves_rejected_source_without_projection(self):
        with contextlib.closing(SqliteStore(':memory:')) as store:
            store.ensure_shop('s')
            for index, date in enumerate(('2月29日', '2月31日', '2026-02-30')):
                text = '营业额1000，' + date + '牛肉面卖了30份300元，客流120人'
                ref = 'invalid-date:' + str(index)
                with self.subTest(date=date):
                    with self.assertRaisesRegex(ValueError, '日期'):
                        ingest(store, 's', text, base=BASE, dedup_ref=ref)
                    source = store.record_find('s', ref)
                    self.assertEqual((source['raw_text'], source['parse_status']), (text, 'rejected'))
                    before = store.export_all('s')
                    with self.assertRaises(ValueError):
                        ingest(store, 's', '换成合法正文营业额9999', base=BASE, dedup_ref=ref)
                    self.assertEqual(store.export_all('s'), before)
            self.assertEqual(store.list_entries('s', '0000', '9999'), [])
            self.assertEqual(store.list_sales('s', '0000', '9999'), [])
            self.assertEqual(store.observation_list('s', '0000', '9999'), [])

    def test_purchase_quantity_and_payment_verb_stay_together(self):
        result = self.parse('买了50斤牛肉花了680交房租8000买了三箱啤酒花了240水电200')
        self.assertEqual([(e['type'], e['category'], e['item'], e['qty'], e['amount'])
                          for e in result['entries']],
                         [('purchase', '食材成本', '牛肉', 50, 680),
                          ('expense', '房租', '房租', None, 8000),
                          ('purchase', '食材成本', '啤酒', 3, 240),
                          ('expense', '水电', '水电', None, 200)])
        self.assertEqual(result['leftovers'], [])

    def test_explicit_sale_total_cannot_become_the_next_purchase(self):
        result = self.parse('牛肉面卖了30份共300元买了牛肉680')
        self.assertEqual([(s['product_name'], s['qty'], s['amount']) for s in result['sales']],
                         [('牛肉面', 30, 300)])
        self.assertEqual([(e['type'], e['item'], e['amount']) for e in result['entries']],
                         [('purchase', '牛肉', 680)])
        self.assertEqual(result['leftovers'], [])

    def test_ambiguous_leading_amount_is_not_an_extra_expense(self):
        result = self.parse('牛肉面卖了30份300元交房租8000')
        self.assertIsNone(result['sales'][0]['amount'])
        self.assertEqual([(e['category'], e['amount']) for e in result['entries']], [('房租', 8000)])
        self.assertTrue(any('300元' in text for text in result['leftovers']))

    def test_extra_sale_amount_cannot_fall_back_to_an_expense(self):
        for text, expected, unresolved in (
                ('牛肉面卖了30份每份12元合计360元', 360, '合计360元'),
                ('卖了30份牛肉面每份12元合计400元', 360, '合计400元'),
                ('牛肉面卖了30份共300元200元', 300, '200元')):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual(result['sales'][0]['amount'], expected)
                self.assertEqual(result['entries'], [])
                self.assertIn(unresolved, result['leftovers'])
        result = self.parse('牛肉面卖了30份300元今天买了牛肉680')
        self.assertEqual(result['sales'][0]['amount'], 300)
        self.assertEqual([(e['type'], e['amount']) for e in result['entries']], [('purchase', 680)])
        self.assertEqual(result['leftovers'], [])

    def test_dates_inside_masked_sales_still_control_following_expenses(self):
        result = self.parse('昨天牛肉面卖了30份300元今天炒饭卖了20份200元房租8000')
        self.assertEqual([(e['date'], e['category'], e['amount']) for e in result['entries']],
                         [('2026-10-24', '房租', 8000)])
        self.assertEqual(result['leftovers'], [])

    def test_later_explicit_date_cannot_change_an_earlier_undated_entry(self):
        for text in ('房租8000昨天水电200', '房租8000，昨天水电200'):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual([(e['date'], e['category']) for e in result['entries']],
                                 [('2026-10-24', '房租'), ('2026-10-23', '水电')])
        result = self.parse('房租8000昨天牛肉面卖了30份300元水电200')
        self.assertEqual([(e['date'], e['category']) for e in result['entries']],
                         [('2026-10-24', '房租'), ('2026-10-23', '水电')])

    def test_expense_category_prefix_is_not_a_quantity_unit(self):
        for text in ('房租8000包装80', '房租八千包装八十', '房租8000餐盒80'):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertEqual([(e['category'], e['amount']) for e in result['entries']],
                                 [('房租', 8000), ('包装耗材', 80)])
                self.assertEqual(result['leftovers'], [])

    def test_one_accounting_anchor_does_not_fan_out_to_extra_rows(self):
        for text, amount, leftover in (('交房租300元8000', 300, '8000'),
                                       ('交房租8000前300元', 8000, '300元'),
                                       ('300元房租8000水电200', 8000, '300元')):
            with self.subTest(text=text):
                result = self.parse(text)
                expected = [('房租', amount)] + ([('水电', 200)] if '水电' in text else [])
                self.assertEqual([(e['category'], e['amount']) for e in result['entries']], expected)
                self.assertIn(leftover, result['leftovers'])

    def test_trailing_category_binds_only_its_immediate_amount(self):
        result = self.parse('营业额1280元300元成本')
        self.assertEqual([(e['type'], e['amount']) for e in result['entries']],
                         [('revenue', 1280), ('purchase', 300)])
        self.assertEqual(result['leftovers'], [])

    def test_ingest_preserves_source_and_same_request_id_once(self):
        text = '卖了30份牛肉面300元卖了20份炒饭200元房租8000水电200'
        with contextlib.closing(SqliteStore(':memory:')) as store:
            store.ensure_shop('s')
            result = ingest(store, 's', text, base=BASE, dedup_ref='boundaries:1')
            self.assertEqual((len(result['sale_ids']), len(result['entry_ids'])), (2, 2))
            self.assertEqual(result['status'], 'parsed')
            self.assertEqual([(s['product_name'], s['qty'], s['amount'])
                              for s in result['parsed']['sales']],
                             [('牛肉面', 30, 300), ('炒饭', 20, 200)])
            self.assertEqual([(e['category'], e['amount']) for e in result['parsed']['entries']],
                             [('房租', 8000), ('水电', 200)])
            rid = result['record']['id']
            self.assertEqual(result['record']['raw_text'], text)
            for origin in ['sale:' + i for i in result['sale_ids']] + ['entry:' + i for i in result['entry_ids']]:
                self.assertEqual(store.record_for_origin('s', origin)['id'], rid)
                projected = store.observation_origin('s', origin + ':')
                self.assertTrue(projected)
                self.assertEqual({o['record_id'] for o in projected}, {rid})
            summary = store.summary('s', BASE.isoformat(), BASE.isoformat())
            self.assertEqual((summary['count'], summary['revenue'], summary['expense']), (4, 500, 8200))
            before = store.export_all('s')
            again = ingest(store, 's', '改写的正文9999', base=BASE, dedup_ref='boundaries:1')
            self.assertTrue(again['duplicate'])
            self.assertEqual(store.export_all('s'), before)

    def test_partial_ingest_review_keeps_rows_summary_and_source(self):
        from engine.review import apply_review, review_items

        with contextlib.closing(SqliteStore(':memory:')) as store:
            store.ensure_shop('s')
            text = '牛肉面卖了30份300元交房租8000'
            receipt = ingest(store, 's', text, base=BASE, dedup_ref='boundaries:partial')
            self.assertEqual(receipt['status'], 'partial')
            self.assertEqual(receipt['parsed']['leftovers'], ['300元'])
            self.assertEqual((len(receipt['entry_ids']), len(receipt['sale_ids'])), (1, 1))
            sale_id = receipt['sale_ids'][0]
            target = 'sale:' + sale_id
            item = next(i for i in review_items(store, 's') if i['id'] == target)
            self.assertIn('amount', item['missing'])
            rid = receipt['record']['id']
            apply_review(store, 's', 'boundaries:review', target,
                         {'amount': 300, 'platform': '美团'}, '牛肉面30份合计300元，美团')
            summary = store.summary('s', BASE.isoformat(), BASE.isoformat())
            self.assertEqual((summary['count'], summary['revenue'], summary['expense']), (2, 300, 8000))
            self.assertEqual(store.record_for_origin('s', target)['id'], rid)
            self.assertEqual({o['record_id'] for o in store.observation_origin('s', target + ':')}, {rid})
            self.assertEqual(store.record_for_origin('s', target)['raw_text'], text)
            before = store.export_all('s')
            again = ingest(store, 's', '不同正文9999', base=BASE, dedup_ref='boundaries:partial')
            self.assertTrue(again['duplicate'])
            self.assertEqual(store.export_all('s'), before)

    def test_ingest_summary_matrix_respects_payment_tail_ambiguity(self):
        forms = [('牛肉面卖了30份300元', '卖了30份牛肉面300元'),
                 ('炒饭卖了20份200元', '卖了20份炒饭200元')]
        expenses = ('房租8000水电200', '房租8000包装80', '交房租8000付水电200')
        for first, second, sep, expense in itertools.product(*forms, ('', '，', '；'), expenses):
            text = sep.join((first, second, expense))
            with self.subTest(text=text), contextlib.closing(SqliteStore(':memory:')) as store:
                store.ensure_shop('s')
                result = ingest(store, 's', text, base=BASE, dedup_ref='matrix')
                ambiguous = not sep and expense.startswith('交')
                self.assertEqual(result['status'], 'partial' if ambiguous else 'parsed')
                self.assertEqual((len(result['sale_ids']), len(result['entry_ids'])), (2, 2))
                self.assertEqual([(s['product_name'], s['qty'], s['amount'])
                                  for s in result['parsed']['sales']],
                                 [('牛肉面', 30, 300), ('炒饭', 20, None if ambiguous else 200)])
                self.assertEqual(result['parsed']['leftovers'], ['200元'] if ambiguous else [])
                summary = store.summary('s', BASE.isoformat(), BASE.isoformat())
                self.assertEqual(summary['count'], 4)
                self.assertEqual(summary['expense'], 8080 if '包装' in expense else 8200)
                if not ambiguous:
                    self.assertEqual(summary['revenue'], 500)

    def test_unambiguous_forms_are_invariant_under_punctuation(self):
        forms = [('牛肉面卖了30份300元', '卖了30份牛肉面300元'),
                 ('炒饭卖了20份200元', '卖了20份炒饭200元')]
        for first, second, sep in itertools.product(*forms, ('', '，', '；')):
            with self.subTest(first=first, second=second, sep=sep):
                self.assert_sales(first + sep + second,
                                  [('牛肉面', 30, 300), ('炒饭', 20, 200)])
        for sep in ('', '，', '；'):
            result = self.parse(sep.join(('房租8000', '水电200', '工资600')))
            self.assertEqual([(e['category'], e['amount']) for e in result['entries']],
                             [('房租', 8000), ('水电', 200), ('人工', 600)])


if __name__ == '__main__':
    unittest.main()
