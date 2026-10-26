# Staging deployment

本目录提供 0.2.0-staging Docker Compose 发布包说明与原生运行参考，不包含账号、token 或数据库。新库直接使用 schema v6，含独立 `thoughts` 与 `thought_tags`；旧 v5 库必须先备份并显式迁移副本。默认发布包只启用 Agent API；当前独立 iLink 通道面向腾讯 iLink **Bot**，不是普通个人微信号，尚未进行真实联调。

## Docker Compose 发布包

给部署 bot 的入口见 [BOT_DEPLOY.md](BOT_DEPLOY.md)。需要 Linux Docker Engine、Docker Compose v2.20+、宿主 Python 3.10+，以及下载基础镜像和时区/证书包的网络。镜像用 Python 3.12，仅业务代码无 pip 依赖。本机无 Docker/Podman，容器构建、挂载和网络未实测；下述命令必须在有授权的目标环境验收。

在解压后的项目根目录运行，状态目录必须是源码外全新绝对路径，其父目录由执行用户拥有。可将 `/opt/suikouji-state` 换成自己拥有的目录。准备工具创建数据、备份、密钥目录和 Compose 配置，不读取旧账本，不覆盖已有目录，不显示密钥。普通用户执行会用其 UID/GID 运行容器；root 执行则为新文件设置容器 UID/GID 10001。rootless Docker/启用 userns-remap 的环境需额外核对映射权限，本包未实测这些环境。

```bash
python3 tools/prepare_deploy.py --state-dir /opt/suikouji-state --workspace-name 我的账本
```

生成的 `compose.env` 是 `--env-file` 输入，**不要用 shell source**。Compose 环境插值自动读取该文件，runtime 不会加载 `.env`。宿主 shell 中同名 `SUIKOUJI_*` 优先，执行前应检查没有旧环境变量覆盖新配置。路径和名字可含空格，准备工具拒绝控制字符、单引号、反斜杠和 `$`。完整选项见 `python3 tools/prepare_deploy.py --help`。

```bash
docker compose --env-file /opt/suikouji-state/compose.env config --quiet
```

```bash
docker compose --env-file /opt/suikouji-state/compose.env build
```

```bash
docker compose --env-file /opt/suikouji-state/compose.env up -d --wait --wait-timeout 90
```

```bash
python3 tools/verify_api.py --config /opt/suikouji-state/compose.env
```

`verify_api.py` 读取显式提供的配置与密钥，仅访问本机 /healthz、/readyz、认证 capabilities/workspaces，不写账、不输出密钥。root 创建的密钥由 UID 10001 持有，宿主验证需由 root 或有相应访问权限的用户执行。容器内通过 Compose secret 挂载文件读取密钥，不把密钥值写进镜像或 Compose 环境。

服务端口固定容器内 8765，宿主默认 `127.0.0.1:8765`；准备参数 `--port` 可改宿主端口。默认仅 v1 与探针，`/`、`/api/*` 返回 404。现有网页为本地 demo，不是本发布包的服务器控制台。Agent 使用 HTTP 工具对接接口，包内没有 MCP/Grok 插件。远程 bot 需要另行配置经授权的 VPN/私网/TLS 反向代理，不能直接公开旧无认证路由。

首次启动用同目录临时库完整初始化 schema v6 后原子发布为 `/var/lib/suikouji/accounting.db`（0600）；目标已存在则只读检查，不补列、迁移或更改 profile。schema/owner/workspace/profile/timezone 不匹配会退出。工作空间名称只用于新建，不在重启时重命名。时间默认 Asia/Shanghai，容器进程 TZ 同步该设置。餐饮 profile 已实现受控解析，其余 profile 不代表已完成对应行业核算。

源码目录可替换，数据留在外部状态目录；Compose 项目名固定 suikouji，禁止在多个解压目录同时启动同一数据目录，禁止扩为多副本或同时用 CLI 写入。容器非 root、根文件系统只读、临时目录可写，停止期限 70 秒。停止只操作本服务，不影响数据：

```bash
docker compose --env-file /opt/suikouji-state/compose.env stop
```

恢复启动使用相同 env 文件，不要重跑准备脚本。

### Compose 备份与恢复

容器内 `/var/lib/suikouji` 与 `/var/backups/suikouji` 对应仓外 data 和 backups。以下文件名是示例，每次必须换成唯一的新名字；命令拒绝已有目标，避免覆盖备份。原生 SQLite backup 可对正在运行的单实例服务生成一致快照。

```bash
docker compose --env-file /opt/suikouji-state/compose.env exec -T api python3 -m engine.deployment backup /var/lib/suikouji/accounting.db /var/backups/suikouji/backup-20261025-001.db
```

```bash
docker compose --env-file /opt/suikouji-state/compose.env exec -T api python3 -m engine.deployment check /var/backups/suikouji/backup-20261025-001.db
```

为避免第二个业务写进程，恢复演练前停止 API，再用一次性容器恢复到全新的演练文件；`run` 的命令参数复用镜像入口，不要重复加 python。

```bash
docker compose --env-file /opt/suikouji-state/compose.env stop
```

```bash
docker compose --env-file /opt/suikouji-state/compose.env run --rm --no-deps api restore /var/backups/suikouji/backup-20261025-001.db /var/lib/suikouji/rehearsal-001.db
```

```bash
docker compose --env-file /opt/suikouji-state/compose.env run --rm --no-deps api check /var/lib/suikouji/rehearsal-001.db
```

```bash
docker compose --env-file /opt/suikouji-state/compose.env up -d --wait --wait-timeout 90
```

演练不会切换当前账本；真正恢复切换须有用户授权、停写并保留旧文件。相同版本的 SQLite backup/restore 包含随想、标签、原话、控制表和完整幂等回执。`engine.cli export` 的 JSON 虽包含随想及标签，仍不是完整数据库备份，不承诺 JSON restore 能恢复全部控制状态；不要以导出文件替代 SQLite 备份。保留离线/异地加密备份并定期恢复演练。

### 从 v5 升级到 v6

此流程面向0.1.1-staging的v5库升级至0.2.0-staging。API、readiness 和容器启动均不自动升级旧库；新版本普通 CLI 打开旧库也不是推荐升级方式。以下均为已授权目标环境的操作说明，本文没有执行真实迁移、切换或 Docker 验收。

1. 保留旧程序/镜像和原部署配置。用**旧版本**的 `engine.deployment backup` 对v5库生成唯一的新备份，并用旧版本 `check` 验证。新版本的普通 backup/check 要求v6，不能拿它们直接验证旧库。运行中的单实例可先做一次一致快照演练。

   ```bash
   python3 -m engine.deployment backup /var/lib/suikouji/accounting.db /var/backups/suikouji/v5-rehearsal-001.db
   ```

   ```bash
   python3 -m engine.deployment check /var/backups/suikouji/v5-rehearsal-001.db
   ```

2. 切到已校验的**新版本**源码或镜像，用显式 `migrate SOURCE DESTINATION` 从v5备份生成新的v6迁移副本。源文件只读、保持不变；目标必须不存在，不能传当前在线库路径。目标已存在时重复执行失败；需要重演练就选另一个新目标。迁移失败不能拿未校验文件切换；检查错误并保留源备份后重新演练。

   ```bash
   python3 -m engine.deployment migrate /var/backups/suikouji/v5-rehearsal-001.db /var/lib/suikouji/v6-rehearsal-001.db
   ```

   ```bash
   python3 -m engine.deployment check /var/lib/suikouji/v6-rehearsal-001.db
   ```

   Compose的一次性容器复用入口命令，使用新镜像时形式为 `docker compose --env-file /opt/suikouji-state/compose.env run --rm --no-deps api migrate /var/backups/suikouji/v5-rehearsal-001.db /var/lib/suikouji/v6-rehearsal-001.db`。迁移的是独立副本，不开启第二个业务写进程。检查原话与旧汇总未变、旧完整回执原样保留、新表及索引就绪，并在隔离测试库验随想写入/查询/重放；不能向真实账本写验收样例。

3. 演练通过后，在批准的停写窗口停止所有 API、Bot、CLI 等写入。仍使用旧版工具生成并校验**最终v5备份**，以同一已验证的新版本命令把这个最终备份迁移为全新的v6最终副本。不要直接拿早先演练副本上线，否则会丢掉演练后的写入。
4. 保留旧数据库和对应备份，在服务完全停止时将运行配置/数据文件切到已校验的v6最终副本；核对权限、owner、workspace、profile、timezone。不要对正在使用的 SQLite 文件做覆盖替换，也不要删除状态目录重新初始化。用新版本启动唯一实例，再验 `/readyz`、认证接口和只读 `verify_api.py`，确认 schema v6 与必需表、字段、索引就绪。
5. 失败时保持停写并保全v6文件，恢复**旧程序加匹配的最终v5备份**，使用旧版工具恢复到新路径并核对后切回；仅回滚镜像不够，旧程序不保证可读v6。若切换后已产生新随想或账目，先另行保全并人工制定后续处理，不声称旧备份回滚无损保留这些新数据。

真实环境的停写、备份、切换、恢复与远程操作需用户明确授权。迁移演练与离线测试不代表 Docker 挂载权限、真实 iLink 或公网已验收。

### 构建干净源码包

```bash
python3 tools/build_release.py
```

输出 `dist/suikouji-0.2.0-staging.tar.gz` 与 `.sha256`；已有输出拒绝覆盖，重建使用 `--output-dir` 指定新目录或 `--version` 指定新版本。归档严格按显式文件清单，含 per-file `SHA256SUMS` 和 `RELEASE.json`，不遍历 data/、.claude/、真实 .env 或凭据。Docker build context 也采用默认排除，只允许运行源码和必需网页。没有 GitHub 发布、镜像推送或服务器操作。

## 原生 Python 运行参考

## 安全边界

- 默认使用 `python3 -m engine.runtime`，只监听 `127.0.0.1`。
- 需要外部 Agent 访问时，优先使用 SSH tunnel、VPN 或私网反向代理；反向代理负责 TLS，API 仍必须带 Bearer key。
- 非回环 bind 没有 API key 会被拒绝；不要把未认证的旧 `/api/*` demo 路由暴露到公网。
- 生产 secrets 和 iLink 状态放在仓库外，文件权限为 `0600`；不要读取、提交或打印真实 token。
- SQLite 只支持一个 runtime 进程：一个 HTTPServer 加一个可选 Bot 线程。API 请求和 Bot 入账共用进程内锁串行访问；连接分别在所属线程创建、使用、关闭。长轮询、回复发送和退避不占数据库锁。不要另起 API、Bot、CLI、迁移或其他写库进程；该锁不能协调多进程。
- 不把 `data/*.db` 或 `.claude/launch.json` 放进镜像或当作生产配置。

## 启动

```bash
export SUIKOUJI_DB=/var/lib/suikouji/staging.db
export SUIKOUJI_BIND=127.0.0.1
export SUIKOUJI_PORT=8765
export SUIKOUJI_API_KEY='use-a-random-secret'
export SUIKOUJI_OWNER_ID=owner-1
export SUIKOUJI_PRINCIPAL_ID=agent-api
export SUIKOUJI_WORKSPACE_ID=shop-a
export SUIKOUJI_API_ONLY=1
python3 -m engine.runtime
```

启动前先在独立 staging 库中创建工作空间，再运行上面的 `python3 -m engine.runtime`。先导出配置中的环境变量，以下初始化命令只创建不存在的新文件；路径已存在时会拒绝，不修改原账本。它创建餐饮 profile 的工作空间，名称可自行更改：

```bash
python3 - <<'PY'
import os
from contextlib import closing
from pathlib import Path
from engine.store import SqliteStore

path = Path(os.environ['SUIKOUJI_DB'])
if not path.is_absolute():
    raise ValueError('staging 必须使用独立绝对路径')
workspace = os.environ['SUIKOUJI_WORKSPACE_ID']
owner = os.environ['SUIKOUJI_OWNER_ID']
if not workspace or not owner:
    raise ValueError('必须先配置 workspace 和 owner')
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
with closing(SqliteStore(str(path))) as store:
    store.ensure_shop(workspace, 'Staging 试账', owner_id=owner, profile_id='restaurant')
PY
```

此初始化只用于新库，直接创建v6。已有v5库按[副本迁移流程](#从-v5-升级到-v6)用旧版工具备份、新版 `python3 -m engine.deployment migrate SOURCE DESTINATION` 迁移副本，再停写、迁移最终副本并切换；不要用普通 CLI 打开真实旧库代替升级。readiness 不替旧库迁移。启动后通过 `/readyz` 验证 schema、必需表/字段/索引、完整性和 owner/workspace 配置。真实服务器执行需要用户授权。

## 备份与恢复

完整 SQLite 备份使用原生 backup API，不要把 `engine.cli export` 当成可恢复备份：

```bash
python3 -m engine.deployment backup /var/lib/suikouji/staging.db /var/backups/suikouji/staging-001.db
```

```bash
python3 -m engine.deployment check /var/backups/suikouji/staging-001.db
```

```bash
python3 -m engine.deployment restore /var/backups/suikouji/staging-001.db /var/lib/suikouji/rehearsal-001.db
```

这些命令使用发布入口的保护：备份和恢复都要求新目标，`check` 未通过时退出非零。每次选择新的文件名。先恢复到新路径并执行 `check`，再考虑切换服务。保留离线/异地加密备份，并定期演练恢复；本轮不替用户执行线上备份。

## iLink与Agent整理的接法

若外部 Grokbot/Agent 已接入iLink，推荐让它独占该Bot账号的消息接收和回复，先按 `/v1/capabilities` 的 `input_preparation` 整理，再向随口记提交 `text + structured`。此时保持本服务的三个iLink变量为空，只运行认证API。同账号不能再启动本服务worker并行拉取消息；详细的消息身份、固定请求重试与追问边界见[微信iLink入口](../docs/agent-capabilities.md#微信-ilink-入口)。

下述内置worker直接把文本送到受控规则入口，没有模型整理层。0.2.0-staging 有限支持明确“记个想法/记个随想”和“明天想试/下周想推新品”模式及两种账目/随想顺序，具体见[解析边界](../docs/parser-boundaries.md)；不提供自动欢迎、查询对话或多轮追问状态。它与外部Agent整理方案是两种接法，不能把结构化API上线描述成内置worker已经能理解任意口语。安装介绍须由外部Agent先核对 capabilities 和实际工作空间，再使用[冷启动约定](../docs/agent-capabilities.md#安装后介绍与冷启动)；不因无标签重复追问，partial 先说明已存及未入账部分。

## iLink worker 合同

`channel.bot.ILinkBotWorker` 通过 `AgentService` 写入，使用 `ilink:<account>:<peer>:<message_id>` 稳定幂等键。未知 peer、缺少消息 ID、非文本、非完成态和媒体消息均默认忽略；成功处理整批后才推进 `sync_buf`。发送失败不推进游标，故障恢复后可能重复投递，但不会重复入账。CSV 导入必须先预览，再把同一 CSV 和 `preview_token` 一起确认提交；token 有效期 1800 秒，成功后绑定一个业务 request_id，相同请求可继续重放原结果。预览后若出现同文件批次但本请求没有完整缓存，会要求核对并重新预览；新预览明确看到已有批次后，可确认重复文件，返回 duplicate 且不增加账目。

### 显式启用

三个 iLink 环境变量全部留空时只启动 API，运行时不会搜索账号文件、读取默认 `data/weixin-*.json` 或登录。启用时同时设置：

```bash
export SUIKOUJI_ILINK_ACCOUNT_PATH=/etc/suikouji/secrets/weixin-account.json
export SUIKOUJI_ILINK_STATE_PATH=/var/lib/suikouji/weixin-state.json
export SUIKOUJI_ILINK_ALLOWED_PEERS=peer-id-1,peer-id-2
```

`SUIKOUJI_WORKSPACE_ID`、owner 和 principal 必须有效；所有 peer 精确映射到该工作空间。allowlist 使用逗号分隔，拒绝空项、重复项和 ID 内的空白；没有通配符或跨工作空间路由。必须保持 `SUIKOUJI_API_ONLY=1`，布尔配置仅接受 `1/true/yes`、`0/false/no`（忽略大小写）。运行时只读取已导出的环境变量，不自动加载 `.env`。

启用 iLink 时，数据库、账号和状态必须使用仓库外的三个不同绝对路径，符号链接解析后也不得落入仓库；状态写入使用同目录内排他创建的随机临时文件（创建即 `0600`），同步文件并原子替换后同步目录，不跟随已有的固定 `.tmp` 链接。账号和状态目录由专用系统用户持有。已有状态必须属于同一 `account_id`；有内容但未绑定账号的旧状态应先人工核对，使用独立状态文件，不能直接复用游标。

账号文件沿用 `channel.ilink.login` 返回并由 `save_json` 保存的对象格式，只接受 `account_id`、`token`、可选 `base_url`、可选 `user_id`。`account_id` 和 `token` 必须为无空白的非空字符串；`base_url` 缺省为 `https://ilinkai.weixin.qq.com`，显式值必须为 HTTPS origin，不能包含用户信息、路径、查询或 fragment；`user_id` 为字符串。凭据由外部显式流程准备，运行时只读这个文件，不复制到状态文件，不打印 token。

### 启动、就绪和故障

Bot 在线程中先只读检查数据库 schema、完整性及 owner/workspace，再创建客户端、状态和 SQLite store；不为缺失数据库或工作空间自动初始化。初始化失败、状态损坏、账号不匹配会在 HTTP 服务启动前报错并清理资源。worker 初始化最多等待 10 秒。服务 bind、signal 注册或 `serve_forever` 失败也会停止并 join 已启动的 worker。

`/healthz` 保持进程存活语义。启用 iLink 后，`/readyz` 以及受就绪检查保护的 `/v1/*` 操作同时要求数据库正常、worker 线程存活、没有错误且最近 120 秒内成功轮询；第一批成功轮询前返回 503，空批次也算成功。worker 未就绪时返回 `ilink_not_ready`，错误响应不含 token 或通道原始异常。`/v1/capabilities` 沿用无需数据库的现有行为。

网络及限流错误按退避重试，期间未就绪；客户端传输超时产生的兼容空批次不计作成功轮询。下一次收到真实成功响应后恢复就绪。发送携带旧 context token 返回 `-14` 时，按协议仅允许一次不带 context token 的回退；回退成功继续工作，不视为账号过期。轮询返回 `-14`，或无 token 发送/回退仍返回 `-14`，以及配置错误或未知异常，都会停止 worker；进程继续提供存活探针但保持未就绪。运行时不扫码、不自动重新登录、不监听账号文件变化，也不静默降级为纯 API。人工准备好有效账号后，通过进程 supervisor 显式重启。

### 关闭期限

SIGINT/SIGTERM 先通知 worker 停止，再从辅助线程关闭 HTTP 循环。默认退避使用可中断的 event wait；长轮询不能强制取消，但其返回后不再开始处理该批消息。每条消息边界检查停止，不再启动下一条；停止前已开始的入账/回复会完成，未完成整批不推进游标。

worker 的 `close()` 先 stop 再 join，默认期限为 45 秒：35 秒长轮询加 urllib 的 5 秒余量，再留 5 秒清理；正常回复请求使用 15 秒超时加 5 秒余量。Bot store 仅在 worker 的 finally 中关闭。超过 join 期限会明确报错，由进程 supervisor 强制终止，绝不从主线程强关 SQLite。HTTP 请求另有 10 秒 socket 空闲超时，等待当前请求结束和数据库锁也可能增加总退出时间；urllib/socket 超时不是对持续缓慢传输的总时限保证。建议 supervisor 设置 70 秒的进程退出总期限（例如 systemd 的 `TimeoutStopSec=70s`）。

不支持媒体导入或群聊；真实联调必须使用独立 Bot、独立数据库和明确授权。离线回归只使用临时假账号和假客户端：

```bash
python3 -m unittest discover -s tests -p 'test_runtime_lifecycle.py' -v
```
