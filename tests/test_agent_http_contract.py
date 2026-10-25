"""HTTP contract regressions using real handlers and temporary databases only."""
from __future__ import annotations

import http.client
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.store import SqliteStore
from engine.web import DemoHandler


class InMemoryConnection:
    """Socket-shaped byte streams for BaseHTTPRequestHandler and HTTPResponse."""

    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class AgentHttpContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "contract.db"
        store = SqliteStore(str(self.db))
        try:
            store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
        finally:
            store.close()
        self.server = SimpleNamespace(
            db_path=self.db, api_key="fake-contract-token", owner_id="owner",
            principal_id="contract-agent", workspace_id="", api_only=False,
            server_port=8765,
        )

    def request(self, method, path, payload=None, *, authorization="Bearer fake-contract-token", raw_body=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        if raw_body is not None:
            body = raw_body
        headers = ["Host: 127.0.0.1:8765", "Content-Type: application/json",
                   f"Content-Length: {len(body)}"]
        if authorization is not None:
            headers.append(f"Authorization: {authorization}")
        raw = (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + body
        connection = InMemoryConnection(raw)
        DemoHandler(connection, ("127.0.0.1", 12345), self.server)
        response = http.client.HTTPResponse(InMemoryConnection(connection.output.getvalue()), method=method)
        response.begin()
        status, headers, raw = response.status, response.headers, response.read()
        response.close()
        result = json.loads(raw) if raw and headers.get_content_type() == "application/json" else raw
        return status, headers, result

    def test_review_response_exposes_duplicate_and_preserves_review_wrapper(self):
        status, _, captured = self.request("POST", "/v1/captures", {
            "workspace_id": "shop", "text": "今天营业额100", "request_id": "source",
        })
        self.assertEqual(status, 200)
        target_id = "entry:" + captured["entries"][0]["id"]
        payload = {"workspace_id": "shop", "request_id": "correction", "target_id": target_id,
                   "fields": {"amount": 101}, "reason": "修正金额"}
        status, _, first = self.request("POST", "/v1/reviews", payload)
        self.assertEqual(status, 200)
        self.assertIs(first.get("duplicate"), False)
        self.assertIs(first["review"]["duplicate"], False)
        self.assertEqual(first["review"]["target_id"], target_id)
        self.assertEqual(first["review"]["changed"], {"amount": 101})
        status, _, replay = self.request("POST", "/v1/reviews", payload)
        self.assertEqual(status, 200)
        self.assertIs(replay["duplicate"], True)
        self.assertEqual(replay["review"], {**first["review"], "duplicate": True})
        status, _, entries = self.request("GET", "/v1/entries?workspace_id=shop")
        self.assertEqual(status, 200)
        self.assertEqual([(row["id"], row["amount"]) for row in entries["entries"]],
                         [(captured["entries"][0]["id"], 101)])

    def test_colloquial_revenue_capture_keeps_narrative_and_replays_once(self):
        text = ("今天是周末，但是天气不好，阴天，天气也越来越冷了。"
                "外面还有跑马拉松的，可能影响生意。今天的营业额很差，只有1300。")
        payload = {"workspace_id": "shop", "text": text, "request_id": "colloquial-revenue",
                   "received_at": "2026-10-25T20:00:00+08:00"}
        status, _, first = self.request("POST", "/v1/captures", payload)
        self.assertEqual(status, 200)
        self.assertEqual(first["capture"]["raw_text"], text)
        # Narrative is still partial, even though the explicit revenue is booked.
        self.assertEqual(first["status"], "partial")
        self.assertEqual([(e["date"], e["type"], e["category"], e["amount"], e["granularity"])
                          for e in first["entries"]],
                         [("2026-10-25", "revenue", "营业额", 1300, "daily")])
        self.assertEqual(first["sales"], [])
        weather = [o for o in first["observations"] if o["kind"] == "天气"]
        self.assertEqual([(o["occurred_at"], o["value_text"]) for o in weather],
                         [("2026-10-25", "阴")])
        self.assertTrue(first["parsed"]["leftovers"])
        self.assertFalse(any("1300" in value for value in first["parsed"]["leftovers"]))
        status, _, replay = self.request("POST", "/v1/captures", payload)
        self.assertEqual(status, 200)
        self.assertEqual(replay, {**first, "duplicate": True})
        # A second handler opens a fresh Store; both the entry and its receipt are durable.
        status, _, summary = self.request("GET", "/v1/summary?workspace_id=shop&month=2026-10")
        self.assertEqual(status, 200)
        self.assertEqual(summary["summary"]["revenue"], 1300)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*), SUM(amount) FROM entry").fetchone(),
                             (1, 1300))

    def test_capabilities_explain_capture_result_before_agent_write(self):
        status, _, contract = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(contract["contract_version"], "agent-1")
        preparation = contract["agent_policy"]["input_preparation"]
        self.assertTrue(preparation["user_speech_may_be_messy"])
        self.assertEqual(preparation["responsible_party"], "calling_agent")
        self.assertEqual(preparation["preferred_payload_field"], "structured")
        self.assertTrue(preparation["preserve_verbatim_text"])
        self.assertTrue(preparation["require_source_quote"])
        schema = contract["agent_policy"]["input_preparation"]["structured_schema"]
        self.assertEqual(set(schema["required"]), {"entries", "sales", "observations", "leftovers"})
        entry = schema["properties"]["entries"]["items"]
        self.assertIn("amount_basis", entry["required"])
        self.assertIn("granularity", entry["required"])
        self.assertEqual(entry["properties"]["amount_basis"]["enum"], ["gross", "settlement"])
        self.assertIn("product_name", schema["properties"]["sales"]["items"]["required"])
        example = preparation["request_example"]
        from engine.agent_normalization import validate_structured
        self.assertEqual(validate_structured(example["text"], example["structured"]), example["structured"])
        presentation = contract["agent_policy"]["user_presentation"]
        self.assertFalse(presentation["show_internal_field_names"])
        self.assertFalse(presentation["show_server_paths_as_downloads"])
        self.assertTrue(presentation["state_scope_before_read"])
        self.assertEqual(presentation["report_policy"]["unavailable_value_text"], "暂无法计算")
        policy = contract["agent_policy"]["capture_result_policy"]
        self.assertFalse(policy["http_200_means_financial_write"])
        self.assertEqual(policy["report_financial_rows_from"], ["entries", "sales"])
        self.assertEqual(policy["report_unparsed_fragments_from"], "parsed.leftovers")
        self.assertTrue(policy["partial_may_include_financial_rows"])
        self.assertTrue(policy["parser_miss_is_not_user_ambiguity"])
        self.assertTrue(policy["do_not_rewrite_and_resubmit_to_force_parsing"])

    def test_structured_agent_capture_uses_clean_rows_and_preserves_original(self):
        text = "今天的营业额很差，只有1300。阴天，马拉松可能影响生意。"
        structured = {
            "entries": [{"date": "2026-10-25", "type": "revenue", "category": "营业额",
                         "amount": 1300, "amount_basis": "gross", "granularity": "daily",
                         "source_quote": "今天的营业额很差，只有1300", "assertion": "actual"}],
            "sales": [],
            "observations": [{"date": "2026-10-25", "kind": "天气", "measure": "天气",
                              "value_text": "阴", "source_quote": "阴天", "assertion": "actual"}],
            "leftovers": ["马拉松可能影响生意"],
        }
        payload = {"workspace_id": "shop", "text": text, "request_id": "agent-cleaned",
                   "received_at": "2026-10-25T20:00:00+08:00", "structured": structured}
        with patch("engine.agent_service.ingest", side_effect=AssertionError("must not reparse")):
            status, _, result = self.request("POST", "/v1/captures", payload)
        self.assertEqual(status, 200)
        self.assertEqual(result["capture"]["raw_text"], text)
        self.assertEqual(result["capture"]["parser_version"], "agent-structured-1")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([(e["type"], e["amount"]) for e in result["entries"]], [("revenue", 1300)])
        self.assertEqual(result["sales"], [])
        self.assertEqual(len([o for o in result["observations"] if o["kind"] == "天气"]), 1)
        self.assertEqual(result["parsed"]["leftovers"], ["马拉松可能影响生意"])
        self.assertEqual(result["normalization"], structured)
        status, _, replay = self.request("POST", "/v1/captures", payload)
        self.assertEqual(status, 200)
        self.assertEqual(replay, {**result, "duplicate": True})
        changed = {**structured, "leftovers": []}
        status, _, conflict = self.request("POST", "/v1/captures", {**payload, "structured": changed})
        self.assertEqual((status, conflict["error"]["code"]), (409, "idempotency_conflict"))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM record").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*), SUM(amount) FROM entry").fetchone(), (1, 1300))

    def test_invalid_structured_payload_never_falls_back_to_raw_parser(self):
        for index, structured in enumerate((None, [], "clean text", False, {"entries": []})):
            with self.subTest(structured=structured):
                status, _, body = self.request("POST", "/v1/captures", {
                    "workspace_id": "shop", "text": "今天营业额9999", "request_id": f"bad-clean-{index}",
                    "structured": structured,
                })
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_structured")
        with sqlite3.connect(self.db) as conn:
            for table in ("record", "entry", "sale", "observation", "agent_request"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_padded_structured_product_is_a_validation_error_without_writes(self):
        status, _, body = self.request("POST", "/v1/captures", {
            "workspace_id": "shop", "text": "牛肉面卖了2份30元", "request_id": "padded-product",
            "structured": {"entries": [], "sales": [{
                "date": "2026-10-25", "product_name": " 牛肉面 ", "qty": 2, "amount": 30,
                "unit": "份", "amount_basis": "gross", "granularity": "transaction",
                "source_quote": "牛肉面卖了2份30元", "assertion": "actual",
            }], "observations": [], "leftovers": []},
        })
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_structured"))
        with sqlite3.connect(self.db) as conn:
            for table in ("record", "entry", "sale", "product", "observation", "agent_request"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_auth_precedes_structured_validation_and_database_access(self):
        self.server.db_path = self.db.parent / "missing.db"
        status, headers, body = self.request("POST", "/v1/captures", {
            "workspace_id": "shop", "text": "今天营业额1300", "request_id": "unauth-cleaned",
            "structured": None,
        }, authorization=None)
        self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
        self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
        self.assertFalse(self.server.db_path.exists())

    def test_weather_only_200_has_no_financial_entry(self):
        status, _, result = self.request("POST", "/v1/captures", {
            "workspace_id": "shop", "text": "今天阴天，生意有点差", "request_id": "weather-only",
            "received_at": "2026-10-25T20:00:00+08:00",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["sales"], [])
        self.assertEqual([o["kind"] for o in result["observations"]], ["天气"])
        self.assertTrue(result["parsed"]["leftovers"])

    def test_known_v1_routes_reject_unsupported_methods_with_exact_allow(self):
        routes = (
            ("/v1/capabilities", {"GET"}, "GET"),
            ("/v1/workspaces", {"GET"}, "GET"),
            ("/v1/captures", {"POST"}, "POST"),
            ("/v1/captures/example", {"GET"}, "GET"),
            ("/v1/timeline", {"GET"}, "GET"),
            ("/v1/summary", {"GET"}, "GET"),
            ("/v1/entries", {"GET"}, "GET"),
            ("/v1/sales", {"GET"}, "GET"),
            ("/v1/observations", {"GET"}, "GET"),
            ("/v1/reviews", {"GET", "POST"}, "GET, POST"),
            ("/v1/imports/preview", {"POST"}, "POST"),
            ("/v1/imports/commit", {"POST"}, "POST"),
        )
        # A missing database must not mask a method error or be created by it.
        self.server.db_path = self.db.parent / "missing.db"
        for path, supported, allow in routes:
            for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT", "PROPFIND"):
                if method in supported:
                    continue
                with self.subTest(path=path, method=method):
                    status, headers, body = self.request(method, path + "?workspace_id=shop", {})
                    self.assertEqual(status, 405)
                    self.assertEqual(headers.get("Allow"), allow)
                    self.assertEqual(headers.get_content_type(), "application/json")
                    trace = headers.get("X-Request-ID")
                    self.assertTrue(trace)
                    if method == "HEAD":
                        self.assertEqual(body, b"")
                    else:
                        self.assertEqual(body["error"]["code"], "method_not_allowed")
                        self.assertEqual(body["error"]["details"], {})
                        self.assertEqual(body["request_id"], trace)
                        self.assertEqual(body["error"]["request_id"], trace)
                    self.assertFalse(self.server.db_path.exists())

    def test_bearer_scheme_is_case_insensitive_and_token_is_exact(self):
        for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
            with self.subTest(scheme=scheme):
                status, _, body = self.request("GET", "/v1/workspaces",
                                               authorization=f"{scheme} fake-contract-token")
                self.assertEqual(status, 200)
                self.assertEqual([row["id"] for row in body["workspaces"]], ["shop"])
        for credential in (None, "Basic fake-contract-token", "Bearer", "Bearer ",
                           "Bearer FAKE-CONTRACT-TOKEN", "Bearer fake-contract-token ",
                           "Bearer fake-contract-token-extra"):
            with self.subTest(credential=credential):
                status, headers, body = self.request("GET", "/v1/workspaces",
                                                     authorization=credential)
                self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
                self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")

    def test_non_ascii_credentials_are_unauthorized_before_body_or_database(self):
        self.server.db_path = self.db.parent / "missing.db"
        for method, path in (("GET", "/v1/workspaces"), ("POST", "/v1/captures")):
            for credential in ("Bearer café", "bearer fake-contract-tokenÿ", "Béarer fake-contract-token"):
                with self.subTest(method=method, credential=credential):
                    status, headers, body = self.request(method, path, authorization=credential,
                                                         raw_body=b"not-json")
                    self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
                    self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
                    self.assertEqual(body["request_id"], headers.get("X-Request-ID"))
                    self.assertFalse(self.server.db_path.exists())

    def test_malformed_agent_request_columns_are_not_ready_or_repaired(self):
        status, _, ready = self.request("GET", "/readyz", authorization=None)
        self.assertEqual((status, ready["status"]), (200, "ready"))
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("ALTER TABLE agent_request RENAME COLUMN response TO malformed_response")
            conn.commit()
        finally:
            conn.close()
        before = self.db.read_bytes()
        for method, path, payload in (
            ("GET", "/readyz", None),
            ("GET", "/v1/workspaces", None),
            ("POST", "/v1/captures", {"workspace_id": "shop", "text": "今天营业额100",
                                       "request_id": "malformed-schema"}),
        ):
            with self.subTest(method=method, path=path):
                status, headers, body = self.request(method, path, payload)
                self.assertEqual((status, body["error"]["code"]), (503, "not_ready"))
                self.assertEqual(body["error"]["details"], {"reason": "database_not_ready"})
                self.assertEqual(body["request_id"], headers.get("X-Request-ID"))
                self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.request("GET", "/healthz", authorization=None)[0], 200)
        self.assertEqual(self.request("GET", "/v1/capabilities")[0], 200)
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_business_columns_are_not_ready_or_repaired(self):
        for table, column in (("product", "category"), ("sale", "unit"),
                              ("entry", "external_id"), ("entry", "date")):
            with self.subTest(table=table, column=column):
                with sqlite3.connect(self.db) as conn:
                    conn.execute(f"ALTER TABLE {table} RENAME COLUMN {column} TO broken_column")
                before = self.db.read_bytes()
                try:
                    for method, path, payload in (
                        ("GET", "/readyz", None),
                        ("GET", "/v1/workspaces", None),
                        ("GET", "/v1/entries?workspace_id=shop", None),
                        ("POST", "/v1/captures", {"workspace_id": "shop", "text": "今天营业额100",
                                                   "request_id": "missing-business-column"}),
                    ):
                        status, _, body = self.request(method, path, payload)
                        self.assertEqual(status, 503)
                        self.assertEqual(body["error"]["code"], "not_ready")
                        self.assertEqual(self.db.read_bytes(), before)
                finally:
                    with sqlite3.connect(self.db) as conn:
                        conn.execute(f"ALTER TABLE {table} RENAME COLUMN broken_column TO {column}")

    def test_auth_precedes_method_body_and_database_for_get_and_post(self):
        self.server.db_path = self.db.parent / "missing.db"
        for method, path in (("POST", "/v1/captures"), ("GET", "/v1/workspaces"),
                             ("POST", "/v1/capabilities"), ("GET", "/v1/captures")):
            with self.subTest(method=method, path=path):
                status, headers, body = self.request(method, path, authorization=None,
                                                     raw_body=b"not-json")
                self.assertEqual((status, body["error"]["code"]), (401, "unauthorized"))
                self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
                self.assertFalse(self.server.db_path.exists())

    def test_legacy_capabilities_still_work_without_bearer_auth(self):
        status, headers, body = self.request("GET", "/api/capabilities", authorization=None)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertIsInstance(body, dict)
        self.assertNotIn("error", body)
        self.server.api_only = True
        self.assertEqual(self.request("GET", "/api/capabilities", authorization=None)[0], 404)


if __name__ == "__main__":
    unittest.main()
