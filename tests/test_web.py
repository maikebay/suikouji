"""本地测试页的真实 HTTP 边界，使用临时库，不污染网页试记数据。"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from engine.web import make_server


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(0, Path(cls.tmp.name) / "demo.db")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        conn.request(method, path, body=body, headers=headers or {"Content-Type": "application/json"})
        response = conn.getresponse()
        status, content_type, raw = response.status, response.getheader("Content-Type"), response.read()
        conn.close()
        return status, json.loads(raw) if "application/json" in content_type else raw.decode()

    def test_page_is_real_html_and_not_arbitrary_file_server(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('id="record-form"', page)
        self.assertIn("/api/records", page)
        self.assertEqual(self.request("GET", "/data/web-demo.db")[0], 404)
        self.assertEqual(self.request("GET", "/../engine/store.py")[0], 404)

    def test_mixed_record_persists_and_retry_is_idempotent(self):
        payload = {"request_id": "http-mixed", "text": "今天营业额1280，买了50斤牛肉680，客流120人，下雨"}
        status, result = self.request("POST", "/api/records", payload)
        self.assertEqual(status, 200)
        rid = result["receipt"]["record"]["id"]
        self.assertEqual(result["receipt"]["record"]["source"], "web")
        rows = [o for o in result["state"]["observations"] if o["record_id"] == rid]
        self.assertEqual(len(rows), 5)
        self.assertEqual(result["receipt"]["status"], "parsed")
        before = result["state"]
        status, duplicate = self.request("POST", "/api/records", payload)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["receipt"]["duplicate"])
        self.assertEqual(duplicate["state"], before)
        self.assertEqual(self.request("GET", "/api/state")[1], before)

    def test_nonmoney_partial_and_unparsed_never_add_ledger(self):
        before = self.request("GET", "/api/state")[1]["entries"]
        cases = [("客流零人，上座20桌，外卖30单，差评三个", "parsed"),
                 ("气温28度", "unparsed"), ("今天下雨，生意差", "partial")]
        for i, (text, expected) in enumerate(cases):
            status, result = self.request("POST", "/api/records", {"text": text, "request_id": f"nonmoney-{i}"})
            self.assertEqual(status, 200)
            self.assertEqual(result["receipt"]["status"], expected)
            self.assertEqual(result["state"]["entries"], before)
            self.assertTrue(any(r["raw_text"] == text for r in result["state"]["records"]))
        zeros = self.request("GET", "/api/state")[1]["observations"]
        self.assertTrue(any(r["measure"] == "人数" and r["value_num"] == 0 for r in zeros))

    def test_recipe_and_import_end_to_end(self):
        csv = '日期,类型,商品,数量,单位,金额\n2026-10-01,进货,测试牛肉,50,斤,680\n'
        self.assertEqual(self.request('POST', '/api/import/preview', {'csv': csv})[1]['plan']['kind'], 'entries')
        self.assertEqual(self.request('POST', '/api/import/commit', {'csv': csv})[1]['result']['imported'], 1)
        payload = {'product': '测试牛肉面', 'category': '面食', 'recipe': '测试牛肉0.08公斤',
                   'units': 1, 'price': 12, 'effective_date': '2026-10-01'}
        status, data = self.request('POST', '/api/recipes', payload)
        self.assertEqual(status, 200)
        cost = next(c for c in data['state']['costs'] if c['product']['name'] == '测试牛肉面')
        self.assertEqual(cost['unit_cost'], 2.176)
        self.assertIsNone(cost['actual_cost'])
        sale_csv = '日期,平台,渠道,商品品类,商品,数量,销售额\n2026-10-23,美团,配送,面食,测试牛肉面,30,360\n'
        self.assertEqual(self.request('POST', '/api/import/commit', {'csv': sale_csv})[1]['result']['imported'], 1)
        self.assertTrue(self.request('POST', '/api/import/commit', {'csv': sale_csv})[1]['result']['duplicate'])
        status, data = self.request('GET', '/api/state?month=2026-10')
        self.assertEqual(status, 200)
        row = next(r for r in data['summary']['sales_analysis'] if r['product_name'] == '测试牛肉面')
        self.assertEqual((row['platform'], row['channel'], row['product_category']), ('美团', '配送', '面食'))
        self.assertEqual(row['standard_cost'], 65.28)
        self.assertEqual(self.request('GET', '/api/state?month=bad')[0], 400)
        before = self.request('GET', '/api/state')[1]
        self.assertEqual(self.request('POST', '/api/recipes', {**payload, 'units': 0})[0], 400)
        self.assertEqual(self.request('GET', '/api/state')[1], before)

    def test_result_card_data_and_review_endpoint_never_double_book(self):
        status, data = self.request("POST", "/api/records",
                                    {"text": "今天牛肉面卖了30份", "request_id": "http-sale"})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["written"]["entries"]), 0)
        sale = data["written"]["sales"][0]
        self.assertIsNone(sale["amount"])
        target = f"sale:{sale['id']}"
        item = next(i for i in data["state"]["review"] if i["id"] == target)
        self.assertEqual(item["missing"], ["amount", "platform"])
        before = data["state"]
        payload = {"request_id": "http-review", "target_id": target,
                   "fields": {"amount": 300, "platform": "美团"}, "reason": "美团卖了30份共300"}
        status, reviewed = self.request("POST", "/api/review", payload)
        self.assertEqual(status, 200)
        self.assertFalse(reviewed["review"]["duplicate"])
        self.assertEqual(reviewed["review"]["changed"], {"amount": 300.0, "platform": "美团"})
        self.assertEqual(reviewed["state"]["summary"]["count"], before["summary"]["count"])
        self.assertEqual(len(reviewed["state"]["sales"]), len(before["sales"]))
        row = next(r for r in reviewed["state"]["sales"] if r["id"] == sale["id"])
        self.assertEqual((row["amount"], row["platform"]), (300.0, "美团"))
        self.assertNotIn(target, [i["id"] for i in reviewed["state"]["review"]])
        # 同一个修正编号重试只生效一次，改不回别的值
        again = self.request("POST", "/api/review", {**payload, "fields": {"amount": 999}})[1]
        self.assertTrue(again["review"]["duplicate"])
        self.assertEqual(next(r for r in again["state"]["sales"] if r["id"] == sale["id"])["amount"], 300.0)
        self.assertEqual(len(again["state"]["records"]), len(reviewed["state"]["records"]))
        # 修正依据作为一条原话保留，来源仍在
        self.assertTrue(any(r["raw_text"] == "美团卖了30份共300" and r["source"] == "review"
                            for r in again["state"]["records"]))
        self.assertTrue(any(r["raw_text"] == "今天牛肉面卖了30份" and r["source"] == "web"
                            for r in again["state"]["records"]))

    def test_consecutive_sales_and_expenses_http_keep_four_rows(self):
        payload = {"request_id": "consecutive-http",
                   "text": "卖了30份牛肉面300元卖了20份炒饭200元房租8000水电200"}
        status, data = self.request("POST", "/api/records", payload)
        self.assertEqual(status, 200)
        self.assertEqual(data['receipt']['status'], 'parsed')
        self.assertEqual([(s['product_name'], s['qty'], s['amount'])
                          for s in data['written']['sales']],
                         [('牛肉面', 30, 300), ('炒饭', 20, 200)])
        self.assertEqual([(e['category'], e['amount']) for e in data['written']['entries']],
                         [('房租', 8000), ('水电', 200)])
        before = data['state']
        status, again = self.request('POST', '/api/records', payload)
        self.assertEqual(status, 200)
        self.assertTrue(again['receipt']['duplicate'])
        self.assertEqual(again['state'], before)

    def test_ambiguous_sale_tail_http_returns_partial_not_extra_expense(self):
        status, data = self.request('POST', '/api/records', {
            'request_id': 'ambiguous-tail-http', 'text': '牛肉面卖了30份300元交房租8000'})
        self.assertEqual(status, 200)
        self.assertEqual(data['receipt']['status'], 'partial')
        self.assertEqual([(e['category'], e['amount']) for e in data['written']['entries']],
                         [('房租', 8000)])
        self.assertIsNone(data['written']['sales'][0]['amount'])
        self.assertTrue(any('300元' in s for s in data['receipt']['parsed']['leftovers']))
        rid = data['receipt']['record']['id']
        self.assertEqual(next(r for r in data['state']['records'] if r['id'] == rid)['parse_status'],
                         'partial')

    def test_month_amount_category_order_http_preserves_period_and_source(self):
        payload = {'request_id': 'month-leading-http', 'text': '10月8000元房租11月9000元水电'}
        status, data = self.request('POST', '/api/records', payload)
        self.assertEqual(status, 200)
        self.assertEqual(data['receipt']['status'], 'parsed')
        self.assertEqual([(e['period_month'], e['category'], e['amount'])
                          for e in data['written']['entries']],
                         [('2026-10', '房租', 8000), ('2026-11', '水电', 9000)])
        self.assertEqual(data['receipt']['record']['raw_text'], payload['text'])
        status, retry = self.request('POST', '/api/records', {**payload, 'text': '营业额9999'})
        self.assertEqual(status, 200)
        self.assertTrue(retry['receipt']['duplicate'])
        self.assertEqual(retry['state'], data['state'])

    def test_explicit_year_http_aligns_source_observations_and_ledger(self):
        payload = {'request_id': 'explicit-year-http',
                   'text': '2024年2月29日客流10人牛肉面卖了30份300元房租8000'}
        status, data = self.request('POST', '/api/records', payload)
        self.assertEqual(status, 200)
        self.assertEqual(data['receipt']['status'], 'parsed')
        record = data['receipt']['record']
        self.assertEqual((record['raw_text'], record['occurred_at']),
                         (payload['text'], '2024-02-29'))
        source = next(r for r in data['state']['records'] if r['id'] == record['id'])
        self.assertEqual(source['occurred_at'], '2024-02-29')
        self.assertEqual([(e['date'], e['amount']) for e in data['written']['entries']],
                         [('2024-02-29', 8000)])
        self.assertEqual([(s['date'], s['amount']) for s in data['written']['sales']],
                         [('2024-02-29', 300)])
        rows = [o for o in data['state']['observations'] if o['record_id'] == record['id']]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(o['occurred_at'] == '2024-02-29' for o in rows))
        status, retry = self.request('POST', '/api/records', {**payload, 'text': '今天营业额9999'})
        self.assertEqual(status, 200)
        self.assertTrue(retry['receipt']['duplicate'])
        self.assertEqual(retry['state'], data['state'])

    def test_invalid_explicit_dates_http_rejects_without_ledger_or_observations(self):
        before = self.request('GET', '/api/state')[1]
        for index, date in enumerate(('2026年2月29日', '2026年2月31日', '2026-02-30')):
            request_id = 'invalid-date-http:' + str(index)
            text = '营业额1000，' + date + '房租8000，客流120人'
            status, data = self.request('POST', '/api/records', {'request_id': request_id, 'text': text})
            self.assertEqual(status, 400)
            self.assertIn('日期', data['error'])
            state = self.request('GET', '/api/state')[1]
            for key in ('entries', 'sales', 'observations'):
                self.assertEqual(state[key], before[key])
            source = next(r for r in state['records'] if r['raw_text'] == text)
            self.assertEqual(source['parse_status'], 'rejected')
            self.assertEqual(self.request('POST', '/api/records', {
                'request_id': request_id, 'text': '营业额9999'})[0], 400)
            self.assertEqual(self.request('GET', '/api/state')[1], state)

    def test_review_rejects_bad_input_without_writing(self):
        status, data = self.request("POST", "/api/records",
                                    {"text": "买了牛肉680", "request_id": "http-purchase"})
        entry = data["written"]["entries"][0]
        target = f"entry:{entry['id']}"
        before = data["state"]
        for payload in ({"request_id": "bad-1", "target_id": target, "fields": [], "reason": "格式不对"},
                        {"request_id": "bad-2", "target_id": target, "fields": {"qty": 50}},
                        {"request_id": "bad-3", "target_id": target, "fields": {"qty": -1}, "reason": "负数"},
                        {"request_id": "bad-4", "target_id": "entry:nope", "fields": {"qty": 1}, "reason": "不存在"},
                        {"request_id": "bad-5", "target_id": target, "fields": {"qty": 50}, "reason": "   "}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/api/review", payload)[0], 400)
        self.assertEqual(self.request("GET", "/api/state")[1], before)
        payload = {"request_id": "cross-review", "target_id": target, "fields": {"qty": 50}, "reason": "补数量"}
        self.assertEqual(self.request("POST", "/api/review", payload,
                         {"Content-Type": "application/json", "Origin": "https://elsewhere.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/review", payload,
                         {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("GET", "/api/state")[1], before)

    def test_deep_json_and_overflow_review_are_json_400(self):
        status, data = self.request("POST", "/api/records",
                                    {"text": "买了牛肉680", "request_id": "deep-seed"})
        target = 'entry:' + data['written']['entries'][0]['id']
        before = self.request("GET", "/api/state")[1]
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        raw = ('[' * 1200 + ']' * 1200).encode()
        conn.request("POST", "/api/review", body=raw,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader('Content-Type').split(';')[0], 'application/json')
        self.assertIn('error', json.loads(response.read()))
        conn.close()
        for value in (True, 10 ** 400):
            status, _ = self.request("POST", "/api/review", {
                "request_id": "overflow-" + str(len(str(value))), "target_id": target,
                "fields": {"amount": value}, "reason": "非法数字"})
            self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/state")[1], before)

    def test_validation_and_origin_rejection_do_not_write(self):
        before = self.request("GET", "/api/state")[1]
        for payload in ({}, [], {"text": "   ", "request_id": "empty"},
                        {"text": "客流120人"}, {"text": "字" * 2001, "request_id": "long"}):
            self.assertEqual(self.request("POST", "/api/records", payload)[0], 400)
        payload = {"text": "营业额1000", "request_id": "cross-origin"}
        self.assertEqual(self.request("POST", "/api/records", payload,
                         {"Content-Type": "application/json", "Origin": "https://elsewhere.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/records", payload,
                         {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "elsewhere.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state")[1], before)


if __name__ == "__main__":
    unittest.main()
