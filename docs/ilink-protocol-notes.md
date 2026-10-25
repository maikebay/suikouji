# iLink 机器人协议要点（自建微信通道用）

> 历史协议参考来源：`~/.hermes/hermes-agent/gateway/platforms/weixin.py`。这是外部实现的资料出处，**不属于本项目，不是运行依赖，也不参与项目调度或验收**；只作协议参考，不安装、不修改。
> 下面每条都标了出处行号，可回查。本文只记「协议事实」，不含任何账号或令牌。
> 接入方向是腾讯 **iLink Bot**：扫码取得机器人身份，再长轮询。它不等同于普通个人微信。
> 资料表明该方式不需要公网回调；本项目目前完成离线实现，真实账号、扫码和完整入站消息格式尚未联调验收。

## 1. 连接方式为什么可行

- 长轮询（`getupdates`，超时 35s）而不是 webhook ⇒ **不需要公网 IP / 域名 / 备案**。
- 扫码登录拿到的是一个 **iLink 机器人身份**（形如 `5637ef459da7@im.bot`），
  不是可脚本化的个人号 ⇒ 能收发的范围以 iLink 的规则为准（**群聊基本收不到事件，按单聊做**）。
- 协议参考包含**文件**类型（`ITEM_FILE`），可作为未来对话导表格的参考。当前 Bot worker 明确忽略文件和其他媒体；CSV 导入仅通过受控 API/CLI/本地网页完成。

## 2. 常量（weixin.py:91-108, 113-114, 167-175）

| 名称 | 值 |
| --- | --- |
| `ILINK_BASE_URL` | `https://ilinkai.weixin.qq.com` |
| `WEIXIN_CDN_BASE_URL` | `https://novac2c.cdn.weixin.qq.com/c2c` |
| `ILINK_APP_ID` | `bot` |
| `CHANNEL_VERSION` | `2.2.0` |
| `ILINK_APP_CLIENT_VERSION` | `(2<<16)|(2<<8)|0` = `131584` |
| 长轮询超时 | `LONG_POLL_TIMEOUT_MS = 35000` |
| 普通接口超时 | `API_TIMEOUT_MS = 15000` |
| `SESSION_EXPIRED_ERRCODE` | `-14` |
| `RATE_LIMIT_ERRCODE` | `-2`（限频，要退避重试） |
| `ITEM_TEXT/IMAGE/VOICE/FILE/VIDEO` | `1 / 2 / 3 / 4 / 5` |
| `MEDIA_IMAGE/VIDEO/FILE/VOICE` | `1 / 2 / 3 / 4` |
| `MSG_TYPE_USER` / `MSG_TYPE_BOT` | `1` / `2` |
| `MSG_STATE_FINISH` | `2` |

### 端点（weixin.py:97-103）

```
POST ilink/bot/getupdates      长轮询收消息
POST ilink/bot/sendmessage     发消息
POST ilink/bot/sendtyping      正在输入
POST ilink/bot/getconfig       取配置（含 typing_ticket）
POST ilink/bot/getuploadurl    取上传地址（发媒体用）
GET  ilink/bot/get_bot_qrcode?bot_type=<bot_type>
GET  ilink/bot/get_qrcode_status?qrcode=<qrcode_value>
```

## 3. 请求头（weixin.py:239-251）

```
Content-Type: application/json
AuthorizationType: ilink_bot_token
Content-Length: <len(body utf-8)>
X-WECHAT-UIN: <base64(十进制随机 uint32)>
iLink-App-Id: bot
iLink-App-ClientVersion: 131584
Authorization: Bearer <token>          # 有 token 时才带
```

`X-WECHAT-UIN` 的算法（weixin.py:230-232）：
`base64.b64encode(str(struct.unpack(">I", secrets.token_bytes(4))[0]).encode())`
—— 每次请求重新随机。

## 4. 请求体（weixin.py:400-420）

所有 POST 都是 `{**payload, "base_info": {"channel_version": "2.2.0"}}`，
`json.dumps` 后按 utf-8 编码发送。

**收消息**（weixin.py:453-465）：
```json
{"get_updates_buf": "<上次返回的 sync_buf>", "base_info": {"channel_version": "2.2.0"}}
```
→ `{"ret": 0, "msgs": [...], "get_updates_buf": "<新的>"}`。
客户端保留超时空批次兼容行为，并附内部 `_transport_timeout` 标记；worker 识别后按传输超时退避，不推进游标或刷新成功心跳。只有真实成功响应（含空消息批次）算就绪依据。

**发消息**（weixin.py:468-499）：
```json
{"msg": {"from_user_id": "", "to_user_id": "<对方>", "client_id": "<自己生成>",
         "message_type": 2, "message_state": 2,
         "item_list": [{"type": 1, "text_item": {"text": "<正文>"}}],
         "context_token": "<该会话最新的>"},
 "base_info": {"channel_version": "2.2.0"}}
```

### 两条必须遵守的规则

1. **每条回复都要回带该会话最新的 `context_token`**（weixin.py:8）。
   它随入站消息一起来，按「账号 + 对方」缓存（weixin.py:299, 1469-1471）。
   发送失败且 `errcode == -14` 时，**去掉 `context_token` 重发一次**
   （iLink 接受无 token 发送，weixin.py:1770-1779）。
2. **单条消息上限 2000 字**（`MAX_MESSAGE_LENGTH`，weixin.py:1178），
   超了要自己切分（weixin.py:1726）。

## 5. 扫码登录流程（weixin.py:1060-1160）

1. `GET get_bot_qrcode?bot_type=<bot_type>` → `{"qrcode": "<hex token>", "qrcode_img_content": "<可扫的 liteapp URL>"}`
2. 轮询 `GET get_qrcode_status?qrcode=<qrcode>`，`status` 取值：
   - `wait` 等待扫码
   - `scaned` 已扫码待确认
   - `scaned_but_redirect` → 用返回的 `redirect_host` 换 `base_url` 继续轮询
   - `expired` → 重新取二维码（最多刷 3 次）
   - `confirmed` → 返回 `ilink_bot_id` / `bot_token` / `baseurl` / `ilink_user_id`
3. **扫码要用 `qrcode_img_content`（完整 URL），不是 `qrcode`（纯 hex）**（weixin.py:1071-1073）。
4. 拿到的凭据要存盘（Hermes 存在 `~/.hermes/weixin/accounts/`）。

## 6. 媒体：AES-128-ECB 加密 CDN

- 入站媒体带 `media.encrypt_query_param` 或 `media.full_url`，
  下载地址 = `{cdn_base_url}?{encrypt_query_param}`（weixin.py:666-683）。
- 有 `aes_key_b64` 时，下载完要 **AES-128-ECB 解密**（weixin.py:209-220, 380-388）。
  密钥解析：base64 解码后按长度判定（16 字节直接用；32 字节是 hex 文本要再 `bytes.fromhex`）。
- 图片的 key 在 `image_item.aeskey`，是 **hex 字符串**，要先 `bytes.fromhex` 再 base64
  （weixin.py:1635-1637）—— 跟其它类型的 `media.aes_key`（已是 base64）格式不同，容易踩。

### ⚠️ 本机环境约束（实测）

```
aiohttp        没有
cryptography   没有
requests       没有
Crypto         没有
qrcode         没有
qrencode 等命令行二维码工具  没有
```

所以自建通道要**纯标准库**实现三件事：

| 要做的 | 标准库方案 | 代价 |
| --- | --- | --- |
| HTTP（含 35s 长轮询） | `urllib.request` | 小，直接可用 |
| AES-128-ECB 解密 | 自己写（GF(256) + 轮密钥） | 约 150 行，**发媒体和收表格都要它** |
| 二维码渲染 | 自己写编码器，或**只打印 URL** | 约 300 行；或登录时用别的办法显示 |

二维码这一条是登录体验的卡点：没有 `qrcode` 库也没命令行工具，
扫码登录就得自己实现 QR 编码。**待定**（见下）。

## 7. 还没验证的

- `getupdates` 返回的 `msgs[]` 里每条消息的**完整字段**（`message_id` / `sender_id` /
  `item_list` / `context_token` 都见过，但没拿到真实样本）。
- 群聊到底能不能收到事件（Hermes 里有 `_guess_chat_type` 和群策略，
  但前面已判断机器人身份收不到群事件，**需要实测**）。
- 发媒体（`getuploadurl` + 上传 + `aeskey`）的完整流程没细读。
- 限频（`errcode -2`）的实际阈值。
