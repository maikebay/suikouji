"""Synthetic message delivery, durable disposition, and cursor regressions."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from channel.bot import ILinkBotWorker
from channel.ilink import StateStore
from engine.agent_service import Principal
from engine.store import SqliteStore
from tests.test_agent_bot import FakeBotClient


def message(message_id="m-1", peer="peer-1", text="今天营业额100", **extra):
    return {"message_id": message_id, "sender_id": peer,
            "message_type": 1, "message_state": 2, "context_token": "context",
            "item_list": [{"type": 1, "text_item": {"text": text}}], **extra}


class BotDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SqliteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_shop("shop", "测试店", owner_id="owner")
        self.state_path = str(Path(self.tmp.name) / "state.json")
        self.state = StateStore(self.state_path)

    def worker(self, client, state=None, account="account-a"):
        return ILinkBotWorker(client, self.store, state or self.state,
                              principal=Principal("bot-principal", "owner"),
                              account_id=account, workspace_id="shop",
                              allowed_peers={"peer-1": "shop", "peer-2": "shop"},
                              sleep=lambda _: None)

    def entries(self):
        return self.store.list_entries("shop", "0000", "9999")

    def test_transport_timeout_never_updates_successful_poll_heartbeat(self):
        from tests.test_ilink import FakeClient
        worker = self.worker(FakeClient([TimeoutError("poll timeout")]))
        with self.assertRaises(TimeoutError):
            worker.run_once()
        self.assertIsNone(worker.last_success_at)
        self.assertFalse(worker.ready())
        self.assertEqual(self.state.sync_buf, "")

    def test_context_token_expiry_allows_one_fallback_but_account_expiry_stops(self):
        from tests.test_ilink import FakeClient
        for fallback, expect_stopped in (({"ret": 0}, False), ({"errcode": -14}, True)):
            with self.subTest(expect_stopped=expect_stopped):
                client = FakeClient([{"msgs": [message()], "get_updates_buf": "cursor"},
                                     {"errcode": -14}, fallback])
                state = StateStore(str(Path(self.tmp.name) / f"fallback-{expect_stopped}.json"))
                worker = self.worker(client, state=state)
                if expect_stopped:
                    worker.run_forever()
                    self.assertTrue(worker.stop_event.is_set())
                    self.assertFalse(worker.ready())
                    self.assertEqual(state.sync_buf, "")
                else:
                    worker.run_once()
                    self.assertTrue(worker.ready())
                    self.assertEqual(state.sync_buf, "cursor")
                self.assertEqual(len(client.calls), 3)
                self.assertNotIn("context_token", client.calls[-1]["payload"]["msg"])

    def test_decoder_rejects_unknown_and_ambiguous_shapes(self):
        invalid = [None, [], "text", {}, message(message_type=True), message(message_state=True),
                   message(message_id=1), message(message_id=""), message(message_id="x" * 101),
                   message(sender_id=None), message(context_token=None),
                   message(item_list=[]), message(item_list=[{}, {}]),
                   message(item_list=[{"type": 2, "image_item": {}}]),
                   message(item_list=[{"type": True, "text_item": {"text": "100元"}}]),
                   message(item_list=[{"type": 1, "text_item": {"text": 100}}]),
                   message(item_list=[{"type": 1, "text_item": {"text": " ", "x": 1}}]),
                   message(item_list=[{"type": 1, "text_item": {"text": "x" * 2001}}]),
                   message(group_id="group"), message(from_user_id="peer-1")]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(ILinkBotWorker.normalize_text_message(payload))
        self.assertIsNotNone(ILinkBotWorker.normalize_text_message(message()))

    def test_identity_distinguishes_message_peer_and_account(self):
        payloads = [message("m-1"), message("m-2"), message("m-1", peer="peer-2")]
        client = FakeBotClient([{"msgs": payloads, "get_updates_buf": "cursor-1"}])
        first = self.worker(client)
        first.run_once()
        self.assertEqual(len(self.entries()), 3)
        other_state = StateStore(str(Path(self.tmp.name) / "other-account.json"))
        self.worker(FakeBotClient([{"msgs": [message()], "get_updates_buf": "cursor-2"}]),
                    state=other_state, account="account-b").run_once()
        self.assertEqual(len(self.entries()), 4)
        self.assertEqual(len(self.store.export_all("shop")["tables"]["record"]), 4)

    def test_send_failure_replay_writes_once_then_acknowledges(self):
        batch = {"msgs": [message()], "get_updates_buf": "cursor-1"}
        failed = self.worker(FakeBotClient([copy.deepcopy(batch)], send_error=OSError("send failed")))
        with self.assertRaises(OSError):
            failed.run_once()
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.state.sync_buf, "")
        self.assertEqual(self.state.dispositions, {})
        restarted_state = StateStore(self.state_path)
        replay_client = FakeBotClient([copy.deepcopy(batch)])
        self.worker(replay_client, state=restarted_state).run_once()
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(restarted_state.sync_buf, "cursor-1")
        self.assertIn("没有重复入账", replay_client.sent[0][1])

    def test_cursor_failure_replay_keeps_completed_dispositions(self):
        batch = {"msgs": [message()], "get_updates_buf": "cursor-1"}
        client = FakeBotClient([copy.deepcopy(batch)])
        worker = self.worker(client)
        original_flush = self.state.flush
        def flush(**kwargs):
            if "sync_buf" in kwargs:
                raise OSError("cursor flush failed")
            return original_flush(**kwargs)
        with patch.object(self.state, "flush", side_effect=flush):
            with self.assertRaises(OSError):
                worker.run_once()
        self.assertEqual(self.state.sync_buf, "")
        reopened = StateStore(self.state_path)
        self.assertTrue(reopened.dispositions)
        replay_client = FakeBotClient([copy.deepcopy(batch)])
        self.worker(replay_client, state=reopened).run_once()
        self.assertEqual(reopened.sync_buf, "cursor-1")
        self.assertEqual(replay_client.sent, [])
        self.assertEqual(len(self.entries()), 1)

    def test_partial_batch_retries_only_unacknowledged_message(self):
        batch = {"msgs": [message("m-1"), message("m-2")], "get_updates_buf": "cursor-1"}
        class FailSecond(FakeBotClient):
            def send_long_text(self, *args, **kwargs):
                if self.sent:
                    raise OSError("second send failed")
                return super().send_long_text(*args, **kwargs)
        worker = self.worker(FailSecond([copy.deepcopy(batch)]))
        with self.assertRaises(OSError):
            worker.run_once()
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(len(self.state.dispositions), 1)
        self.assertEqual(self.state.sync_buf, "")
        client = FakeBotClient([copy.deepcopy(batch)])
        result = self.worker(client, state=StateStore(self.state_path)).run_once()
        self.assertEqual((result["handled"], result["ignored"]), (1, 1))
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(self.entries()), 2)


if __name__ == "__main__":
    unittest.main()
