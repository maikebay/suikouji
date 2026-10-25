"""Pre-atomic orphan requests must never silently cache a reduced duplicate."""
from __future__ import annotations

import hashlib
import json
import unittest

from engine.agent_service import AgentError, AgentService, Principal
from engine.importer import commit_import
from engine.observe import ingest
from engine.review import apply_review
from engine.store import SqliteStore


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_shop("shop", "测试店", owner_id="owner")
        self.service = AgentService(self.store)
        self.principal = Principal("agent", "owner")

    def claim(self, operation, payload):
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                                sort_keys=True, default=str).encode()).hexdigest()
        self.store.agent_request_claim("agent", "shop", operation, "legacy", fingerprint)
        self.store.conn.execute("UPDATE agent_request SET created_at=? WHERE request_id='legacy'",
                                ("2000-01-01T00:00:00",))
        self.store.conn.commit()

    def assert_needs_review(self, operation, invoke):
        before = self.store.export_all("shop")
        with self.assertRaises(AgentError) as caught:
            invoke()
        self.assertEqual(caught.exception.code, "recovery_needs_review")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.export_all("shop"), before)
        self.assertIsNone(self.store.agent_request_get("agent", "shop", operation, "legacy")["response"])

    def test_old_capture_commit_without_response_requires_review(self):
        text, dedup = "今天营业额100", "agent:agent:legacy"
        self.claim("capture", {"text": text, "source": "agent", "received_at": None,
                               "context": None, "intent": None, "profile_id": None,
                               "peer": None, "dedup_ref": dedup})
        ingest(self.store, "shop", text, source="agent", dedup_ref=dedup)
        self.assert_needs_review("capture", lambda: self.service.capture_text(
            self.principal, "shop", text, "legacy"))

    def test_old_review_commit_without_response_requires_review(self):
        receipt = self.service.capture_text(self.principal, "shop", "今天营业额100", "source")
        target = "entry:" + receipt["entries"][0]["id"]
        fields, reason = {"amount": 101}, "修正金额"
        self.claim("review", {"target_id": target, "fields": fields, "reason": reason})
        internal_id = "agent-" + hashlib.sha256(b"agent:legacy").hexdigest()
        apply_review(self.store, "shop", internal_id, target, fields, reason)
        self.assert_needs_review("review", lambda: self.service.review(
            self.principal, "shop", "legacy", target, fields, reason))

    def test_old_import_commit_without_response_requires_review(self):
        csv = "日期,类型,金额\n2026-10-25,支出,10\n"
        preview = self.service.preview_import(self.principal, "shop", csv)
        self.claim("import_commit", {"sha256": preview["sha256"], "filename": "agent.csv",
                                     "confirmed": True, "preview_token": preview["preview_token"]})
        commit_import(self.store, "shop", preview["preview"], "agent.csv", preview["sha256"])
        self.assert_needs_review("import_commit", lambda: self.service.commit_import(
            self.principal, "shop", csv, "legacy", confirmed=True,
            preview_token=preview["preview_token"]))

    def test_old_claim_without_business_effects_can_be_retried(self):
        text, dedup = "今天营业额100", "agent:agent:legacy"
        self.claim("capture", {"text": text, "source": "agent", "received_at": None,
                               "context": None, "intent": None, "profile_id": None,
                               "peer": None, "dedup_ref": dedup})
        receipt = self.service.capture_text(self.principal, "shop", text, "legacy")
        self.assertFalse(receipt["duplicate"])
        self.assertIsNotNone(receipt["parsed"])
        self.assertEqual(receipt["entries"][0]["amount"], 100)


if __name__ == "__main__":
    unittest.main()
