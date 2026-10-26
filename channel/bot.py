"""iLink Bot 到随口记业务层的文本 worker。

这里只处理已验证的单聊文本消息。它不是普通个人微信客户端：未知消息形状、
媒体、群聊和未授权 peer 都默认忽略，不猜字段、不写账。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
from contextlib import nullcontext

from engine.agent_service import AgentError, AgentService, Principal
from engine.store import SqliteStore
from .ilink import (ILinkError, ILinkClient, StateStore, StateStoreError,
                    ITEM_TEXT, MSG_STATE_FINISH, MSG_TYPE_USER)

LOG = logging.getLogger(__name__)


class BotConfigurationError(ValueError):
    pass


class ILinkBotWorker:
    """一个账号一个 worker；StateStore 不支持并发写入，因此只单线程运行。"""

    def __init__(self, client: ILinkClient, store, state: StateStore,
                 *, principal: Principal, workspace_id: str,
                 allowed_peers: dict[str, str], account_id: str | None = None,
                 service: AgentService | None = None,
                 sleep=None, backoff_base=1.0, backoff_max=60.0,
                 database_lock=None):
        if not isinstance(workspace_id, str) or not workspace_id:
            raise BotConfigurationError("必须配置工作空间")
        if not isinstance(allowed_peers, dict) or not allowed_peers:
            raise BotConfigurationError("peer allowlist 不能为空")
        if any(not isinstance(peer, str) or not peer or not isinstance(workspace, str) or not workspace
               for peer, workspace in allowed_peers.items()):
            raise BotConfigurationError("peer allowlist 格式不正确")
        self.account_id = account_id or principal.id
        if not isinstance(self.account_id, str) or not self.account_id:
            raise BotConfigurationError("必须配置 iLink account_id")
        try:
            state.bind_account(self.account_id)
        except (ValueError, StateStoreError) as exc:
            raise BotConfigurationError(str(exc)) from exc
        self.client = client
        self.store = store
        self.state = state
        self.principal = principal
        self.workspace_id = workspace_id
        self.allowed_peers = dict(allowed_peers)
        self.service = service or AgentService(store)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.stop_event = threading.Event()
        # 默认退避可被 stop 唤醒；测试仍可注入记录 delay 的假 sleep。
        self.sleep = self.stop_event.wait if sleep is None else sleep
        self.database_lock = database_lock if database_lock is not None else nullcontext()
        self.last_poll_at = None
        self.last_success_at = None
        self.last_error = None
        self._backoff = backoff_base

    @staticmethod
    def _message_id(msg: dict) -> str:
        value = msg.get("message_id")
        if type(value) is not str or not value.strip() or len(value) > 100:
            return ""
        return value.strip()

    @staticmethod
    def _peer(msg: dict) -> str:
        value = msg.get("sender_id")
        if type(value) is not str or not value.strip():
            return ""
        return value.strip()

    @staticmethod
    def _context_token(msg: dict) -> str | None:
        if "context_token" not in msg:
            return ""
        value = msg.get("context_token")
        if type(value) is not str:
            return None
        return value.strip()

    @classmethod
    def _identity(cls, msg: object, account_id: str) -> str:
        if isinstance(msg, dict):
            message_id = msg.get("message_id")
            peer = msg.get("sender_id")
            if type(message_id) is str and type(peer) is str and message_id and peer:
                return f"ilink:{account_id}:{peer}:{message_id}"
        try:
            raw = json.dumps(msg, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = repr(msg)
        return f"ilink:{account_id}:payload:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    @classmethod
    def normalize_text_message(cls, msg: object) -> dict | None:
        """只接受完整、明确的文本消息；未知 payload 不进入业务层。"""
        if not isinstance(msg, dict):
            return None
        if set(msg) - {"message_id", "sender_id", "message_type", "message_state",
                       "context_token", "item_list"}:
            return None
        if type(msg.get("message_type")) is not int or msg.get("message_type") != MSG_TYPE_USER:
            return None
        if type(msg.get("message_state")) is not int or msg.get("message_state") != MSG_STATE_FINISH:
            return None
        message_id = cls._message_id(msg)
        peer = cls._peer(msg)
        items = msg.get("item_list")
        if not message_id or not peer or not isinstance(items, list) or len(items) != 1:
            return None
        item = items[0]
        if not isinstance(item, dict) or set(item) - {"type", "text_item"}:
            return None
        if type(item.get("type")) is not int or item.get("type") != ITEM_TEXT:
            return None
        text_item = item.get("text_item")
        if not isinstance(text_item, dict) or set(text_item) != {"text"}:
            return None
        text = text_item.get("text")
        context_token = cls._context_token(msg)
        if type(text) is not str or not text.strip() or len(text) > 2000:
            return None
        if context_token is None:
            return None
        return {"message_id": message_id, "peer": peer, "text": text,
                "context_token": context_token}

    @staticmethod
    def format_reply(receipt: dict) -> str:
        status = receipt.get("status")
        if status == "needs_confirmation":
            return "原话已保存。当前账本的行业尚不支持自动记录账目和随想，需要先确认适用的记录方式。"
        if status == "rejected":
            return "原话已保存，但日期或金额等内容未通过校验，账目和随想均未写入，请先核对。"
        if status not in {"parsed", "partial", "unparsed"}:
            return "暂时没拿到结果，正在核对。"
        entries = receipt.get("entries") or []
        sales = receipt.get("sales") or []
        observations = receipt.get("observations") or []
        thoughts = receipt.get("thoughts") or []
        leftovers = (receipt.get("parsed") or {}).get("leftovers") or []
        parts = []
        if receipt.get("duplicate"):
            parts.append("这条消息已记录，没有重复入账或保存。")
        saved = []
        if entries:
            saved.append(f"账目 {len(entries)} 条")
        if sales:
            saved.append(f"销量 {len(sales)} 条")
        if observations:
            saved.append(f"经营观测 {len(observations)} 条")
        if saved:
            parts.append("已记录：" + "、".join(saved) + "。")
        if thoughts:
            dates = sorted({row["date"] for row in thoughts if row.get("date")})
            date_label = (dates[0] if len(dates) == 1 else f"{dates[0]}至{dates[-1]}") if dates else ""
            parts.append(f"已保存 {len(thoughts)} 条随想" + (f"（记录日：{date_label}）" if date_label else "") + "。")
        if not saved and not thoughts and not receipt.get("duplicate"):
            parts.append("原话已保存，暂未识别出可单独记录的账目或随想。")
        if leftovers:
            parts.append("以下片段尚待核对，涉及的金额未入账：" + "；".join(f"“{text}”" for text in leftovers) + "。")
        elif status == "partial":
            parts.append("还有部分内容待核对，未处理部分涉及的金额未入账。")
        if leftovers or status == "unparsed":
            parts.append("未识别的内容可交给已连接的外部助手整理；请先核对已有记录，不要重发整段原话。")
        if any("归档日" in str(warning) for warning in receipt.get("warnings") or []):
            parts.append("指定随想归档日需要外部助手整理，相关随想尚未保存。")
        elif receipt.get("warnings"):
            parts.append("另有待核对提示，请核对已保存内容。")
        return "".join(parts)

    def _send_reply(self, peer: str, text: str):
        token = self.state.get_context_token(peer)
        self.client.send_long_text(peer, text, token)

    def _handle_message(self, msg: dict) -> bool:
        normalized = self.normalize_text_message(msg)
        if not normalized:
            LOG.warning("ignored unsupported iLink message")
            return True
        peer = normalized["peer"]
        workspace = self.allowed_peers.get(peer)
        if not workspace or workspace != self.workspace_id:
            LOG.warning("ignored non-allowlisted iLink peer")
            return True
        token = normalized["context_token"]
        dedup_ref = f"ilink:{self.account_id}:{peer}:{normalized['message_id']}"
        # AgentService request IDs have a bounded size; hash only the stable
        # account/peer/message identity, never message text.
        request_id = "ilink-" + hashlib.sha256(dedup_ref.encode("utf-8")).hexdigest()
        with self.database_lock:
            receipt = self.service.capture_text(
                self.principal, workspace, normalized["text"], request_id,
                source="ilink", peer=peer, dedup_ref=dedup_ref,
            )
        if token:
            self.state.set_context_token(peer, token)
        self._send_reply(peer, self.format_reply(receipt))
        self.state.mark_disposition(dedup_ref, "processed")
        return True

    def run_once(self) -> dict:
        """处理一批消息；只有全部成功才推进 sync buffer。"""
        if self.stop_event.is_set():
            return {"stopped": True, "handled": 0, "ignored": 0}
        old_buf = self.state.sync_buf
        self.last_poll_at = time.time()
        updates = self.client.get_updates(old_buf)
        if self.stop_event.is_set():
            return {"stopped": True, "handled": 0, "ignored": 0}
        if updates.get("_transport_timeout") is True:
            raise TimeoutError("iLink poll did not receive a successful response")
        new_buf = updates.get("get_updates_buf", old_buf)
        msgs = updates.get("msgs", [])
        if not isinstance(msgs, list) or not isinstance(new_buf, str):
            raise ILinkError("iLink getupdates 返回结构不正确")
        handled = ignored = 0
        for msg in msgs:
            if self.stop_event.is_set():
                return {"stopped": True, "handled": handled, "ignored": ignored}
            identity = self._identity(msg, self.account_id)
            if identity in self.state.dispositions:
                ignored += 1
                continue
            before = self.normalize_text_message(msg)
            if before and self.allowed_peers.get(before["peer"]) == self.workspace_id:
                try:
                    self._handle_message(msg)
                except AgentError as exc:
                    if exc.code not in {"invalid_text", "invalid_source", "profile_mismatch",
                                        "workspace_forbidden", "idempotency_conflict"}:
                        raise
                    self.state.mark_disposition(identity, f"rejected:{exc.code}")
                    LOG.error("agent operation rejected permanently: %s", exc.code)
                handled += 1
            else:
                self.state.mark_disposition(identity, "ignored")
                self._handle_message(msg)
                ignored += 1
        if new_buf != old_buf:
            self.state.set_sync_buf(new_buf)
        self.last_success_at = time.time()
        self.last_error = None
        self._backoff = self.backoff_base
        return {"stopped": False, "handled": handled, "ignored": ignored,
                "sync_buf": new_buf}

    def _recover_session(self):
        """登录本身由显式 supervisor 提供；这里不偷偷扫码或读取凭据。"""
        raise BotConfigurationError("iLink 会话已过期，需要外部 supervisor 重新加载账号凭据")

    def run_forever(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except ILinkError as exc:
                self.last_error = exc
                if exc.session_expired:
                    LOG.error("iLink session expired; worker stopped")
                    try:
                        self._recover_session()
                    except BotConfigurationError:
                        self.stop_event.set()
                        break
                delay = self._backoff * 2 if exc.rate_limited else self._backoff
                self.sleep(min(delay, self.backoff_max))
                self._backoff = min(self._backoff * 2, self.backoff_max)
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                self.last_error = exc
                self.sleep(min(self._backoff, self.backoff_max))
                self._backoff = min(self._backoff * 2, self.backoff_max)
            except AgentError as exc:
                self.last_error = exc
                LOG.error("agent operation rejected: %s", exc.code)
                self.sleep(min(self._backoff, self.backoff_max))
            except BotConfigurationError as exc:
                self.last_error = exc
                LOG.error("iLink worker configuration requires intervention")
                self.stop_event.set()
                break
            except Exception as exc:
                self.last_error = exc
                LOG.error("iLink worker stopped on unexpected error; intervention required")
                self.stop_event.set()
                break

    def stop(self):
        self.stop_event.set()

    def ready(self, max_stale_seconds=120) -> bool:
        if self.stop_event.is_set() or self.last_error is not None or self.last_success_at is None:
            return False
        return time.time() - self.last_success_at <= max_stale_seconds


def build_worker(db_path: str, client: ILinkClient, state_path: str, *,
                 principal: Principal, workspace_id: str, allowed_peers: dict[str, str],
                 account_id: str | None = None, database_lock=None):
    # 状态先校验，避免配置错误时打开数据库；构造失败也由当前线程关闭连接。
    state = StateStore(state_path, account_id=account_id or principal.id)
    store = SqliteStore(db_path, migrate=False)
    try:
        return ILinkBotWorker(client, store, state, principal=principal,
                              workspace_id=workspace_id, allowed_peers=allowed_peers,
                              account_id=account_id, database_lock=database_lock)
    except BaseException:
        store.close()
        raise
