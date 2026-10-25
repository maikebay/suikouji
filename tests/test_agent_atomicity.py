"""Crash real subprocesses against disposable databases at transaction boundaries."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engine.agent_service import AgentError, AgentService, Principal
from engine.store import SqliteStore


CSV = "日期,类型,金额\n2026-10-25,支出,10\n"
PRINCIPAL = Principal("agent", "owner")
ROOT = Path(__file__).resolve().parents[1]

# os._exit deliberately bypasses exception handling, finally, and connection close.
CRASH_WORKER = r'''
import json
import os
import sys
from engine.agent_service import AgentService, Principal
from engine.store import SqliteStore

path, operation, stage, target, token, output = sys.argv[1:]
store = SqliteStore(path)
service = AgentService(store)
principal = Principal("agent", "owner")
if stage == "after_claim":
    original_claim = store.agent_request_claim
    def claim(*args, **kwargs):
        original_claim(*args, **kwargs)
        os._exit(71)
    store.agent_request_claim = claim
elif stage in ("before_finish", "after_finish"):
    original_finish = service._finish
    def finish(*args, **kwargs):
        if stage == "after_finish":
            original_finish(*args, **kwargs)
        os._exit(71)
    service._finish = finish
if operation in ("capture", "capture_rejected", "capture_profile"):
    text = "营业额" + "9" * 400 if operation == "capture_rejected" else "今天营业额100"
    result = service.capture_text(principal, "shop", text, "crash-request",
                                  received_at="2026-10-25T12:00:00")
elif operation == "review":
    result = service.review(principal, "shop", "crash-request", target,
                            {"amount": 101}, "修正金额")
else:
    result = service.commit_import(principal, "shop", "日期,类型,金额\n2026-10-25,支出,10\n",
                                   "crash-request", confirmed=True, preview_token=token)
with open(output, "w", encoding="utf-8") as stream:
    json.dump(result, stream, ensure_ascii=False)
os._exit(71)
'''


class AgentAtomicityTests(unittest.TestCase):
    def prepare(self, path, operation):
        store = SqliteStore(str(path))
        store.ensure_shop("shop", "测试店", owner_id="owner",
                          profile_id="general" if operation == "capture_profile" else "restaurant")
        service = AgentService(store)
        target = token = ""
        if operation == "review":
            receipt = service.capture_text(PRINCIPAL, "shop", "今天营业额100", "source")
            target = "entry:" + receipt["entries"][0]["id"]
        elif operation == "import_commit":
            token = service.preview_import(PRINCIPAL, "shop", CSV)["preview_token"]
        snapshot = store.export_all("shop")
        store.close()
        return target, token, snapshot

    def invoke(self, service, operation, target, token):
        if operation in ("capture", "capture_rejected", "capture_profile"):
            text = "营业额" + "9" * 400 if operation == "capture_rejected" else "今天营业额100"
            return service.capture_text(PRINCIPAL, "shop", text, "crash-request",
                                        received_at="2026-10-25T12:00:00")
        if operation == "review":
            return service.review(PRINCIPAL, "shop", "crash-request", target,
                                  {"amount": 101}, "修正金额")
        return service.commit_import(PRINCIPAL, "shop", CSV, "crash-request",
                                     confirmed=True, preview_token=token)

    def assert_complete(self, result, operation):
        self.assertFalse(result.get("duplicate", False))
        if operation == "capture":
            self.assertIsNotNone(result["parsed"])
            self.assertEqual(result["entries"][0]["amount"], 100)
        elif operation in ("capture_rejected", "capture_profile"):
            self.assertEqual(result["status"], "rejected" if operation == "capture_rejected" else "needs_confirmation")
            self.assertEqual(result["entries"], [])
            self.assertEqual(result["sales"], [])
        elif operation == "review":
            self.assertEqual(result["changed"], {"amount": 101})
            self.assertTrue(result["history"])
        else:
            self.assertEqual(result["result"]["imported"], 1)
            self.assertFalse(result["result"]["duplicate"])

    def test_process_exit_rolls_back_claim_business_and_response(self):
        for operation in ("capture", "capture_rejected", "capture_profile", "review", "import_commit"):
            for stage in ("after_claim", "before_finish", "after_finish", "after_commit"):
                with self.subTest(operation=operation, stage=stage), tempfile.TemporaryDirectory() as tmp:
                    db, output = Path(tmp) / "crash.db", Path(tmp) / "response.json"
                    target, token, before = self.prepare(db, operation)
                    child = subprocess.run(
                        [sys.executable, "-c", CRASH_WORKER, str(db), operation, stage,
                         target, token, str(output)],
                        cwd=ROOT, capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(child.returncode, 71, child.stderr)
                    store = SqliteStore(str(db))
                    try:
                        service = AgentService(store)
                        if stage != "after_commit":
                            self.assertEqual(store.export_all("shop"), before)
                            self.assertIsNone(store.agent_request_get(
                                "agent", "shop", "capture" if operation.startswith("capture") else operation,
                                "crash-request"))
                            first = self.invoke(service, operation, target, token)
                            self.assert_complete(first, operation)
                        else:
                            first = json.loads(output.read_text(encoding="utf-8"))
                            self.assert_complete(first, operation)
                        after = store.export_all("shop")
                        store.close()
                        store = SqliteStore(str(db))
                        replay = self.invoke(AgentService(store), operation, target, token)
                        self.assertEqual(replay, {**first, "duplicate": True})
                        self.assertEqual(store.export_all("shop"), after)
                    finally:
                        store.close()

    def test_failed_fenced_finish_rolls_back_business(self):
        for operation in ("capture", "capture_rejected", "capture_profile", "review", "import_commit"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "lease.db"
                target, token, before = self.prepare(db, operation)
                store = SqliteStore(str(db))
                try:
                    service = AgentService(store)
                    with patch.object(store, "agent_request_finish", return_value=False):
                        with self.assertRaises((RuntimeError, AgentError)):
                            self.invoke(service, operation, target, token)
                    self.assertEqual(store.export_all("shop"), before)
                    self.assert_complete(self.invoke(service, operation, target, token), operation)
                finally:
                    store.close()

    def test_rejected_capture_is_terminal_and_replayable(self):
        store = SqliteStore(":memory:")
        self.addCleanup(store.close)
        store.ensure_shop("shop", "测试店", owner_id="owner")
        service = AgentService(store)
        first = service.capture_text(PRINCIPAL, "shop", "营业额" + "9" * 400, "invalid")
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(first["capture"]["parse_status"], "rejected")
        self.assertEqual(first["entries"], [])
        self.assertEqual(service.capture_text(
            PRINCIPAL, "shop", "营业额" + "9" * 400, "invalid"), {**first, "duplicate": True})

    def test_busy_commit_leaves_no_uncommitted_rows_for_next_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "busy.db"
            self.prepare(db, "capture")
            reader = sqlite3.connect(str(db))
            writer = SqliteStore(str(db))
            try:
                writer.conn.execute("PRAGMA busy_timeout=1")
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM shop").fetchall()
                with self.assertRaises(sqlite3.OperationalError):
                    self.invoke(AgentService(writer), "capture", "", "")
                self.assertEqual(writer._transaction_depth, 0)
                self.assertFalse(writer.conn.in_transaction)
                reader.rollback()
                first = self.invoke(AgentService(writer), "capture", "", "")
                self.assert_complete(first, "capture")
            finally:
                reader.close()
                writer.close()

    def test_parsing_capture_is_not_cached_as_complete(self):
        store = SqliteStore(":memory:")
        self.addCleanup(store.close)
        store.ensure_shop("shop", "测试店", owner_id="owner")
        store.record_add("shop", "今天营业额100", source="agent", dedup_ref="busy",
                         parse_status="parsing")
        before = store.export_all("shop")
        with self.assertRaises(AgentError) as caught:
            AgentService(store).capture_text(PRINCIPAL, "shop", "今天营业额100", "busy-request",
                                             dedup_ref="busy")
        self.assertEqual(caught.exception.code, "request_in_progress")
        self.assertEqual(store.export_all("shop"), before)


if __name__ == "__main__":
    unittest.main()
