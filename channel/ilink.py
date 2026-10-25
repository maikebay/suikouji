"""随口记 · iLink Bot API 客户端（纯标准库）。

腾讯 iLink 是「扫码登录一个机器人身份 + 长轮询收消息」的接口，
所以不需要公网地址、不需要公众号或企业微信资质。

协议出处逐条见 docs/ilink-protocol-notes.md；外部实现仅作协议参考，
不是本项目的运行依赖。

这里只做通道本身：登录、收、发、下载附件。
「收到的话怎么变成账」在 channel/bot.py。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import aes

# ---------------------------------------------------------------- 常量

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2

ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

MAX_MESSAGE_LENGTH = 2000

# 时间戳文件（sync_buf、context_token）跟账号放一起
DEFAULT_STATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class ILinkError(RuntimeError):
    """iLink 返回了非 0 的 ret / errcode 或 HTTP 错误。"""

    def __init__(self, message: str, *, ret=None, errcode=None, errmsg=None,
                 endpoint: str = "", status_code: int | None = None,
                 retry_after: str | None = None):
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        self.endpoint = endpoint
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def session_expired(self) -> bool:
        return SESSION_EXPIRED_ERRCODE in (self.ret, self.errcode)

    @property
    def rate_limited(self) -> bool:
        return self.status_code == 429 or RATE_LIMIT_ERRCODE in (self.ret, self.errcode)


# ---------------------------------------------------------------- HTTP


def _wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str | None, body: bytes) -> dict:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body)),
        "X-WECHAT-UIN": _wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _timeout_seconds(ms: int) -> float:
    # urllib 的 timeout 要留一点余量，否则长轮询正好卡在服务端超时上
    return ms / 1000 + 5


class ILinkClient:
    """一个机器人账号的客户端。token 为空时只能做扫码登录。"""

    def __init__(self, token: str | None = None, base_url: str = ILINK_BASE_URL,
                 cdn_base_url: str = WEIXIN_CDN_BASE_URL, timeout_ms: int = API_TIMEOUT_MS):
        self.token = token
        self.base_url = (base_url or ILINK_BASE_URL).rstrip("/")
        self.cdn_base_url = (cdn_base_url or WEIXIN_CDN_BASE_URL).rstrip("/")
        self.timeout_ms = timeout_ms

    # ------------------------------------------------------------ 底层

    def _post(self, endpoint: str, payload: dict, timeout_ms: int | None = None) -> dict:
        body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
                          ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/{endpoint}"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=_headers(self.token, body))
        return self._open(req, timeout_ms or self.timeout_ms, endpoint)

    def _get(self, endpoint: str, timeout_ms: int | None = None) -> dict:
        url = f"{self.base_url}/{endpoint}"
        req = urllib.request.Request(url, method="GET")
        return self._open(req, timeout_ms or self.timeout_ms, endpoint)

    def _open(self, req, timeout_ms: int, endpoint: str) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=_timeout_seconds(timeout_ms)) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ILinkError(
                f"iLink {endpoint} HTTP {exc.code}: {detail}",
                endpoint=endpoint,
                status_code=exc.code,
                retry_after=exc.headers.get("Retry-After") if exc.headers else None,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TimeoutError(endpoint) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ILinkError(f"iLink {endpoint} 返回的不是 JSON：{raw[:200]}",
                             endpoint=endpoint) from exc
        return data

    @staticmethod
    def check(data: dict, endpoint: str) -> dict:
        """iLink 用 ret/errcode 报错，HTTP 状态码还是 200，必须自己查。"""
        ret = data.get("ret")
        errcode = data.get("errcode")
        if ret in (0, None) and errcode in (0, None):
            return data
        err = ILinkError(
            f"iLink {endpoint} 失败：ret={ret} errcode={errcode} "
            f"errmsg={data.get('errmsg')}",
            ret=ret, errcode=errcode, errmsg=data.get("errmsg"), endpoint=endpoint)
        raise err

    # ------------------------------------------------------------ 收

    def get_updates(self, sync_buf: str = "", timeout_ms: int = LONG_POLL_TIMEOUT_MS) -> dict:
        """长轮询收消息。保留兼容的超时空批次，并标明非成功响应。"""
        try:
            data = self._post(EP_GET_UPDATES, {"get_updates_buf": sync_buf},
                              timeout_ms=timeout_ms)
        except TimeoutError:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf, "_transport_timeout": True}
        return self.check(data, EP_GET_UPDATES)

    # ------------------------------------------------------------ 发

    def send_text(self, to_user_id: str, text: str,
                  context_token: str | None = None) -> dict:
        """发一条文本。微信这边原生渲染 Markdown，所以表格直接发就行。"""
        if not text or not text.strip():
            raise ValueError("send_text: 正文不能为空")
        message = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": secrets.token_hex(8),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        return self.check(self._post(EP_SEND_MESSAGE, {"msg": message}), EP_SEND_MESSAGE)

    def send_text_resilient(self, to_user_id: str, text: str,
                            context_token: str | None = None) -> dict:
        """发文本，会话过期时去掉 context_token 再发一次。

        iLink 接受不带 token 的发送（协议笔记第 4 节），
        所以 token 过期不该让商家的话直接丢掉。
        """
        try:
            return self.send_text(to_user_id, text, context_token)
        except ILinkError as exc:
            if not exc.session_expired or not context_token:
                raise
            return self.send_text(to_user_id, text, None)

    def send_long_text(self, to_user_id: str, text: str,
                       context_token: str | None = None) -> list:
        """超过 2000 字就切开分条发，返回每条的结果。"""
        return [self.send_text_resilient(to_user_id, chunk, context_token)
                for chunk in split_message(text)]

    # ------------------------------------------------------------ 附件

    def download_media(self, media: dict, aes_key_b64: str | None = None,
                       full_url: str | None = None) -> bytes:
        """下载（并解密）一个附件。

        media 是 item 里 *_item.media 那个字典，带 encrypt_query_param。
        """
        param = (media or {}).get("encrypt_query_param")
        url = (media or {}).get("full_url") or full_url
        if param:
            target = f"{self.cdn_base_url}?{param}"
        elif url:
            _assert_cdn_url(url)
            target = url
        else:
            raise ILinkError("附件既没有 encrypt_query_param 也没有 full_url")

        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        if aes_key_b64:
            raw = aes.ecb_decrypt(raw, aes.parse_aes_key(aes_key_b64))
        return raw


def _assert_cdn_url(url: str) -> None:
    """只从微信自己的 CDN 下载，防止服务端返回的 URL 把我们带去别处。"""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host.endswith(".weixin.qq.com")
                                        or host.endswith(".qq.com")):
        raise ILinkError(f"拒绝从非微信 CDN 下载：{url[:120]}")


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list:
    """按长度切分，尽量在换行处断，不要把表格切散。"""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                     # 单行就超长，硬切
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------- 扫码登录


def fetch_qrcode(client: ILinkClient, bot_type: str = "3") -> tuple:
    """取登录二维码，返回 (qrcode_value, 可扫的 URL)。"""
    data = client.check(
        client._get(f"{EP_GET_BOT_QR}?{urllib.parse.urlencode({'bot_type': bot_type})}",
                    timeout_ms=QR_TIMEOUT_MS),
        EP_GET_BOT_QR)
    value = str(data.get("qrcode") or "")
    url = str(data.get("qrcode_img_content") or "")
    if not value:
        raise ILinkError("iLink 没返回 qrcode", endpoint=EP_GET_BOT_QR)
    # 微信扫的是完整 URL，不是那串 hex
    return value, (url or value)


def poll_qrcode(client: ILinkClient, qrcode_value: str) -> dict:
    """查一次扫码状态，返回原样的响应。

    status: wait / scaned / scaned_but_redirect / expired / confirmed
    """
    return client.check(
        client._get(f"{EP_GET_QR_STATUS}?{urllib.parse.urlencode({'qrcode': qrcode_value})}",
                    timeout_ms=QR_TIMEOUT_MS),
        EP_GET_QR_STATUS)


def login(client: ILinkClient | None = None, bot_type: str = "3",
          timeout_seconds: int = 300, on_qrcode=None, on_status=None,
          sleep=time.sleep, monotonic=time.monotonic) -> dict:
    """扫码登录，返回账号凭据字典。

    on_qrcode(value, url) 用来把二维码显示给人看；
    on_status(status) 用来打印进度。两个都是可选回调，
    这样测试可以不碰终端、不碰网络。
    """
    client = client or ILinkClient()
    qrcode_value, qr_url = fetch_qrcode(client, bot_type)
    if on_qrcode:
        on_qrcode(qrcode_value, qr_url)

    deadline = monotonic() + timeout_seconds
    refreshes = 0
    while monotonic() < deadline:
        try:
            resp = poll_qrcode(client, qrcode_value)
        except ILinkError:
            raise
        except Exception:
            sleep(1)
            continue

        status = str(resp.get("status") or "wait")
        if on_status:
            on_status(status)

        if status == "scaned_but_redirect":
            host = str(resp.get("redirect_host") or "")
            if host:
                client.base_url = f"https://{host}"
        elif status == "expired":
            refreshes += 1
            if refreshes > 3:
                raise ILinkError("二维码多次过期，请重新登录")
            qrcode_value, qr_url = fetch_qrcode(client, bot_type)
            if on_qrcode:
                on_qrcode(qrcode_value, qr_url)
        elif status == "confirmed":
            account_id = str(resp.get("ilink_bot_id") or "")
            token = str(resp.get("bot_token") or "")
            if not account_id or not token:
                raise ILinkError("扫码已确认，但 iLink 没给全账号和 token")
            return {
                "account_id": account_id,
                "token": token,
                "base_url": str(resp.get("baseurl") or ILINK_BASE_URL),
                "user_id": str(resp.get("ilink_user_id") or ""),
            }
        sleep(1)
    raise ILinkError("等扫码超时了")


# ---------------------------------------------------------------- 本地状态


def account_path(state_dir: str = DEFAULT_STATE_DIR) -> str:
    return os.path.join(state_dir, "weixin-account.json")


def state_path(state_dir: str = DEFAULT_STATE_DIR) -> str:
    return os.path.join(state_dir, "weixin-state.json")


def save_json(path: str, data: dict) -> None:
    """凭据落盘，权限收紧到只有自己能读。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # Exclusive creation avoids following a pre-existing predictable .tmp link;
    # mkstemp also sets 0600 before any token or context data is written.
    fd, temporary = tempfile.mkstemp(prefix=".ilink-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except OSError as exc:
        raise StateStoreError(f"无法读取状态文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise StateStoreError(f"状态文件不是有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise StateStoreError(f"状态文件根节点必须是对象：{path}")
    return value


class StateStoreError(RuntimeError):
    pass


class StateStore:
    """按「对方」记住 sync_buf 和 context_token。

    context_token 必须回带给 iLink，否则消息发不出去（协议笔记第 4 节）。
    """

    def __init__(self, path: str = "", *, account_id: str = ""):
        self.path = path or state_path()
        data = load_json(self.path)
        stored_account = data.get("account_id")
        if stored_account is not None and not isinstance(stored_account, str):
            raise StateStoreError("状态文件 account_id 类型不正确")
        if account_id and stored_account and stored_account != account_id:
            raise StateStoreError("状态文件属于其他 iLink 账号")
        if not account_id and stored_account:
            account_id = stored_account
        self.account_id = stored_account or ""
        sync_buf = data.get("sync_buf", "")
        tokens = data.get("context_tokens", {})
        dispositions = data.get("dispositions", {})
        if not isinstance(sync_buf, str) or not isinstance(tokens, dict) or not isinstance(dispositions, dict):
            raise StateStoreError("状态文件字段类型不正确")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in tokens.items()):
            raise StateStoreError("状态文件 context_token 类型不正确")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in dispositions.items()):
            raise StateStoreError("状态文件 disposition 类型不正确")
        self.sync_buf = sync_buf
        self.tokens = dict(tokens)
        self.dispositions = dict(dispositions)
        if account_id:
            self.bind_account(account_id)

    def set_sync_buf(self, value: str) -> None:
        if not value or value == self.sync_buf:
            return
        # 先成功落盘，再推进内存；写失败时下一轮仍会重试同一个游标。
        self.flush(sync_buf=value)
        self.sync_buf = value

    def set_context_token(self, peer: str, token: str) -> None:
        if not token or self.tokens.get(peer) == token:
            return
        tokens = dict(self.tokens)
        tokens[peer] = token
        self.flush(tokens=tokens)
        self.tokens = tokens

    def get_context_token(self, peer: str) -> str:
        return self.tokens.get(peer, "")

    def flush(self, *, sync_buf: str | None = None, tokens: dict | None = None,
              dispositions: dict | None = None, account_id: str | None = None) -> None:
        save_json(self.path, {
            "account_id": self.account_id if account_id is None else account_id,
            "sync_buf": self.sync_buf if sync_buf is None else sync_buf,
            "context_tokens": self.tokens if tokens is None else tokens,
            "dispositions": self.dispositions if dispositions is None else dispositions,
        })

    def mark_disposition(self, identity: str, disposition: str) -> None:
        if not identity or not isinstance(disposition, str) or not disposition:
            raise ValueError("disposition 参数不正确")
        updated = dict(self.dispositions)
        updated[identity] = disposition
        # 只保留最近的一小段审计状态，避免状态文件无限增长。
        if len(updated) > 1000:
            updated = dict(list(updated.items())[-1000:])
        self.save_dispositions(updated)

    def save_dispositions(self, dispositions: dict) -> None:
        self.flush(dispositions=dispositions)
        self.dispositions = dict(dispositions)

    def bind_account(self, account_id: str) -> None:
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id 必须是非空文字")
        if self.account_id and self.account_id != account_id:
            raise StateStoreError("状态文件属于其他 iLink 账号")
        if not self.account_id and (self.sync_buf or self.tokens or self.dispositions):
            raise StateStoreError("已有状态未绑定账号，请人工核对并使用独立状态文件")
        self.flush(account_id=account_id)
        self.account_id = account_id
