"""服务器 staging 运行时配置与生命周期。

默认只绑定回环地址。任何非回环监听都必须显式配置 API key；TLS、反向代理、
私网/VPN 和专用系统用户由部署环境提供，不能由当前标准库 demo 服务假装提供。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from channel.bot import BotConfigurationError, build_worker
from channel.ilink import ILINK_BASE_URL, LONG_POLL_TIMEOUT_MS, ILinkClient
from .agent_service import Principal
from .store import SCHEMA_VERSION, database_schema_ready
from .web import make_server

LOOPBACKS = {"127.0.0.1", "localhost", "::1"}
ROOT = Path(__file__).resolve().parent.parent
ILINK_STARTUP_TIMEOUT = 10
HTTP_REQUEST_TIMEOUT = 10
# urllib 为 35 秒长轮询保留 5 秒余量，另留 5 秒做线程退出/资源清理。
ILINK_SHUTDOWN_TIMEOUT = LONG_POLL_TIMEOUT_MS / 1000 + 10


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"1", "true", "yes", "0", "false", "no"}:
        raise ValueError("SUIKOUJI_API_ONLY 必须为 1/true/yes 或 0/false/no")
    return normalized in {"1", "true", "yes"}


@dataclass(frozen=True)
class RuntimeConfig:
    db_path: str
    bind: str = "127.0.0.1"
    port: int = 8765
    api_key: str = field(default="", repr=False)
    owner_id: str = "local"
    principal_id: str = "agent"
    workspace_id: str = ""
    ilink_account_path: str = ""
    ilink_state_path: str = ""
    ilink_allowed_peers: str = ""
    api_only: bool = True

    @classmethod
    def from_env(cls, environ=None) -> "RuntimeConfig":
        env = os.environ if environ is None else environ
        return cls(
            db_path=env.get("SUIKOUJI_DB", "data/staging.db"),
            bind=env.get("SUIKOUJI_BIND", "127.0.0.1"),
            port=int(env.get("SUIKOUJI_PORT", "8765")),
            api_key=env.get("SUIKOUJI_API_KEY", ""),
            owner_id=env.get("SUIKOUJI_OWNER_ID", "local"),
            principal_id=env.get("SUIKOUJI_PRINCIPAL_ID", "agent"),
            workspace_id=env.get("SUIKOUJI_WORKSPACE_ID", ""),
            ilink_account_path=env.get("SUIKOUJI_ILINK_ACCOUNT_PATH", ""),
            ilink_state_path=env.get("SUIKOUJI_ILINK_STATE_PATH", ""),
            ilink_allowed_peers=env.get("SUIKOUJI_ILINK_ALLOWED_PEERS", ""),
            api_only=_boolean(env.get("SUIKOUJI_API_ONLY", "1")),
        )

    def allowed_peers(self) -> dict[str, str]:
        if not isinstance(self.ilink_allowed_peers, str):
            raise ValueError("iLink peer allowlist 必须为逗号分隔的文字")
        peers = [peer.strip() for peer in self.ilink_allowed_peers.split(",")]
        if (any(not peer or peer == "*" or any(char.isspace() for char in peer) for peer in peers)
                or len(set(peers)) != len(peers)):
            raise ValueError("iLink peer allowlist 不能包含空值、通配符、重复值或空白字符")
        return dict.fromkeys(peers, self.workspace_id)

    def validate(self) -> None:
        if not isinstance(self.db_path, str) or not self.db_path.strip() or self.db_path == ":memory:":
            raise ValueError("staging 必须使用持久化 SQLite 路径")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("端口范围不正确")
        if self.bind not in LOOPBACKS and not self.api_key:
            raise ValueError("非回环监听必须配置 SUIKOUJI_API_KEY")
        if self.bind not in LOOPBACKS and len(self.api_key) < 16:
            raise ValueError("非回环监听的 API key 至少需要 16 个字符")
        for name, value in (("owner_id", self.owner_id), ("principal_id", self.principal_id)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"必须配置有效的 {name}")
        if type(self.api_only) is not bool:
            raise ValueError("api_only 必须为布尔值")
        options = (self.ilink_account_path, self.ilink_state_path, self.ilink_allowed_peers)
        if any(not isinstance(value, str) for value in options):
            raise ValueError("iLink 配置必须为文字")
        if not any(options):
            return
        if not all(options):
            raise ValueError("启用 iLink 必须同时配置账号路径、状态路径和 peer allowlist")
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("启用 iLink 必须配置工作空间")
        if self.workspace_id != self.workspace_id.strip():
            raise ValueError("工作空间不能包含首尾空白字符")
        if not self.api_only:
            raise ValueError("启用 iLink 必须使用 SUIKOUJI_API_ONLY=1")
        self.allowed_peers()
        paths = []
        for value in (self.db_path, self.ilink_account_path, self.ilink_state_path):
            path = Path(value)
            if not path.is_absolute() or path.resolve().is_relative_to(ROOT):
                raise ValueError("启用 iLink 时数据库、账号和状态必须使用仓库外的绝对路径")
            paths.append(path.resolve())
        if len(set(paths)) != len(paths):
            raise ValueError("数据库、iLink 账号和状态路径不能相同")
        if Path(str(paths[2]) + ".tmp") in paths[:2]:
            raise ValueError("iLink 状态临时文件不能覆盖数据库或账号")


@dataclass(frozen=True)
class ILinkAccount:
    account_id: str
    token: str = field(repr=False)
    base_url: str = ILINK_BASE_URL


def load_ilink_account(path: str) -> ILinkAccount:
    """只读取显式指定的 save_json/login 凭据格式；不搜索、不登录、不写回。"""
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError, RecursionError):
        raise BotConfigurationError("无法读取显式配置的 iLink 账号 JSON") from None
    if not isinstance(data, dict) or set(data) - {"account_id", "token", "base_url", "user_id"}:
        raise BotConfigurationError("iLink 账号 JSON 字段不正确")
    for key in ("account_id", "token"):
        value = data.get(key)
        if not isinstance(value, str) or not value or any(char.isspace() for char in value):
            raise BotConfigurationError("iLink 账号必须包含有效的 account_id 和 token")
    if "user_id" in data and not isinstance(data["user_id"], str):
        raise BotConfigurationError("iLink 账号 user_id 类型不正确")
    base_url = data.get("base_url", ILINK_BASE_URL)
    try:
        if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
            raise ValueError
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or "\\" in base_url
                or url.username is not None or url.password is not None or url.query or url.fragment
                or url.path not in {"", "/"}):
            raise ValueError
        # 触发非法端口的校验；不把 URL/token 放进错误信息。
        url.port
    except ValueError:
        raise BotConfigurationError("iLink 账号 base_url 必须为 HTTPS origin") from None
    return ILinkAccount(data["account_id"], data["token"], base_url.rstrip("/"))


def probe_database(path: str, owner_id: str = "", workspace_id: str = "") -> dict:
    """只读探针：不存在或未初始化时失败，不创建、迁移或回填数据库。"""
    if path == ":memory:" or not Path(path).is_file():
        return {"ok": False, "reason": "database_missing"}
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            try:
                version = int(version[0]) if version else None
            except (TypeError, ValueError):
                version = None
            if integrity != "ok" or version != SCHEMA_VERSION or not database_schema_ready(conn):
                return {"ok": False, "reason": "database_not_ready", "integrity": integrity,
                        "schema_version": version}
            if workspace_id:
                row = conn.execute(
                    "SELECT 1 FROM workspace_meta WHERE shop_id=? AND owner_id=?",
                    (workspace_id, owner_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "reason": "workspace_missing"}
            return {"ok": True, "schema_version": version}
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return {"ok": False, "reason": "database_unavailable"}


class ILinkRuntime:
    """拥有一个 worker 线程；该线程独占 Bot 的 SQLite 连接及其关闭操作。"""

    def __init__(self, config: RuntimeConfig, account: ILinkAccount, database_lock):
        self.config = config
        self.account = account
        self.database_lock = database_lock
        self.worker = None
        self.error = None
        self._started = threading.Event()
        self._stopping = threading.Event()
        # 超时会显式报错，由进程 supervisor 终止；绝不跨线程强关连接。
        self.thread = threading.Thread(target=self._run, name="ilink-worker", daemon=True)

    def _run(self):
        try:
            with self.database_lock:
                probe = probe_database(self.config.db_path, self.config.owner_id,
                                       self.config.workspace_id)
                if not probe["ok"]:
                    raise BotConfigurationError("iLink 数据库或 owner/workspace 尚未就绪")
                client = ILinkClient(token=self.account.token, base_url=self.account.base_url)
                self.worker = build_worker(
                    self.config.db_path, client, self.config.ilink_state_path,
                    principal=Principal(self.config.principal_id, self.config.owner_id),
                    workspace_id=self.config.workspace_id,
                    allowed_peers=self.config.allowed_peers(), account_id=self.account.account_id,
                    database_lock=self.database_lock,
                )
                self.worker.service.workspace(self.worker.principal, self.config.workspace_id)
            self._started.set()
            if self._stopping.is_set():
                self.worker.stop()
            self.worker.run_forever()
            if not self._stopping.is_set():
                self.error = self.worker.last_error or RuntimeError("iLink worker 意外退出")
        except BaseException as exc:
            self.error = exc
        finally:
            try:
                if self.worker is not None:
                    with self.database_lock:
                        self.worker.store.close()
            except BaseException as exc:
                self.error = exc
            finally:
                self._started.set()

    def start(self, timeout=ILINK_STARTUP_TIMEOUT):
        self.thread.start()
        if not self._started.wait(timeout):
            raise BotConfigurationError("iLink worker 初始化超时")
        if self.error is not None or not self.thread.is_alive():
            raise BotConfigurationError("iLink worker 初始化失败；请检查账号、状态和工作空间配置")

    def stop(self):
        self._stopping.set()
        if self.worker is not None:
            self.worker.stop()

    def join(self, timeout=ILINK_SHUTDOWN_TIMEOUT):
        if self.thread.ident is None:
            return
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise RuntimeError("iLink worker 未在关闭期限内退出；需要进程 supervisor 介入")

    def close(self, timeout=ILINK_SHUTDOWN_TIMEOUT):
        self.stop()
        self.join(timeout)

    def ready(self) -> bool:
        return (self._started.is_set() and not self._stopping.is_set()
                and self.error is None and self.thread.is_alive()
                and self.worker is not None and self.worker.ready())


def _attach_worker(server, worker: ILinkRuntime, database_lock):
    # make_server 当前没有 worker 参数；只为这个 server 派生 handler，避免全局 monkeypatch。
    handler = server.RequestHandlerClass

    class RuntimeHandler(handler):
        def setup(self):
            self.request.settimeout(HTTP_REQUEST_TIMEOUT)
            return super().setup()

        def handle(self):
            # HTTPServer 单线程处理请求；连接在请求线程创建/关闭，不与 Bot 共用。
            with database_lock:
                return super().handle()

        def _ready_probe(self):
            ok, reason = super()._ready_probe()
            if ok and not worker.ready():
                return False, "ilink_not_ready"
            return ok, reason

    server.RequestHandlerClass = RuntimeHandler
    server.worker = worker


def install_signal_handlers(server, worker=None):
    def stop(_signum, _frame):
        if worker is not None:
            worker.stop()
        # serve_forever runs on the main thread; shutdown must run elsewhere.
        threading.Thread(target=server.shutdown, name="server-shutdown", daemon=True).start()
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
    except BaseException:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise
    return previous


def run(config: RuntimeConfig):
    config.validate()
    # 账号缺省时完全不触碰 iLink 文件、客户端或 worker。
    account = load_ilink_account(config.ilink_account_path) if config.ilink_account_path else None
    database_lock = threading.RLock()
    worker = ILinkRuntime(config, account, database_lock) if account is not None else None
    server = None
    previous_handlers = {}
    try:
        if worker is not None:
            worker.start()
        server = make_server(
            config.port, config.db_path, api_key=config.api_key,
            owner_id=config.owner_id, principal_id=config.principal_id,
            bind=config.bind, workspace_id=config.workspace_id, api_only=config.api_only,
        )
        if worker is not None:
            _attach_worker(server, worker, database_lock)
        previous_handlers = install_signal_handlers(server, worker)
        logging.info("随口记 API listening bind=%s port=%s", config.bind, server.server_port)
        server.serve_forever()
    finally:
        if worker is not None:
            worker.stop()
        try:
            if server is not None:
                server.server_close()
        finally:
            try:
                if worker is not None:
                    worker.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="随口记 staging API 与可选 iLink worker")
    parser.add_argument("--db", default=None)
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    config = RuntimeConfig.from_env()
    config = replace(
        config,
        db_path=config.db_path if args.db is None else args.db,
        bind=config.bind if args.bind is None else args.bind,
        port=config.port if args.port is None else args.port,
    )
    run(config)


if __name__ == "__main__":
    main()
