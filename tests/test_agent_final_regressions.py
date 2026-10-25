"""Request identity and durability regressions against disposable databases."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from engine.agent_service import AgentError, AgentService, Principal
from engine.importer import commit_import
from engine.observe import ingest
from engine.review import apply_review
from engine.store import SqliteStore


CSV = "日期,类型,金额\n2026-10-25,支出,10\n"
PRINCIPAL = Principal("agent", "owner")


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def capture_payload(text, dedup):
    return {"text": text, "source": "agent", "received_at": None,
            "context": None, "intent": None, "profile_id": None,
            "peer": None, "dedup_ref": dedup}


class FinalRequestRegressions(unittest.TestCase):
    def database(self, path):
        store = SqliteStore(str(path))
        store.ensure_shop("shop", "测试店", owner_id="owner")
        return store

    def test_failed_standalone_claim_does_not_hide_next_service_commit(self):
        for state in ("active", "completed", "different"):
            for operation in ("capture", "review", "import_commit"):
                with self.subTest(state=state, operation=operation), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "ledger.db"
                    with closing(self.database(path)) as store:
                        service = AgentService(store)
                        target = preview = None
                        if operation == "review":
                            captured = service.capture_text(PRINCIPAL, "shop", "今天营业额100", "source")
                            target = "entry:" + captured["entries"][0]["id"]
                        elif operation == "import_commit":
                            preview = service.preview_import(PRINCIPAL, "shop", CSV)
                        token = store.agent_request_claim("agent", "shop", "capture", "busy", "same")
                        if state == "completed":
                            store.agent_request_finish("agent", "shop", "capture", "busy", token, {"done": True})
                        self.assertIsNone(store.agent_request_claim(
                            "agent", "shop", "capture", "busy", "other" if state == "different" else "same"))
                        if operation == "capture":
                            expected = service.capture_text(PRINCIPAL, "shop", "今天营业额200", "durable")
                        elif operation == "review":
                            expected = service.review(PRINCIPAL, "shop", "durable", target,
                                                      {"amount": 101}, "修正金额")
                        else:
                            expected = service.commit_import(PRINCIPAL, "shop", CSV, "durable",
                                confirmed=True, preview_token=preview["preview_token"])
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                            row = reader.execute(
                                "SELECT response FROM agent_request WHERE principal_id=? AND workspace_id=? "
                                "AND operation=? AND request_id=?", ("agent", "shop", operation, "durable")
                            ).fetchone()
                            self.assertIsNotNone(row, "successful response must be visible to another connection")
                            self.assertEqual(json.loads(row[0]), expected)
                        self.assertFalse(store.conn.in_transaction)
                    with closing(SqliteStore(str(path))) as reopened:
                        self.assertEqual(reopened.agent_request_get(
                            "agent", "shop", operation, "durable")["response"], expected)

    def test_failed_nested_claim_does_not_publish_or_rollback_outer_business(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with closing(self.database(path)) as store:
                store.agent_request_claim("agent", "shop", "capture", "busy", "same")
                with self.assertRaisesRegex(RuntimeError, "rollback"):
                    with store.transaction():
                        self.assertIsNone(store.agent_request_claim(
                            "agent", "shop", "capture", "busy", "same"))
                        AgentService(store).capture_text(PRINCIPAL, "shop", "今天营业额200", "outer")
                        self.assertEqual(len(store.list_entries("shop", "0000", "9999")), 1)
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
                            self.assertEqual(reader.execute("SELECT COUNT(*) FROM entry").fetchone()[0], 0)
                        raise RuntimeError("rollback")
                self.assertEqual(store.list_entries("shop", "0000", "9999"), [])
                self.assertFalse(store.conn.in_transaction)

    def test_distinct_principal_request_tuples_have_distinct_business_effects(self):
        with closing(self.database(":memory:")) as store:
            service = AgentService(store)
            first = service.capture_text(Principal("a", "owner"), "shop", "今天营业额100", "x:y")
            second = service.capture_text(Principal("a:x", "owner"), "shop", "今天营业额100", "y")
            self.assertNotEqual(first["capture"]["id"], second["capture"]["id"])
            self.assertEqual(len(store.list_entries("shop", "0000", "9999")), 2)
            for principal, request, capture, amount in (
                (Principal("a", "owner"), "x:y", first, 101),
                (Principal("a:x", "owner"), "y", second, 201),
            ):
                entry = capture["entries"][0]["id"]
                result = service.review(principal, "shop", request, "entry:" + entry,
                                        {"amount": amount}, "修正金额")
                self.assertFalse(result["duplicate"])
                self.assertEqual(store.get_entry(entry)["amount"], amount)
                self.assertEqual(result["record"]["correction_of"], "entry:" + entry)

    def test_review_identity_is_distinct_without_capture_identity_dependency(self):
        with closing(self.database(":memory:")) as store:
            service = AgentService(store)
            targets = [service.capture_text(PRINCIPAL, "shop", text, request)["entries"][0]["id"]
                       for text, request in (("今天营业额100", "source-1"),
                                             ("今天营业额200", "source-2"))]
            corrections = []
            for principal, request, target, amount in (
                (Principal("a", "owner"), "x:y", targets[0], 101),
                (Principal("a:x", "owner"), "y", targets[1], 201),
            ):
                result = service.review(principal, "shop", request, "entry:" + target,
                                        {"amount": amount}, "修正金额")
                self.assertEqual(store.get_entry(target)["amount"], amount)
                self.assertEqual(result["record"]["correction_of"], "entry:" + target)
                corrections.append(result["record"]["id"])
            self.assertNotEqual(*corrections)

    def test_review_cache_replays_after_target_delete_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with closing(self.database(path)) as store:
                service = AgentService(store)
                captured = service.capture_text(PRINCIPAL, "shop", "今天营业额100", "source")
                entry = captured["entries"][0]["id"]
                expected = service.review(PRINCIPAL, "shop", "review", "entry:" + entry,
                                          {"amount": 101}, "修正金额")
                store.delete_entry(entry)
            with closing(SqliteStore(str(path))) as store:
                service = AgentService(store)
                actual = service.review(PRINCIPAL, "shop", "review", "entry:" + entry,
                                        {"amount": 101}, "修正金额")
                self.assertEqual(actual, {**expected, "duplicate": True})
                with self.assertRaises(AgentError) as caught:
                    service.review(Principal("agent", "other-owner"), "shop", "review",
                                   "entry:" + entry, {"amount": 101}, "修正金额")
                self.assertEqual(caught.exception.code, "workspace_forbidden")
                with self.assertRaises(AgentError) as caught:
                    service.review(PRINCIPAL, "shop", "review", "entry:" + entry,
                                   {"amount": 102}, "修正金额")
                self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_released_legacy_claims_require_review_across_reopen(self):
        for operation in ("capture", "review", "import_commit"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ledger.db"
                with closing(self.database(path)) as store:
                    service = AgentService(store)
                    target = preview = None
                    if operation == "capture":
                        dedup = "agent:agent:legacy"
                        payload = capture_payload("今天营业额100", dedup)
                    elif operation == "review":
                        captured = service.capture_text(PRINCIPAL, "shop", "今天营业额100", "source")
                        target = "entry:" + captured["entries"][0]["id"]
                        payload = {"target_id": target, "fields": {"amount": 101}, "reason": "修正金额"}
                    else:
                        preview = service.preview_import(PRINCIPAL, "shop", CSV)
                        payload = {"sha256": preview["sha256"], "filename": "agent.csv",
                                   "confirmed": True, "preview_token": preview["preview_token"]}
                    token = store.agent_request_claim("agent", "shop", operation, "legacy", fingerprint(payload))
                    if operation == "capture":
                        ingest(store, "shop", "今天营业额100", source="agent", dedup_ref=dedup)
                    elif operation == "review":
                        internal = "agent-" + hashlib.sha256(b"agent:legacy").hexdigest()
                        apply_review(store, "shop", internal, target, {"amount": 101}, "修正金额")
                    else:
                        commit_import(store, "shop", preview["preview"], "agent.csv", preview["sha256"])
                    self.assertTrue(store.agent_request_release("agent", "shop", operation, "legacy", token))
                with closing(SqliteStore(str(path))) as store:
                    service = AgentService(store)
                    before = store.export_all("shop")
                    with self.assertRaises(AgentError) as caught:
                        if operation == "capture":
                            service.capture_text(PRINCIPAL, "shop", "今天营业额100", "legacy")
                        elif operation == "review":
                            service.review(PRINCIPAL, "shop", "legacy", target, {"amount": 101}, "修正金额")
                        else:
                            service.commit_import(PRINCIPAL, "shop", CSV, "legacy", confirmed=True,
                                                  preview_token=preview["preview_token"])
                    self.assertEqual(caught.exception.code, "recovery_needs_review")
                    self.assertEqual(store.export_all("shop"), before)

    def test_new_preview_can_confirm_an_intentional_duplicate_file(self):
        with closing(self.database(":memory:")) as store:
            service = AgentService(store)
            old = service.preview_import(PRINCIPAL, "shop", CSV)
            self.assertFalse(old["duplicate_file"])
            self.assertIsNone(old["existing_batch"])
            original = commit_import(store, "shop", old["preview"], "legacy.csv", old["sha256"])
            preview = service.preview_import(PRINCIPAL, "shop", CSV)
            self.assertTrue(preview["duplicate_file"])
            self.assertEqual(preview["existing_batch"], {"id": original["batch"]["id"],
                                                       "filename": "legacy.csv", "rows": 1})
            response = service.commit_import(PRINCIPAL, "shop", CSV, "new-confirmation",
                confirmed=True, preview_token=preview["preview_token"])
            self.assertTrue(response["result"]["duplicate"])
            self.assertEqual(response["result"]["imported"], 0)
            self.assertEqual(len(store.list_entries("shop", "0000", "9999")), 1)
            replay = service.commit_import(PRINCIPAL, "shop", CSV, "new-confirmation",
                confirmed=True, preview_token=preview["preview_token"])
            self.assertEqual(replay, {**response, "duplicate": True})

    def test_capture_replay_survives_workspace_profile_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with closing(self.database(path)) as store:
                expected = AgentService(store).capture_text(PRINCIPAL, "shop", "今天营业额100",
                    "profile-replay", profile_id="restaurant")
                store.workspace_set_profile("shop", "retail", owner_id="owner")
            with closing(SqliteStore(str(path))) as store:
                service = AgentService(store)
                actual = service.capture_text(PRINCIPAL, "shop", "今天营业额100",
                    "profile-replay", profile_id="restaurant")
                self.assertEqual(actual, {**expected, "duplicate": True})
                self.assertEqual(len(store.list_entries("shop", "0000", "9999")), 1)
                before = store.export_all("shop")
                with self.assertRaises(AgentError) as caught:
                    service.capture_text(PRINCIPAL, "shop", "今天营业额200",
                        "new-profile-write", profile_id="restaurant")
                self.assertEqual(caught.exception.code, "profile_mismatch")
                self.assertEqual(store.export_all("shop"), before)
                with self.assertRaises(AgentError) as caught:
                    service.capture_text(PRINCIPAL, "shop", "今天营业额200",
                        "profile-replay", profile_id="restaurant")
                self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_old_completed_review_cache_keeps_full_history_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            with closing(self.database(path)) as store:
                service = AgentService(store)
                captured = service.capture_text(PRINCIPAL, "shop", "今天营业额100", "source")
                target = "entry:" + captured["entries"][0]["id"]
                payload = {"target_id": target, "fields": {"amount": 101}, "reason": "修正金额"}
                token = store.agent_request_claim("agent", "shop", "review", "legacy", fingerprint(payload))
                internal = "agent-" + hashlib.sha256(b"agent:legacy").hexdigest()
                expected = apply_review(store, "shop", internal, target, {"amount": 101}, "修正金额")
                store.agent_request_finish("agent", "shop", "review", "legacy", token, expected)
            with closing(SqliteStore(str(path))) as store:
                actual = AgentService(store).review(PRINCIPAL, "shop", "legacy", target,
                                                     {"amount": 101}, "修正金额")
                self.assertEqual(actual, {**expected, "duplicate": True})
                self.assertEqual(actual["changed"], {"amount": 101})
                self.assertTrue(actual["history"])

    def test_old_completed_capture_cache_keeps_original_identity_and_response(self):
        with closing(self.database(":memory:")) as store:
            dedup = "agent:agent:legacy"
            receipt = ingest(store, "shop", "今天营业额100", source="agent", dedup_ref=dedup)
            expected = {"capture": receipt["record"], "parsed": receipt["parsed"],
                        "duplicate": False, "legacy_complete": True}
            token = store.agent_request_claim("agent", "shop", "capture", "legacy",
                                               fingerprint(capture_payload("今天营业额100", dedup)))
            store.agent_request_finish("agent", "shop", "capture", "legacy", token, expected)
            actual = AgentService(store).capture_text(PRINCIPAL, "shop", "今天营业额100", "legacy")
            self.assertEqual(actual, {**expected, "duplicate": True})
            self.assertEqual(len(store.list_entries("shop", "0000", "9999")), 1)


if __name__ == "__main__":
    unittest.main()
