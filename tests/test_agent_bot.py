from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from channel.bot import ILinkBotWorker
from channel.ilink import StateStore
from engine.agent_service import AgentError, AgentService, Principal
from engine.store import SqliteStore
from engine.web import make_server
import http.client
import threading


class FakeBotClient:
    def __init__(self, batches=None, send_error=None):
        self.batches = list(batches or [])
        self.sent = []
        self.send_error = send_error

    def get_updates(self, sync_buf=""):
        if not self.batches:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}
        return self.batches.pop(0)

    def send_long_text(self, peer, text, context_token=None):
        if self.send_error:
            raise self.send_error
        self.sent.append((peer, text, context_token))
        return [{"ret": 0}]


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
        self.service = AgentService(self.store)
        self.principal = Principal("agent-1", "owner")
        self.addCleanup(self.store.close)

    def test_capture_is_scoped_and_idempotent(self):
        first = self.service.capture_text(self.principal, "shop", " 今天营业额100 ", "req-1")
        with self.assertRaises(AgentError) as conflict:
            self.service.capture_text(self.principal, "shop", "不同正文", "req-1")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        again = self.service.capture_text(self.principal, "shop", " 今天营业额100 ", "req-1")
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["capture"]["raw_text"], " 今天营业额100 ")
        with self.assertRaises(AgentError) as ctx:
            self.service.capture_text(Principal("other", "nope"), "shop", "x", "r")
        self.assertEqual(ctx.exception.status, 403)

    def test_received_at_rejects_invalid_and_noncanonical_dates(self):
        for value in ("bad-date", "2026-02-30T12:00:00", "20261025", "2026-W43-7"):
            with self.subTest(value=value):
                with self.assertRaises(AgentError) as caught:
                    self.service.capture_text(self.principal, "shop", "今天营业额100",
                                              "bad-time", received_at=value)
                self.assertEqual(caught.exception.code, "invalid_received_at")
        self.assertEqual(self.store.list_entries("shop", "0000", "9999"), [])

    def test_profile_gate_returns_confirmation(self):
        self.store.ensure_shop("project", "项目", owner_id="owner", profile_id="project")
        result = self.service.capture_text(self.principal, "project", "报价30元", "project-1")
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["entries"], [])

    def test_idempotency_lease_fences_old_worker(self):
        fingerprint = "f" * 64
        first = self.store.agent_request_claim("agent", "shop", "capture", "lease-1", fingerprint)
        self.assertTrue(first)
        active = self.store.agent_request_get("agent", "shop", "capture", "lease-1")
        self.assertIsNone(active["response"])
        self.store.conn.execute(
            "UPDATE agent_request SET created_at=? WHERE request_id=?",
            ("2000-01-01T00:00:00", "lease-1"),
        )
        self.store.conn.commit()
        second = self.store.agent_request_claim("agent", "shop", "capture", "lease-1", fingerprint)
        self.assertTrue(second)
        self.assertNotEqual(first, second)
        self.assertFalse(self.store.agent_request_finish(
            "agent", "shop", "capture", "lease-1", first, {"old": True}))
        self.assertTrue(self.store.agent_request_finish(
            "agent", "shop", "capture", "lease-1", second, {"new": True}))

    def test_review_accepts_maximum_external_request_id(self):
        first = self.service.capture_text(self.principal, "shop", "今天营业额100", "review-source")
        entry_id = first["entries"][0]["id"]
        request_id = "r" * 100
        result = self.service.review(
            self.principal, "shop", request_id, f"entry:{entry_id}",
            {"amount": 101}, "修正金额",
        )
        self.assertFalse(result["duplicate"])
        replay = self.service.review(
            self.principal, "shop", request_id, f"entry:{entry_id}",
            {"amount": 101}, "修正金额",
        )
        self.assertTrue(replay["duplicate"])

    def test_import_requires_exact_confirmation_and_preview_binding(self):
        csv = "日期,类型,金额\n2026-10-25,支出,10\n"
        preview = self.service.preview_import(self.principal, "shop", csv)
        for confirmed in (False, None, 0, "false", 1):
            with self.subTest(confirmed=confirmed):
                with self.assertRaises(AgentError) as ctx:
                    self.service.commit_import(
                        self.principal, "shop", csv, f"imp-{confirmed!s}",
                        confirmed=confirmed, preview_token=preview["preview_token"],
                    )
                self.assertEqual(ctx.exception.code, "confirmation_required")
        with self.assertRaises(AgentError) as ctx:
            self.service.commit_import(
                self.principal, "shop", csv + "\n", "imp-mismatch", confirmed=True,
                preview_token=preview["preview_token"],
            )
        self.assertEqual(ctx.exception.code, "preview_mismatch")
        result = self.service.commit_import(
            self.principal, "shop", csv, "imp-1", confirmed=True,
            preview_token=preview["preview_token"],
        )
        self.assertEqual(result["result"]["imported"], 1)
        replay = self.service.commit_import(
            self.principal, "shop", csv, "imp-1", confirmed=True,
            preview_token=preview["preview_token"],
        )
        self.assertTrue(replay["duplicate"])


class BotWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(":memory:")
        self.store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
        self.state = StateStore(str(Path(self.tmp.name) / "state.json"))
        self.principal = Principal("acct-1", "owner")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def msg(message_id="m-1", peer="peer-1", text="今天营业额100", token="ctx-1", **extra):
        return {"message_id": message_id, "sender_id": peer,
                "message_type": 1, "message_state": 2,
                "context_token": token,
                "item_list": [{"type": 1, "text_item": {"text": text}}], **extra}

    def worker(self, client):
        return ILinkBotWorker(client, self.store, self.state,
                              principal=self.principal, workspace_id="shop",
                              allowed_peers={"peer-1": "shop"}, sleep=lambda _: None)

    def test_text_message_is_ingested_once_and_cursor_commits(self):
        client = FakeBotClient([{"ret": 0, "msgs": [self.msg()], "get_updates_buf": "buf-1"}])
        worker = self.worker(client)
        result = worker.run_once()
        self.assertEqual(result["handled"], 1)
        self.assertEqual(worker.state.sync_buf, "buf-1")
        self.assertEqual(worker.state.get_context_token("peer-1"), "ctx-1")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(self.store.list_entries("shop", "0000", "9999")), 1)

        replay = FakeBotClient([{"ret": 0, "msgs": [self.msg()], "get_updates_buf": "buf-2"}])
        worker.client = replay
        worker.run_once()
        self.assertEqual(len(self.store.list_entries("shop", "0000", "9999")), 1)
        self.assertEqual(worker.state.sync_buf, "buf-2")

    def test_unknown_peer_and_nontext_are_discarded_but_cursor_commits(self):
        client = FakeBotClient([{"ret": 0, "msgs": [
            self.msg(peer="stranger"),
            self.msg(message_id="image", peer="peer-1", **{"item_list": [{"type": 2, "image_item": {}}]}),
        ], "get_updates_buf": "buf-x"}])
        worker = self.worker(client)
        result = worker.run_once()
        self.assertEqual(result["handled"], 0)
        self.assertEqual(result["ignored"], 2)
        self.assertEqual(worker.state.sync_buf, "buf-x")
        self.assertEqual(client.sent, [])
        self.assertEqual(self.store.list_entries("shop", "0000", "9999"), [])

    def test_missing_message_type_is_fail_closed(self):
        self.assertIsNone(ILinkBotWorker.normalize_text_message(self.msg(message_type=None)))

    def test_send_failure_does_not_advance_cursor(self):
        client = FakeBotClient([{"ret": 0, "msgs": [self.msg()], "get_updates_buf": "buf-f"}], send_error=OSError("send failed"))
        worker = self.worker(client)
        with self.assertRaises(OSError):
            worker.run_once()
        self.assertEqual(worker.state.sync_buf, "")


class AgentHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "api.db"
        store = SqliteStore(str(self.db))
        store.ensure_shop("shop", "测试店", owner_id="owner", profile_id="restaurant")
        store.close()
        self.server = make_server(0, self.db, api_key="secret", owner_id="owner")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = json.loads(response.read())
        trace = response.getheader("X-Request-ID")
        conn.close()
        return response.status, result, trace

    def test_auth_scope_and_trace(self):
        status, body, trace = self.request("GET", "/v1/workspaces")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertTrue(trace)
        self.assertEqual(body["request_id"], trace)
        self.assertEqual(body["error"]["request_id"], trace)
        status, body, trace = self.request("GET", "/v1/workspaces", token="secret")
        self.assertEqual(status, 200)
        self.assertEqual([w["id"] for w in body["workspaces"]], ["shop"])
        payload = {"workspace_id": "shop", "text": "今天营业额100", "request_id": "http-1"}
        status, body, trace = self.request("POST", "/v1/captures", payload, token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(body["capture"]["raw_text"], "今天营业额100")
        self.assertTrue(trace)

    def test_health_needs_no_auth(self):
        status, body, _ = self.request("GET", "/healthz")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, body, _ = self.request("GET", "/readyz")
        self.assertEqual(status, 200)

    def test_http_import_preview_commit_uses_token(self):
        csv = "日期,类型,金额\n2026-10-25,支出,10\n"
        status, preview, _ = self.request(
            "POST", "/v1/imports/preview",
            {"workspace_id": "shop", "csv": csv}, token="secret",
        )
        self.assertEqual(status, 200)
        status, body, _ = self.request(
            "POST", "/v1/imports/commit",
            {"workspace_id": "shop", "csv": csv, "request_id": "http-import-1",
             "confirmed": True, "preview_token": preview["preview_token"]},
            token="secret",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["imported"], 1)
        status, replay, _ = self.request(
            "POST", "/v1/imports/commit",
            {"workspace_id": "shop", "csv": csv, "request_id": "http-import-1",
             "confirmed": True, "preview_token": preview["preview_token"]}, token="secret",
        )
        self.assertEqual(status, 200)
        self.assertEqual(replay, {**body, "duplicate": True})
        status, error, _ = self.request(
            "POST", "/v1/imports/commit",
            {"workspace_id": "shop", "csv": csv, "request_id": "http-import-2",
             "confirmed": True, "preview_token": preview["preview_token"]}, token="secret",
        )
        self.assertEqual((status, error["error"]["code"]), (409, "preview_consumed"))

    def test_api_only_hides_legacy_reads_writes_and_pages(self):
        self.server.api_only = True
        for path in ("/", "/api/state", "/api/capabilities", "/api/workspaces"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/record", {"text": "今天营业额99"})[0], 404)
        self.assertEqual(self.request("GET", "/v1/workspaces")[0], 401)
        self.assertEqual(self.request("GET", "/v1/workspaces", token="secret")[0], 200)

    def test_auth_precedes_body_and_database_construction(self):
        from unittest.mock import patch
        with patch("engine.web.SqliteStore", side_effect=AssertionError("DB opened")):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            conn.request("POST", "/v1/captures", body=b"not-json",
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            conn.close()
        self.server.db_path = Path(self.tmp.name) / "missing.db"
        status, body, _ = self.request("POST", "/v1/captures",
                                       {"workspace_id": "shop", "text": "今天营业额100",
                                        "request_id": "missing-db"}, token="secret")
        self.assertEqual((status, body["error"]["code"]), (503, "not_ready"))
        self.assertFalse(self.server.db_path.exists())

    def test_incomplete_control_schema_is_not_ready_and_not_repaired_by_request(self):
        store = SqliteStore(str(self.db))
        store.conn.execute("DROP TABLE agent_request")
        store.conn.commit()
        store.close()
        before = self.db.read_bytes()
        for method, path, payload in (
            ("GET", "/readyz", None),
            ("GET", "/v1/workspaces", None),
            ("POST", "/v1/captures", {"workspace_id": "shop", "text": "今天营业额100",
                                       "request_id": "incomplete-schema"}),
        ):
            with self.subTest(method=method, path=path):
                status, body, _ = self.request(method, path, payload, token="secret")
                self.assertEqual((status, body["error"]["code"]), (503, "not_ready"))
                self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.request("GET", "/healthz")[0], 200)
        self.assertEqual(self.request("GET", "/v1/capabilities", token="secret")[0], 200)

    def test_workspace_allowlist_and_foreign_targets_fail_closed(self):
        store = SqliteStore(str(self.db))
        try:
            store.ensure_shop("other", "另一店", owner_id="owner")
            store.ensure_shop("foreign", "其他用户", owner_id="foreign-owner")
            captured = AgentService(store).capture_text(Principal("other", "foreign-owner"),
                                                       "foreign", "今天营业额99", "foreign")
            entry_id = captured["entries"][0]["id"]
        finally:
            store.close()
        self.server.workspace_id = "shop"
        status, body, _ = self.request("GET", "/v1/workspaces", token="secret")
        self.assertEqual([row["id"] for row in body["workspaces"]], ["shop"])
        for workspace in ("other", "foreign", "missing"):
            status, body, _ = self.request("POST", "/v1/captures",
                {"workspace_id": workspace, "text": "今天营业额100", "request_id": "scope"}, token="secret")
            self.assertEqual((status, body["error"]["code"]), (403, "workspace_forbidden"))
        for target in (f"entry:{entry_id}", "entry:absent"):
            status, body, _ = self.request("POST", "/v1/reviews",
                {"workspace_id": "shop", "request_id": "scope-review", "target_id": target,
                 "fields": {"amount": 1}, "reason": "修正"}, token="secret")
            self.assertEqual((status, body["error"]["code"]), (403, "target_forbidden"))

    def test_v1_method_not_allowed_has_allow_header(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("PUT", "/v1/captures")
        response = conn.getresponse()
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "POST")
        response.read()
        conn.close()


if __name__ == "__main__":
    unittest.main()
