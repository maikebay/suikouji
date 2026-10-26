"""离线生命周期回归；仅临时账号/数据库和 fake 客户端，不监听端口。"""
from __future__ import annotations

import json
import signal
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from channel.bot import BotConfigurationError, ILinkBotWorker, build_worker
from channel.ilink import ILinkError, StateStore
from engine import runtime
from engine.agent_service import Principal
from engine.store import SqliteStore
from engine.web import DemoHandler


class TemporaryRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.account_path = self.root / "account.json"
        self.account_path.write_text(json.dumps({
            "account_id": "fake-bot", "token": "fake-token", "user_id": "fake-user",
            "base_url": "https://ilink.example.test",
        }), encoding="utf-8")
        self.config = runtime.RuntimeConfig(
            str(self.root / "runtime.db"), owner_id="owner", principal_id="bot-agent",
            workspace_id="shop", ilink_account_path=str(self.account_path),
            ilink_state_path=str(self.root / "state.json"), ilink_allowed_peers="peer-1,peer-2",
        )
        # 任一意外真实 HTTP 请求/扫码都让测试立即失败。
        for target in ("urllib.request.urlopen", "channel.ilink.login", "channel.ilink.fetch_qrcode"):
            guard = patch(target, side_effect=AssertionError("external I/O forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def initialize_database(self):
        store = SqliteStore(self.config.db_path)
        store.ensure_shop("shop", "临时测试", owner_id="owner", profile_id="restaurant")
        store.close()

    def lifecycle(self, client):
        self.initialize_database()
        client_patch = patch.object(runtime, "ILinkClient", return_value=client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        lifecycle = runtime.ILinkRuntime(
            self.config, runtime.load_ilink_account(str(self.account_path)), threading.RLock(),
        )
        self.addCleanup(lifecycle.close)
        return lifecycle


class ConfigurationTests(TemporaryRuntimeTest):
    def test_empty_environ_does_not_read_ambient_configuration(self):
        with patch.dict("os.environ", {"SUIKOUJI_ILINK_ACCOUNT_PATH": "/do-not-read/real.json",
                                       "SUIKOUJI_PORT": "not-a-port"}):
            config = runtime.RuntimeConfig.from_env({})
        self.assertEqual(config.ilink_account_path, "")
        self.assertEqual(config.port, 8765)
        self.assertTrue(config.api_only)

    def test_boolean_is_strict(self):
        for value in ("1", "true", "YES", " false ", "no", "0"):
            self.assertEqual(runtime.RuntimeConfig.from_env({"SUIKOUJI_API_ONLY": value}).api_only,
                             value.strip().lower() in {"1", "true", "yes"})
        for value in ("", "typo", "enabled", "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.RuntimeConfig.from_env({"SUIKOUJI_API_ONLY": value})

    def test_complete_configuration_and_peer_mapping(self):
        self.config.validate()
        self.assertEqual(self.config.allowed_peers(), {"peer-1": "shop", "peer-2": "shop"})
        config = runtime.RuntimeConfig.from_env({
            "SUIKOUJI_DB": self.config.db_path,
            "SUIKOUJI_ILINK_ACCOUNT_PATH": self.config.ilink_account_path,
            "SUIKOUJI_ILINK_STATE_PATH": self.config.ilink_state_path,
            "SUIKOUJI_ILINK_ALLOWED_PEERS": "peer-1, peer-2",
            "SUIKOUJI_WORKSPACE_ID": "shop",
        })
        config.validate()
        self.assertEqual(config.allowed_peers(), self.config.allowed_peers())

    def test_partial_or_ambiguous_configuration_is_rejected_before_io(self):
        invalid = [
            {"ilink_account_path": ""}, {"ilink_state_path": ""}, {"ilink_allowed_peers": ""},
            {"workspace_id": ""}, {"owner_id": " "}, {"principal_id": ""},
            {"ilink_allowed_peers": "peer-1,"}, {"ilink_allowed_peers": "peer-1,,peer-2"},
            {"ilink_allowed_peers": "peer-1,peer-1"}, {"ilink_allowed_peers": "peer-1 peer-2"},
            {"ilink_allowed_peers": "*"}, {"ilink_allowed_peers": ["peer-1"]},
            {"ilink_account_path": "relative.json"}, {"api_only": False}, {"api_only": "1"},
            {"db_path": "data/staging.db"}, {"db_path": ":memory:"}, {"port": True},
            {"ilink_state_path": str(runtime.ROOT / "data" / "never-open.json")},
            {"ilink_state_path": self.config.ilink_account_path},
            {"ilink_account_path": self.config.db_path},
            {"ilink_account_path": self.config.ilink_state_path + ".tmp"},
        ]
        with patch.object(runtime, "load_ilink_account") as load, patch.object(runtime, "make_server") as make:
            for changes in invalid:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    runtime.run(replace(self.config, **changes))
            load.assert_not_called()
            make.assert_not_called()

    def test_symlink_into_repository_is_rejected(self):
        link = self.root / "repo-link"
        link.symlink_to(runtime.ROOT, target_is_directory=True)
        with self.assertRaises(ValueError):
            replace(self.config, ilink_state_path=str(link / "data" / "never-open.json")).validate()

    def test_account_uses_explicit_saved_credential_format(self):
        account = runtime.load_ilink_account(str(self.account_path))
        self.assertEqual((account.account_id, account.token, account.base_url),
                         ("fake-bot", "fake-token", "https://ilink.example.test"))
        self.assertNotIn("fake-token", repr(account))
        self.account_path.write_text('{"account_id":"fake-bot","token":"fake-token"}', encoding="utf-8")
        self.assertEqual(runtime.load_ilink_account(str(self.account_path)).base_url, runtime.ILINK_BASE_URL)

    def test_bad_accounts_fail_closed_without_disclosing_content(self):
        bad_values = [[], {}, {"account_id": "a"}, {"account_id": "a", "token": ""},
                      {"account_id": 42, "token": "fake-secret"},
                      {"account_id": "a", "token": "fake-secret", "unknown": True},
                      {"account_id": "a", "token": "fake-secret", "user_id": None}]
        for url in (None, "", "http://example.test", "https://u:p@example.test",
                    "https://example.test/path", "https://example.test?token=fake-secret",
                    "https://example.test#fragment", "https://example.test:bad", "https://[bad"):
            bad_values.append({"account_id": "a", "token": "fake-secret", "base_url": url})
        for value in bad_values:
            self.account_path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(BotConfigurationError) as ctx:
                runtime.load_ilink_account(str(self.account_path))
            self.assertNotIn("fake-secret", str(ctx.exception))
        self.account_path.write_text('{"token": "fake-secret",', encoding="utf-8")
        with self.assertRaises(BotConfigurationError) as ctx:
            runtime.run(self.config)
        self.assertNotIn("fake-secret", str(ctx.exception))
        self.account_path.unlink()
        with patch.object(runtime, "make_server") as make, self.assertRaises(BotConfigurationError):
            runtime.run(self.config)
        make.assert_not_called()
        self.assertFalse(Path(self.config.db_path).exists())

    def test_cli_zero_port_is_validated_instead_of_ignored(self):
        with patch.object(runtime.RuntimeConfig, "from_env", return_value=self.config):
            with self.assertRaises(ValueError):
                runtime.main(["--port", "0"])


class FakePollingClient:
    def __init__(self):
        self.polled = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.sent = []

    def get_updates(self, sync_buf=""):
        self.calls += 1
        if self.calls > 1:
            self.polled.set()
            if not self.release.wait(3):
                raise RuntimeError("test poll was not released")
        return {"msgs": [], "get_updates_buf": sync_buf}

    def send_long_text(self, *args):
        self.sent.append(args)


class WorkerLifecycleTests(TemporaryRuntimeTest):
    def test_construction_database_use_and_close_share_worker_thread(self):
        client = FakePollingClient()
        lifecycle = self.lifecycle(client)
        main_ident = threading.get_ident()
        actions = []

        class TrackingStore(SqliteStore):
            def __init__(self, path, *, migrate=True):
                actions.append(("open", threading.get_ident()))
                if migrate:
                    raise AssertionError("runtime worker must never migrate")
                super().__init__(path, migrate=migrate)

            def close(self):
                actions.append(("close", threading.get_ident()))
                super().close()

        original_builder = runtime.build_worker

        def builder(*args, **kwargs):
            actions.append(("build", threading.get_ident()))
            worker = original_builder(*args, **kwargs)
            original_run_once = worker.run_once

            def run_once():
                actions.append(("poll", threading.get_ident()))
                worker.store.conn.execute("SELECT 1").fetchone()
                return original_run_once()
            worker.run_once = run_once
            return worker

        def make_client(**kwargs):
            actions.append(("client", threading.get_ident()))
            self.assertEqual(kwargs, {"token": "fake-token", "base_url": "https://ilink.example.test"})
            return client

        with patch("channel.bot.SqliteStore", TrackingStore), \
                patch.object(runtime, "build_worker", side_effect=builder), \
                patch.object(runtime, "ILinkClient", side_effect=make_client):
            self.assertFalse(lifecycle.ready())
            lifecycle.start()
            self.assertTrue(client.polled.wait(2))
            self.assertTrue(lifecycle.ready())
            self.assertEqual(lifecycle.worker.account_id, "fake-bot")
            self.assertEqual(lifecycle.worker.principal, Principal("bot-agent", "owner"))
            lifecycle.stop()
            self.assertFalse(lifecycle.ready())
            client.release.set()
            lifecycle.close(2)
        self.assertFalse(lifecycle.thread.is_alive())
        self.assertIsNone(lifecycle.error)
        self.assertEqual(actions[0][0], "client")
        self.assertEqual(actions[-1][0], "close")
        self.assertEqual([action for action, _ in actions].count("close"), 1)
        self.assertTrue(all(ident == lifecycle.thread.ident != main_ident for _, ident in actions))

    def test_close_is_safe_before_start(self):
        lifecycle = runtime.ILinkRuntime(self.config, runtime.load_ilink_account(str(self.account_path)),
                                         threading.RLock())
        lifecycle.close()
        self.assertFalse(lifecycle.ready())
        self.assertFalse(Path(self.config.db_path).exists())

    def test_missing_database_never_creates_file_or_client(self):
        lifecycle = runtime.ILinkRuntime(self.config, runtime.load_ilink_account(str(self.account_path)),
                                         threading.RLock())
        with patch.object(runtime, "ILinkClient") as client, self.assertRaises(BotConfigurationError):
            lifecycle.start()
        lifecycle.close(2)
        self.assertFalse(Path(self.config.db_path).exists())
        client.assert_not_called()
        self.assertFalse(lifecycle.ready())

    def test_wrong_owner_or_state_account_rejects_before_worker_construction(self):
        self.initialize_database()
        for changes, state in (({"owner_id": "other"}, {}),
                               ({}, {"account_id": "other-bot"}),
                               ({}, {"sync_buf": []})):
            Path(self.config.ilink_state_path).write_text(json.dumps(state), encoding="utf-8")
            lifecycle = runtime.ILinkRuntime(replace(self.config, **changes),
                                             runtime.load_ilink_account(str(self.account_path)), threading.RLock())
            with patch.object(runtime, "ILinkClient", return_value=FakePollingClient()), \
                    patch("channel.bot.SqliteStore") as store, self.assertRaises(BotConfigurationError):
                lifecycle.start()
            lifecycle.close(2)
            store.assert_not_called()
            self.assertFalse(lifecycle.ready())

    def test_builder_closes_store_when_worker_constructor_fails(self):
        ident = threading.get_ident()
        closed = []
        store = SimpleNamespace(close=lambda: closed.append(threading.get_ident()))
        with patch("channel.bot.SqliteStore", return_value=store), \
                patch("channel.bot.ILinkBotWorker", side_effect=BotConfigurationError("bad config")), \
                self.assertRaises(BotConfigurationError):
            build_worker(self.config.db_path, FakePollingClient(), self.config.ilink_state_path,
                         principal=Principal("agent", "owner"), workspace_id="shop", allowed_peers={})
        self.assertEqual(closed, [ident])

    def test_post_construction_failure_closes_store_in_worker_thread(self):
        lifecycle = self.lifecycle(FakePollingClient())
        closed = []
        original_close = SqliteStore.close

        def close(store):
            closed.append(threading.get_ident())
            original_close(store)

        with patch("channel.bot.SqliteStore.close", close), \
                patch("engine.agent_service.AgentService.workspace", side_effect=ValueError("unavailable")), \
                self.assertRaises(BotConfigurationError):
            lifecycle.start()
        lifecycle.close(2)
        self.assertEqual(closed, [lifecycle.thread.ident])
        self.assertFalse(lifecycle.ready())

    def test_expired_session_stops_without_reauth_and_removes_readiness(self):
        client = FakePollingClient()
        expired = ILinkError("fake expired", errcode=-14)

        def get_updates(_buf):
            client.calls += 1
            if client.calls == 1:
                return {"msgs": [], "get_updates_buf": ""}
            client.polled.set()
            if not client.release.wait(3):
                raise RuntimeError("test poll timed out")
            raise expired
        client.get_updates = get_updates
        lifecycle = self.lifecycle(client)
        lifecycle.start()
        self.assertTrue(client.polled.wait(2))
        self.assertTrue(lifecycle.ready())
        client.release.set()
        lifecycle.join(2)
        self.assertFalse(lifecycle.ready())
        self.assertIs(lifecycle.error, expired)
        self.assertEqual(client.calls, 2)
        state = json.loads(Path(self.config.ilink_state_path).read_text(encoding="utf-8"))
        self.assertEqual(state["account_id"], "fake-bot")
        self.assertEqual(state["sync_buf"], "")

    def test_unexpected_worker_exit_cannot_remain_ready(self):
        client = FakePollingClient()
        lifecycle = self.lifecycle(client)
        with patch.object(ILinkBotWorker, "run_forever", return_value=None):
            try:
                lifecycle.start()
            except BotConfigurationError:
                pass  # 退出可能发生在 start 返回前，也可能紧接其后。
            lifecycle.join(2)
        self.assertFalse(lifecycle.ready())
        self.assertIsInstance(lifecycle.error, RuntimeError)

    def test_startup_timeout_cleans_up_worker_created_after_stop(self):
        lifecycle = self.lifecycle(FakePollingClient())
        release = threading.Event()
        entered = threading.Event()
        original_builder = runtime.build_worker

        def slow_builder(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test startup was not released")
            return original_builder(*args, **kwargs)

        with patch.object(runtime, "build_worker", side_effect=slow_builder):
            with self.assertRaisesRegex(BotConfigurationError, "超时"):
                lifecycle.start(timeout=0.01)
            self.assertTrue(entered.wait(2))
            lifecycle.stop()
            release.set()
            lifecycle.close(2)
        self.assertIsNone(lifecycle.error)
        self.assertFalse(lifecycle.thread.is_alive())

    def test_shutdown_deadline_reports_stuck_poll_without_cross_thread_close(self):
        client = FakePollingClient()
        lifecycle = self.lifecycle(client)
        lifecycle.start()
        self.assertTrue(client.polled.wait(2))
        with self.assertRaisesRegex(RuntimeError, "关闭期限"):
            lifecycle.close(timeout=0.01)
        self.assertTrue(lifecycle.thread.is_alive())
        client.release.set()
        lifecycle.close(2)
        self.assertFalse(lifecycle.thread.is_alive())


class BotStoppingTests(TemporaryRuntimeTest):
    def worker(self, client, **kwargs):
        return ILinkBotWorker(client, SimpleNamespace(), StateStore(self.config.ilink_state_path),
                              principal=Principal("fake-bot", "owner"), workspace_id="shop",
                              allowed_peers={"peer-1": "shop"}, **kwargs)

    def test_default_backoff_is_interruptible(self):
        polling = threading.Event()

        def get_updates(_buf):
            polling.set()
            raise OSError("offline")
        worker = self.worker(SimpleNamespace(get_updates=get_updates), backoff_base=60)
        thread = threading.Thread(target=worker.run_forever)
        thread.start()
        self.assertTrue(polling.wait(2))
        worker.stop()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(worker.ready())

    def test_injected_fake_sleep_preserves_backoff_values(self):
        failures = [ILinkError("rate limited", status_code=429), OSError("offline")]
        delays = []

        def get_updates(_buf):
            raise failures.pop(0)

        def fake_sleep(delay):
            delays.append(delay)
            if len(delays) == 2:
                worker.stop()
        worker = self.worker(SimpleNamespace(get_updates=get_updates), sleep=fake_sleep)
        worker.run_forever()
        self.assertEqual(delays, [2.0, 2.0])

    def test_stop_during_poll_ignores_returned_batch(self):
        def get_updates(_buf):
            worker.stop()
            return {"get_updates_buf": "must-not-commit", "msgs": [{"not": "a message"}]}
        worker = self.worker(SimpleNamespace(get_updates=get_updates))
        before = Path(self.config.ilink_state_path).read_bytes()
        self.assertEqual(worker.run_once(), {"stopped": True, "handled": 0, "ignored": 0})
        self.assertEqual(worker.state.sync_buf, "")
        self.assertEqual(Path(self.config.ilink_state_path).read_bytes(), before)

    def test_fatal_failure_stops_instead_of_retrying(self):
        worker = self.worker(SimpleNamespace(get_updates=Mock(side_effect=ValueError("broken"))),
                             sleep=Mock(side_effect=AssertionError("must not retry")))
        worker.run_forever()
        self.assertTrue(worker.stop_event.is_set())
        self.assertFalse(worker.ready())
        self.assertIsInstance(worker.last_error, ValueError)

    def test_ready_requires_success_and_clears_on_stop_or_error(self):
        worker = self.worker(FakePollingClient())
        self.assertFalse(worker.ready())
        worker.last_success_at = time.time() - 121
        self.assertFalse(worker.ready())
        worker.last_success_at = time.time()
        self.assertTrue(worker.ready())
        worker.last_error = OSError("offline")
        self.assertFalse(worker.ready())
        worker.last_error = None
        worker.stop()
        self.assertFalse(worker.ready())


class FakeServer:
    def __init__(self, serve=None):
        self.server_port = 8765
        self.RequestHandlerClass = DemoHandler
        self.serve = serve
        self.served = False
        self.closed = False
        self.shutdown = Mock()

    def serve_forever(self):
        self.served = True
        if self.serve:
            self.serve(self)

    def server_close(self):
        self.closed = True


class RuntimeOwnershipTests(TemporaryRuntimeTest):
    def test_api_only_default_never_loads_account_or_constructs_worker(self):
        config = runtime.RuntimeConfig(str(self.root / "api.db"))
        server = FakeServer()
        with patch.object(runtime, "load_ilink_account") as load, \
                patch.object(runtime, "ILinkRuntime") as worker, \
                patch.object(runtime, "make_server", return_value=server), \
                patch.object(runtime, "install_signal_handlers", return_value={}):
            runtime.run(config)
        load.assert_not_called()
        worker.assert_not_called()
        self.assertTrue(server.served)
        self.assertTrue(server.closed)
        self.assertIs(server.RequestHandlerClass, DemoHandler)

    def test_run_wires_worker_and_cleans_up_after_server_failure(self):
        events = []
        lock_seen = []
        worker = SimpleNamespace(start=lambda: events.append("start"),
                                 stop=lambda: events.append("stop"),
                                 close=lambda: events.append("close"), ready=lambda: True)

        def serve(server):
            events.append("serve")
            self.assertIs(server.worker, worker)
            self.assertIsNot(server.RequestHandlerClass, DemoHandler)
            raise OSError("fake server failed")
        server = FakeServer(serve)

        def make_worker(config, account, lock):
            lock_seen.append(lock)
            self.assertEqual(config, self.config)
            self.assertEqual(account.account_id, "fake-bot")
            return worker
        with patch.object(runtime, "ILinkRuntime", side_effect=make_worker), \
                patch.object(runtime, "make_server", return_value=server), \
                patch.object(runtime, "install_signal_handlers", return_value={}) as signals, \
                self.assertRaises(OSError):
            runtime.run(self.config)
        signals.assert_called_once_with(server, worker)
        self.assertEqual(events, ["start", "serve", "stop", "close"])
        self.assertTrue(server.closed)
        self.assertEqual(len(lock_seen), 1)

    def test_server_bind_failure_stops_and_joins_owned_worker(self):
        worker = Mock()
        with patch.object(runtime, "ILinkRuntime", return_value=worker), \
                patch.object(runtime, "make_server", side_effect=OSError("port occupied")), \
                self.assertRaises(OSError):
            runtime.run(self.config)
        worker.start.assert_called_once()
        worker.stop.assert_called_once()
        worker.close.assert_called_once()

    def test_startup_failure_never_starts_http_and_always_closes_worker(self):
        worker = Mock()
        worker.start.side_effect = BotConfigurationError("invalid state")
        with patch.object(runtime, "ILinkRuntime", return_value=worker), \
                patch.object(runtime, "make_server") as server, \
                self.assertRaises(BotConfigurationError):
            runtime.run(self.config)
        worker.stop.assert_called_once()
        worker.close.assert_called_once()
        server.assert_not_called()

    def test_partial_signal_installation_restores_previous_handler(self):
        old_term = object()
        setter = Mock(side_effect=[old_term, ValueError("cannot register"), None])
        with patch.object(runtime.signal, "signal", setter), self.assertRaises(ValueError):
            runtime.install_signal_handlers(FakeServer())
        self.assertEqual(setter.call_args_list[-1], unittest.mock.call(signal.SIGTERM, old_term))

    def test_signal_stops_worker_before_async_server_shutdown_and_restores_handlers(self):
        installed = {}
        previous = {signal.SIGTERM: object(), signal.SIGINT: object()}
        events = []
        stopped = threading.Event()
        server = FakeServer()
        server.shutdown = lambda: (events.append("shutdown"), stopped.set())
        worker = SimpleNamespace(stop=lambda: events.append("stop"))

        def set_signal(signum, callback):
            installed[signum] = callback
            return previous[signum]
        with patch.object(runtime.signal, "signal", side_effect=set_signal):
            handlers = runtime.install_signal_handlers(server, worker)
            installed[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(stopped.wait(2))
        self.assertEqual(events, ["stop", "shutdown"])
        self.assertEqual(handlers, previous)
        with patch.object(runtime, "make_server", return_value=FakeServer()), \
                patch.object(runtime, "install_signal_handlers", return_value=previous), \
                patch.object(runtime.signal, "signal") as restore:
            runtime.run(runtime.RuntimeConfig(str(self.root / "api.db")))
        self.assertEqual(restore.call_args_list, [unittest.mock.call(k, v) for k, v in previous.items()])


class HandlerIntegrationTests(TemporaryRuntimeTest):
    def attached_handler(self, worker, parent=DemoHandler):
        server = FakeServer()
        server.RequestHandlerClass = parent
        lock = threading.RLock()
        runtime._attach_worker(server, worker, lock)
        return server.RequestHandlerClass, lock

    def test_runtime_handler_sets_request_idle_timeout(self):
        class Parent:
            def setup(self):
                return "set up"
        handler_class, _ = self.attached_handler(Mock(), Parent)
        handler = handler_class()
        handler.request = Mock()
        self.assertEqual(handler.setup(), "set up")
        handler.request.settimeout.assert_called_once_with(runtime.HTTP_REQUEST_TIMEOUT)

    def test_ready_endpoint_combines_database_and_worker_readiness_without_socket(self):
        worker = Mock()
        handler_class, _ = self.attached_handler(worker)
        handler = object.__new__(handler_class)
        handler.path = "/readyz"
        handler._send = lambda status, body, **_kwargs: (status, body)
        worker.ready.return_value = False
        with patch.object(DemoHandler, "_ready_probe", return_value=(True, "ready")):
            status, body = handler.do_GET()
            self.assertEqual(status, 503)
            self.assertEqual(body["error"]["details"]["reason"], "ilink_not_ready")
            worker.ready.return_value = True
            self.assertEqual(handler.do_GET()[0], 200)
        with patch.object(DemoHandler, "_ready_probe", return_value=(False, "workspace_missing")):
            self.assertEqual(handler.do_GET()[1]["error"]["details"]["reason"], "workspace_missing")
        handler.path = "/healthz"
        self.assertEqual(handler.do_GET()[0], 200)
        self.assertIsNot(handler_class, DemoHandler)

    def test_request_and_bot_capture_use_same_serialization_lock(self):
        capture_entered = threading.Event()
        capture_done = threading.Event()
        database_lock = threading.RLock()
        service = SimpleNamespace(capture_text=Mock(return_value={"status": "pending"}))
        client = SimpleNamespace(send_long_text=Mock())
        worker = ILinkBotWorker(client, SimpleNamespace(), StateStore(self.config.ilink_state_path),
                                principal=Principal("fake-bot", "owner"), workspace_id="shop",
                                allowed_peers={"peer-1": "shop"}, service=service,
                                database_lock=database_lock)
        msg = {"message_id": "m-1", "sender_id": "peer-1", "message_type": 1,
               "message_state": 2, "item_list": [{"type": 1, "text_item": {"text": "测试"}}]}

        def capture():
            capture_entered.set()
            worker._handle_message(msg)
            capture_done.set()

        class Parent:
            def handle(self):
                contender = threading.Thread(target=capture)
                contender.start()
                self.assertions.assertTrue(capture_entered.wait(2))
                self.assertions.assertFalse(capture_done.wait(0.02))
                self.assertions.assertFalse(service.capture_text.called)
                self.contender = contender
        server = FakeServer()
        server.RequestHandlerClass = Parent
        runtime._attach_worker(server, SimpleNamespace(ready=lambda: True), database_lock)
        handler = server.RequestHandlerClass()
        handler.assertions = self
        handler.handle()
        handler.contender.join(2)
        self.assertFalse(handler.contender.is_alive())
        self.assertTrue(capture_done.is_set())
        service.capture_text.assert_called_once()
        client.send_long_text.assert_called_once()

    def test_long_poll_never_holds_database_lock(self):
        lock = threading.RLock()
        polled = threading.Event()
        release = threading.Event()

        def get_updates(_buf):
            polled.set()
            if not release.wait(3):
                raise RuntimeError("test poll was not released")
            worker.stop()
            return {"msgs": []}
        worker = ILinkBotWorker(SimpleNamespace(get_updates=get_updates), SimpleNamespace(),
                                StateStore(self.config.ilink_state_path), principal=Principal("fake", "owner"),
                                workspace_id="shop", allowed_peers={"peer-1": "shop"}, database_lock=lock)
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        self.assertTrue(polled.wait(2))
        acquired = lock.acquire(timeout=0.1)
        try:
            self.assertTrue(acquired)
        finally:
            if acquired:
                lock.release()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
