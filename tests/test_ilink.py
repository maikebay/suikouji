"""iLink 客户端的测试。

全部离线：只在 ILinkClient._open 这一个出口上打桩，
不发任何真实请求，也不需要账号。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from channel import ilink  # noqa: E402


class FakeClient(ilink.ILinkClient):
    """把 _open 换掉，记录每次请求，按队列返回预设响应。"""

    def __init__(self, responses=None, **kw):
        super().__init__(token="tok", **kw)
        self.responses = list(responses or [])
        self.calls = []

    def _open(self, req, timeout_ms, endpoint):
        body = req.data.decode("utf-8") if req.data else None
        self.calls.append({
            "endpoint": endpoint,
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.headers.items()},
            "payload": json.loads(body) if body else None,
            "timeout_ms": timeout_ms,
        })
        if not self.responses:
            return {"ret": 0}
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class TestRequestShape(unittest.TestCase):
    def test_headers(self):
        client = FakeClient()
        client._post("ilink/bot/x", {"a": 1})
        headers = client.calls[0]["headers"]
        self.assertEqual(headers["authorizationtype"], "ilink_bot_token")
        self.assertEqual(headers["ilink-app-id"], "bot")
        self.assertEqual(headers["ilink-app-clientversion"], str(ilink.ILINK_APP_CLIENT_VERSION))
        self.assertEqual(headers["authorization"], "Bearer tok")
        self.assertEqual(headers["content-type"], "application/json")

    def test_wechat_uin_changes_per_request(self):
        """X-WECHAT-UIN 每次都要重新随机，固定值会被服务端当异常。"""
        client = FakeClient()
        for _ in range(5):
            client._post("ilink/bot/x", {})
        uins = {c["headers"]["x-wechat-uin"] for c in client.calls}
        self.assertEqual(len(uins), 5)
        for uin in uins:
            # base64(十进制整数)
            import base64
            int(base64.b64decode(uin).decode())

    def test_base_info_injected(self):
        client = FakeClient()
        client._post("ilink/bot/x", {"hello": "world"})
        payload = client.calls[0]["payload"]
        self.assertEqual(payload["hello"], "world")
        self.assertEqual(payload["base_info"]["channel_version"], ilink.CHANNEL_VERSION)

    def test_url_joins_without_double_slash(self):
        client = FakeClient(base_url="https://example.test/")
        client._post("ilink/bot/x", {})
        self.assertEqual(client.calls[0]["url"], "https://example.test/ilink/bot/x")


class TestErrorHandling(unittest.TestCase):
    def test_ok_passes_through(self):
        client = FakeClient([{"ret": 0, "msgs": []}])
        self.assertEqual(client.check({"ret": 0}, "ep")["ret"], 0)

    def test_ret_nonzero_raises(self):
        with self.assertRaises(ilink.ILinkError) as ctx:
            ilink.ILinkClient.check({"ret": -1, "errmsg": "boom"}, "ep")
        self.assertIn("boom", str(ctx.exception))

    def test_session_expired_flag(self):
        with self.assertRaises(ilink.ILinkError) as ctx:
            ilink.ILinkClient.check({"errcode": -14}, "ep")
        self.assertTrue(ctx.exception.session_expired)
        self.assertFalse(ctx.exception.rate_limited)

    def test_rate_limited_flag(self):
        with self.assertRaises(ilink.ILinkError) as ctx:
            ilink.ILinkClient.check({"ret": -2}, "ep")
        self.assertTrue(ctx.exception.rate_limited)
        self.assertFalse(ctx.exception.session_expired)


class TestGetUpdates(unittest.TestCase):
    def test_timeout_is_not_an_error(self):
        """长轮询超时是常态，要原样带回 sync_buf 继续轮询，不能抛。"""
        client = FakeClient([TimeoutError("ep")])
        out = client.get_updates("buf-1")
        self.assertEqual(out["msgs"], [])
        self.assertEqual(out["get_updates_buf"], "buf-1")
        self.assertEqual(out["ret"], 0)

    def test_passes_sync_buf(self):
        client = FakeClient([{"ret": 0, "msgs": [], "get_updates_buf": "buf-2"}])
        out = client.get_updates("buf-1")
        self.assertEqual(client.calls[0]["payload"]["get_updates_buf"], "buf-1")
        self.assertEqual(out["get_updates_buf"], "buf-2")

    def test_long_poll_timeout_is_35s(self):
        client = FakeClient([{"ret": 0}])
        client.get_updates("")
        self.assertEqual(client.calls[0]["timeout_ms"], 35000)


class TestSendText(unittest.TestCase):
    def test_payload_shape(self):
        client = FakeClient([{"ret": 0}])
        client.send_text("peer-1", "今天卖了1280", context_token="ctx-1")
        msg = client.calls[0]["payload"]["msg"]
        self.assertEqual(msg["to_user_id"], "peer-1")
        self.assertEqual(msg["from_user_id"], "")
        self.assertEqual(msg["message_type"], ilink.MSG_TYPE_BOT)
        self.assertEqual(msg["message_state"], ilink.MSG_STATE_FINISH)
        self.assertEqual(msg["context_token"], "ctx-1")
        self.assertEqual(msg["item_list"], [
            {"type": ilink.ITEM_TEXT, "text_item": {"text": "今天卖了1280"}}])

    def test_omits_empty_context_token(self):
        client = FakeClient([{"ret": 0}])
        client.send_text("peer-1", "hi")
        self.assertNotIn("context_token", client.calls[0]["payload"]["msg"])

    def test_client_id_is_fresh_each_time(self):
        client = FakeClient([{"ret": 0}, {"ret": 0}])
        client.send_text("p", "a")
        client.send_text("p", "b")
        ids = {c["payload"]["msg"]["client_id"] for c in client.calls}
        self.assertEqual(len(ids), 2)

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            FakeClient().send_text("p", "   ")

    def test_retries_without_token_on_session_expiry(self):
        """-14 时去掉 context_token 再发一次，不能让商家的消息直接丢。"""
        client = FakeClient([{"errcode": -14}, {"ret": 0}])
        client.send_text_resilient("p", "hi", context_token="stale")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["payload"]["msg"]["context_token"], "stale")
        self.assertNotIn("context_token", client.calls[1]["payload"]["msg"])

    def test_does_not_retry_other_errors(self):
        client = FakeClient([{"ret": -1, "errmsg": "boom"}])
        with self.assertRaises(ilink.ILinkError):
            client.send_text_resilient("p", "hi", context_token="ctx")
        self.assertEqual(len(client.calls), 1)

    def test_does_not_retry_without_a_token_to_drop(self):
        client = FakeClient([{"errcode": -14}])
        with self.assertRaises(ilink.ILinkError):
            client.send_text_resilient("p", "hi")
        self.assertEqual(len(client.calls), 1)


class TestSplitMessage(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(ilink.split_message("今天卖了1280"), ["今天卖了1280"])

    def test_empty(self):
        self.assertEqual(ilink.split_message(""), [])

    def test_splits_at_newlines(self):
        text = "\n".join(f"第{i}行" for i in range(500))
        chunks = ilink.split_message(text, limit=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("\n".join(chunks), text)   # 内容不丢

    def test_never_cuts_mid_line_when_a_line_fits(self):
        text = "aaaa\nbbbb\ncccc"
        self.assertEqual(ilink.split_message(text, limit=10), ["aaaa\nbbbb", "cccc"])

    def test_hard_cuts_a_single_oversized_line(self):
        chunks = ilink.split_message("x" * 25, limit=10)
        self.assertEqual(chunks, ["x" * 10, "x" * 10, "x" * 5])

    def test_real_limit_is_2000(self):
        self.assertEqual(ilink.MAX_MESSAGE_LENGTH, 2000)
        self.assertEqual(len(ilink.split_message("字" * 4500)), 3)


class TestCdnGuard(unittest.TestCase):
    def test_accepts_weixin_cdn(self):
        ilink._assert_cdn_url("https://novac2c.cdn.weixin.qq.com/c2c?x=1")

    def test_rejects_other_hosts(self):
        for bad in ("https://evil.example.com/x",
                    "http://novac2c.cdn.weixin.qq.com/c2c",     # 不是 https
                    "https://weixin.qq.com.evil.com/x"):
            with self.assertRaises(ilink.ILinkError, msg=bad):
                ilink._assert_cdn_url(bad)

    def test_rejects_when_no_url_at_all(self):
        with self.assertRaises(ilink.ILinkError):
            ilink.ILinkClient().download_media({})


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        store = ilink.StateStore(self.path)
        store.set_sync_buf("buf-9")
        store.set_context_token("peer-1", "ctx-9")

        again = ilink.StateStore(self.path)
        self.assertEqual(again.sync_buf, "buf-9")
        self.assertEqual(again.get_context_token("peer-1"), "ctx-9")
        self.assertEqual(again.get_context_token("nobody"), "")

    def test_missing_file_is_empty_not_an_error(self):
        store = ilink.StateStore(os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(store.sync_buf, "")
        self.assertEqual(store.tokens, {})

    def test_reopen_inherits_account_and_rejects_other_account(self):
        store = ilink.StateStore(self.path, account_id="account-a")
        store.set_sync_buf("buf-a")
        store.set_context_token("peer-a", "ctx-a")

        reopened = ilink.StateStore(self.path)
        self.assertEqual(reopened.account_id, "account-a")
        self.assertEqual(reopened.sync_buf, "buf-a")
        self.assertEqual(reopened.get_context_token("peer-a"), "ctx-a")
        with self.assertRaises(ilink.StateStoreError):
            ilink.StateStore(self.path, account_id="account-b")
        self.assertEqual(ilink.load_json(self.path)["account_id"], "account-a")

    def test_account_binding_is_persisted_before_first_poll(self):
        state = ilink.StateStore(self.path)
        state.bind_account("account-a")
        self.assertEqual(ilink.load_json(self.path)["account_id"], "account-a")
        with self.assertRaises(ilink.StateStoreError):
            ilink.StateStore(self.path).bind_account("account-b")

    def test_unbound_populated_state_cannot_be_attached_to_an_account(self):
        for data in ({"sync_buf": "old-cursor"},
                     {"context_tokens": {"peer": "old-token"}},
                     {"dispositions": {"old-message": "processed"}}):
            with self.subTest(data=data):
                ilink.save_json(self.path, data)
                before = Path(self.path).read_bytes()
                with self.assertRaises(ilink.StateStoreError):
                    ilink.StateStore(self.path, account_id="account-a")
                with self.assertRaises(ilink.StateStoreError):
                    ilink.StateStore(self.path).bind_account("account-a")
                self.assertEqual(Path(self.path).read_bytes(), before)

    def test_binding_flush_failure_does_not_publish_account(self):
        state = ilink.StateStore(self.path)
        with mock.patch.object(state, "flush", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                state.bind_account("account-a")
        self.assertEqual(state.account_id, "")

    def test_atomic_state_write_does_not_follow_predictable_temp_symlink(self):
        victim = Path(self.tmp.name) / "fake-account.json"
        victim.write_bytes(b"preserve fake account")
        Path(self.path + ".tmp").symlink_to(victim)
        ilink.save_json(self.path, {"sync_buf": "new-cursor"})
        self.assertEqual(victim.read_bytes(), b"preserve fake account")
        self.assertEqual(ilink.load_json(self.path), {"sync_buf": "new-cursor"})

    def test_credentials_are_owner_only(self):
        """凭据落盘要 0600，不能让同机其他用户读到。"""
        path = os.path.join(self.tmp.name, "acct.json")
        ilink.save_json(path, {"token": "secret"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(ilink.load_json(path), {"token": "secret"})


class StubLoginClient:
    """给 login() 用的假客户端：check/_get 都直接吐预设响应。"""

    def __init__(self, qr_statuses, base_url=ilink.ILINK_BASE_URL):
        self.qr_statuses = list(qr_statuses)
        self.base_url = base_url
        self.qr_fetches = 0
        self.polled = []

    def check(self, data, endpoint):
        return data

    def _get(self, endpoint, timeout_ms=None):
        if endpoint.startswith(ilink.EP_GET_BOT_QR):
            self.qr_fetches += 1
            return {"qrcode": f"qr-{self.qr_fetches}",
                    "qrcode_img_content": f"https://liteapp/{self.qr_fetches}"}
        query = urllib.parse.urlparse(endpoint).query
        self.polled.append(urllib.parse.parse_qs(query).get("qrcode", [""])[0])
        return self.qr_statuses.pop(0)


class TestLogin(unittest.TestCase):
    def _login(self, statuses, **kw):
        client = StubLoginClient(statuses)
        shown, seen = [], []
        creds = ilink.login(client, on_qrcode=lambda v, u: shown.append((v, u)),
                            on_status=seen.append, sleep=lambda s: None, **kw)
        return creds, client, shown, seen

    def test_happy_path(self):
        creds, client, shown, seen = self._login([
            {"status": "wait"},
            {"status": "scaned"},
            {"status": "confirmed", "ilink_bot_id": "abc@im.bot", "bot_token": "tk",
             "baseurl": "https://ilinkai.weixin.qq.com", "ilink_user_id": "u1"},
        ])
        self.assertEqual(creds["account_id"], "abc@im.bot")
        self.assertEqual(creds["token"], "tk")
        self.assertEqual(creds["user_id"], "u1")
        # 给人看的是完整 URL，不是那串 hex
        self.assertEqual(shown, [("qr-1", "https://liteapp/1")])
        self.assertEqual(seen, ["wait", "scaned", "confirmed"])

    def test_redirect_host_switches_base_url(self):
        creds, client, _, _ = self._login([
            {"status": "scaned_but_redirect", "redirect_host": "sh.weixin.qq.com"},
            {"status": "confirmed", "ilink_bot_id": "a", "bot_token": "t"},
        ])
        self.assertEqual(client.base_url, "https://sh.weixin.qq.com")
        self.assertEqual(creds["base_url"], ilink.ILINK_BASE_URL)  # 服务端没给就用默认

    def test_expired_refreshes_then_gives_up(self):
        with self.assertRaises(ilink.ILinkError) as ctx:
            self._login([{"status": "expired"}] * 4)
        self.assertIn("过期", str(ctx.exception))

    def test_expired_then_refreshed_qr_is_used(self):
        creds, client, shown, _ = self._login([
            {"status": "expired"},
            {"status": "confirmed", "ilink_bot_id": "a", "bot_token": "t"},
        ])
        self.assertEqual(client.qr_fetches, 2)
        self.assertEqual([v for v, _ in shown], ["qr-1", "qr-2"])
        self.assertEqual(client.polled, ["qr-1", "qr-2"])   # 轮询用的是新码
        self.assertEqual(creds["account_id"], "a")

    def test_confirmed_without_credentials_is_an_error(self):
        with self.assertRaises(ilink.ILinkError) as ctx:
            self._login([{"status": "confirmed", "ilink_bot_id": "a"}])
        self.assertIn("没给全", str(ctx.exception))

    def test_times_out(self):
        ticks = iter([0, 1, 2, 999])
        client = StubLoginClient([{"status": "wait"}] * 5)
        with self.assertRaises(ilink.ILinkError) as ctx:
            ilink.login(client, sleep=lambda s: None, monotonic=lambda: next(ticks),
                        timeout_seconds=10)
        self.assertIn("超时", str(ctx.exception))

    def test_missing_qrcode_is_an_error(self):
        class Bad(StubLoginClient):
            def _get(self, endpoint, timeout_ms=None):
                return {"qrcode_img_content": "https://x"}
        with self.assertRaises(ilink.ILinkError):
            ilink.login(Bad([]), sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
