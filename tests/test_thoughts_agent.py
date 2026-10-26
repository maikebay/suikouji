"""Thought capture and query contracts, using disposable ledgers only."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from engine.agent_service import AgentError, AgentService, Principal
from engine.store import SqliteStore
from tests import test_agent_http_contract as http_contract


P = Principal("agent", "owner")
TEXT = "今天营业额1300元。记个想法：明天试南瓜甜品 #新品 #南瓜"


def structured():
    return {"entries": [], "sales": [], "observations": [], "leftovers": [], "thoughts": [{
        "date": "2026-10-26", "body": "明天试南瓜甜品", "title": "南瓜甜品",
        "source_quote": "明天试南瓜甜品 #新品 #南瓜", "tags": [" #新品 ", "南瓜", "新品"],
    }]}


class ThoughtsAgentTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_shop("shop", owner_id="owner", timezone="Asia/Shanghai")
        self.service = AgentService(self.store)

    def capture(self, value=None, request_id="one", text=TEXT):
        return self.service.capture_text(P, "shop", text, request_id,
            received_at="2026-10-26T20:00:00+08:00", structured=structured() if value is None else value)

    def test_thought_only_no_financial_effect_raw_preserved_and_durable_replay(self):
        original = structured()
        with patch("engine.agent_service.ingest", side_effect=AssertionError("must not parse")):
            first = self.capture(original)
        self.assertEqual(original, structured())
        self.assertEqual(first["capture"]["raw_text"], TEXT)
        self.assertEqual(first["status"], "parsed")
        self.assertEqual(first["entries"], [])
        self.assertEqual(first["sales"], [])
        self.assertEqual(first["observations"], [])
        self.assertEqual(first["thoughts"][0]["tags"], ["新品", "南瓜"])
        self.assertEqual(first["thoughts"][0]["workspace_id"], "shop")
        self.assertEqual(first["thoughts"][0]["capture_id"], first["capture"]["id"])
        self.assertEqual(json.loads(first["capture"]["ledger_refs"]), [])
        self.assertNotIn("assertion", first["thoughts"][0])
        self.store.workspace_set_profile("shop", "general", "owner")
        self.assertEqual(self.capture(original), {**first, "duplicate": True})
        with self.assertRaises(AgentError) as caught:
            self.capture(structured(), "new")
        self.assertEqual(caught.exception.code, "profile_unsupported")

    def test_mixed_event_time_uses_financial_date_not_note_date(self):
        data = structured()
        data["entries"] = [{"date": "2026-10-25", "type": "revenue", "category": "营业额",
            "amount": 1300, "amount_basis": "gross", "granularity": "daily",
            "assertion": "actual", "source_quote": "今天营业额1300元"}]
        result = self.capture(data)
        self.assertEqual(result["capture"]["occurred_at"], "2026-10-25")
        self.assertEqual(result["thoughts"][0]["date"], "2026-10-26")
        self.assertEqual(self.service.summary(P, "shop")["revenue"], 1300)
        timeline = self.service.timeline(P, "shop", types="thought")["items"]
        self.assertEqual(timeline[0]["projection_counts"]["thoughts"], 1)
        self.assertEqual(len(timeline[0]["ledger_preview"]), 1)
        self.assertEqual(self.service.capture_detail(P, "shop", result["capture"]["id"])["thoughts"], result["thoughts"])

    def test_optional_defaults_do_not_change_fingerprint_or_old_receipt(self):
        old = {"entries": [], "sales": [], "observations": [], "leftovers": [TEXT]}
        first = self.capture(old)
        self.assertEqual(first["normalization"], {**old, "thoughts": []})
        self.assertNotIn("thoughts", old)
        with self.assertRaises(AgentError) as caught:
            self.capture({**old, "thoughts": []})
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        # Model an already-completed v5 receipt; replay must not decorate it.
        legacy = {"status": "partial", "duplicate": False, "normalization": old, "capture": {"id": "old"}}
        self.store.conn.execute("UPDATE agent_request SET response=?", (json.dumps(legacy),))
        self.store.conn.commit()
        with patch("engine.agent_service.validate_structured", side_effect=AssertionError("cached first")):
            self.assertEqual(self.capture(old), {**legacy, "duplicate": True})

    def test_invalid_thoughts_rollback_and_never_fallback(self):
        cases = [None, {}, "note", [None]]
        row = structured()["thoughts"][0]
        for key, values in {
            "date": ["2026-02-30", "20261026", "2026-1-01", None],
            "body": ["", " ", "x" * 2001, None],
            "title": ["", "x" * 81, None],
            "source_quote": ["fabricated", "", None],
            "tags": [None, "新品", ["x"] * 21, ["##新品"], ["a b"], ["#"], ["a\x00"]],
            "assertion": ["actual"], "type": ["plan"], "amount": [300], "capture_id": ["other"],
            "due_at": ["2026-10-27"],
        }.items():
            cases.extend([[{**row, key: value}] for value in values])
        cases.append([row] * 51)
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(AgentError) as caught:
                self.capture({**structured(), "thoughts": invalid})
            self.assertEqual(caught.exception.code, "invalid_structured")
            self.assertEqual(self.store.record_list("shop", "0000", "9999"), [])
            self.assertIsNone(self.store.agent_request_get(P.id, "shop", "capture", "one"))

    def test_projection_and_receipt_failures_rollback_everything(self):
        for method in ("thought_add", "record_detail", "agent_request_finish"):
            with self.subTest(method=method), patch.object(self.store, method, side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    self.capture()
            self.assertEqual(self.store.record_list("shop", "0000", "9999"), [])
            self.assertEqual(self.store.thought_list("shop")["items"], [])
            self.assertIsNone(self.store.agent_request_get(P.id, "shop", "capture", "one"))

    def test_prd_messy_agent_notes_do_not_invent_weather_or_exact_revenue(self):
        text = ("今天天气很好，生意还可以，老顾客非常的多，大概做了1300。"
                "明天我想做一个新品，然后打算用南瓜来做一个甜品。南瓜甜品可以搭配木薯、银耳、枸杞等。")
        notes = ["生意还可以，老顾客非常的多", "明天我想做一个新品，然后打算用南瓜来做一个甜品。南瓜甜品可以搭配木薯、银耳、枸杞等。"]
        data = {"entries": [], "sales": [], "observations": [],
                "thoughts": [{"date": "2026-10-26", "body": quote, "source_quote": quote} for quote in notes],
                "leftovers": ["今天天气很好", "大概做了1300"]}
        result = self.capture(data, text=text)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["capture"]["raw_text"], text)
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["observations"], [])
        self.assertEqual(len(result["thoughts"]), 2)
        self.assertEqual(result["parsed"]["leftovers"], data["leftovers"])
        self.assertEqual(self.service.summary(P, "shop")["revenue"], 0)

    def test_raw_tag_changes_conflict_and_empty_tags_are_optional(self):
        self.capture()
        changed = structured()
        changed["thoughts"][0]["tags"] = ["新品", "南瓜"]
        with self.assertRaises(AgentError) as caught:
            self.capture(changed)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        del changed["thoughts"][0]["tags"]
        fresh = self.capture(changed, "untagged")
        self.assertEqual(fresh["thoughts"][0]["tags"], [])
        self.assertEqual(fresh["status"], "parsed")

    def test_shared_projection_limit_and_literal_casefold_search(self):
        data = structured()
        data["thoughts"] *= 50
        accepted = self.capture(data)
        self.assertEqual(len(accepted["thoughts"]), 50)
        data["observations"] = [{"date": "2026-10-26", "kind": "天气", "measure": "天气",
                                  "value_text": "阴", "source_quote": "阴天", "assertion": "actual"}]
        with self.assertRaises(AgentError):
            self.capture(data, "too-many", text=TEXT + "阴天")
        other = structured()
        other["thoughts"][0]["body"] = "Straße 100% _literal [abc]"
        other["thoughts"][0]["title"] = "ABC"
        self.capture(other, "search")
        for keyword in ("STRASSE", "100%", "_literal", "[abc]"):
            self.assertEqual(len(self.service.thoughts(P, "shop", q=keyword)["items"]), 1)
        self.assertEqual(self.service.thoughts(P, "shop", q=".*")["items"], [])

    def test_list_filters_cursor_binding_and_strict_inputs(self):
        for index in range(4):
            self.capture(request_id=str(index))
        first = self.service.thoughts(P, "shop", tags=["#新品", "南瓜"], q="南瓜", page_size=2)
        self.assertTrue(first["has_more"])
        self.assertIsInstance(first["next_before"], str)
        second = self.service.thoughts(P, "shop", tags=["新品", "南瓜"], q="南瓜", page_size=2, before=first["next_before"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({row["id"] for row in first["items"] + second["items"]}), 4)
        self.assertEqual(self.service.thoughts(P, "shop", tags=["不存在"])["items"], [])
        cases = [{"start": "2026-1-01"}, {"end": ""}, {"start": "2026-10-27", "end": "2026-10-26"},
                 {"page_size": True}, {"page_size": 1.5}, {"page_size": "1.0"}, {"page_size": " 2"},
                 {"page_size": 0}, {"page_size": 101}, {"tags": ["x"] * 21}, {"tags": ["a b"]},
                 {"q": "x" * 201}, {"before": "bad"}, {"before": ""},
                 {"before": first["next_before"], "q": "changed"}]
        for filters in cases:
            with self.subTest(filters=filters), self.assertRaises(AgentError):
                self.service.thoughts(P, "shop", **filters)
        self.store.ensure_shop("second", owner_id="owner")
        with self.assertRaises(AgentError):
            self.service.thoughts(P, "second", tags=["新品", "南瓜"], q="南瓜", before=first["next_before"])
        with self.assertRaises(AgentError) as caught:
            self.service.thoughts(Principal("other", "other"), "shop")
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(AgentError):
            self.service.timeline(P, "shop", types="plan")


class ThoughtsHttpTests(unittest.TestCase):
    setUp = http_contract.AgentHttpContractTests.setUp
    request = http_contract.AgentHttpContractTests.request

    def test_http_write_query_repeated_tags_and_invalid_filters(self):
        status, _, first = self.request("POST", "/v1/captures", {
            "workspace_id": "shop", "request_id": "thought", "text": TEXT, "structured": structured()})
        self.assertEqual(status, 200)
        query = urlencode([("workspace_id", "shop"), ("tag", "新品"), ("tag", "南瓜")])
        status, _, result = self.request("GET", "/v1/thoughts?" + query)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"], first["thoughts"])
        for suffix in ("&tag=", "&page_size=1.5", "&start=2026-02-30", "&q=" + "a" * 201,
                       "&page_size=2&page_size=3", "&unknown=value"):
            self.assertEqual(self.request("GET", "/v1/thoughts?workspace_id=shop" + suffix)[0], 400)
        self.assertEqual(self.request("GET", "/v1/timeline?workspace_id=shop&types=")[0], 400)
        self.assertEqual(self.request("GET", "/v1/timeline?workspace_id=shop&types=plan")[0], 400)
        self.assertEqual(self.request("GET", "/v1/thoughts?workspace_id=shop", authorization=None)[0], 401)
        status, headers, _ = self.request("POST", "/v1/thoughts", {})
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")


    def test_timeline_cursor_rejects_malformed_values_and_keeps_legacy_pages(self):
        for index in range(2):
            self.assertEqual(self.request("POST", "/v1/captures", {
                "workspace_id": "shop", "request_id": str(index), "text": TEXT,
                "structured": structured()})[0], 200)
        url = "/v1/timeline?workspace_id=shop&types=thought&page_size=1"
        status, _, first = self.request("GET", url)
        self.assertEqual(status, 200)
        cursor = first["next_before"]
        status, _, second = self.request("GET", url + "&" + urlencode({"before": "|".join(cursor)}))
        self.assertEqual(status, 200)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["items"][0]["record"]["id"], second["items"][0]["record"]["id"])
        for value in ("", "garbage", cursor[0], "|".join(cursor) + "|extra",
                      "2026-02-30|" + "|".join(cursor[1:]), cursor[0] + "|bad|id"):
            with self.subTest(value=value):
                status, _, result = self.request("GET", url + "&" + urlencode({"before": value}))
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], "invalid_cursor")

    def test_invalid_structured_unicode_is_a_client_error_without_writes(self):
        data = structured()
        data["thoughts"][0]["date"] = chr(0xD800)
        payload = {"workspace_id": "shop", "request_id": "unicode", "text": TEXT, "structured": data}
        before = self.db.read_bytes()
        status, _, result = self.request("POST", "/v1/captures",
            raw_body=json.dumps(payload, ensure_ascii=True).encode("ascii"))
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "invalid_structured")
        self.assertEqual(self.db.read_bytes(), before)

    def test_nul_thought_text_rejected_before_projection_writes(self):
        def snapshot():
            with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)) as conn:
                return list(conn.iterdump())

        for field in ("body", "source_quote", "title"):
            data = structured()
            data["thoughts"][0][field] = "\x00新品"
            payload = {"workspace_id": "shop", "request_id": "nul", "text": "\x00新品", "structured": data}
            data["thoughts"][0]["source_quote"] = payload["text"] if field == "source_quote" else "新品"
            before = snapshot()
            with self.subTest(field=field):
                status, _, result = self.request("POST", "/v1/captures", payload)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], "invalid_structured")
                # A rolled-back savepoint may advance SQLite header counters;
                # every schema row, business row and request claim must stay unchanged.
                self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
