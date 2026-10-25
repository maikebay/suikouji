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
