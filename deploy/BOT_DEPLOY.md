# 交给部署 bot 的执行说明

任务：在用户已明确授权的服务器上部署随口记源码发布包，启动独立、带认证的记账 API。当前交付仅是本地发布包，不代表已获服务器操作授权。若没有目标主机访问权限，先向用户确认，不能自行选择服务器。

## 交付边界

- 先阅读本文件、[Compose 操作说明](README.md#docker-compose-发布包)和[Agent 能力契约](../docs/agent-capabilities.md)。当前使用 Bearer 认证 HTTP API；本包没有 MCP 服务或 Grok 专用插件。
- 发布包不含数据库和密钥；不得寻找或上传用户本机旧账本、微信账号或任何其他项目的配置。
- 只用新的源码目录和新的仓外状态目录。目录已存在时停止核对，不覆盖、不删除、不轮换密钥，不运行清空卷命令。已有数据库不能自动迁移。
- 默认 API_ONLY、单实例、宿主机回环端口、iLink 关闭。不要自行开启公网监听、扫码、iLink 或任务调度。需要远程访问由用户另行授权私网/VPN/TLS 反向代理。
- 密钥由准备脚本在目标机生成，保存在仓外 0600 文件中。不得输出、粘贴、上传密钥或把它加入模型提示词。报告密钥文件路径即可。
- 不安装或升级目标机 Docker、不重启其他服务，除非用户授权。缺前提时报告缺项。

## 执行步骤

1. 核对独立目标目录、端口和已有服务；需要 Python 3.10+、Docker Engine 和 Docker Compose v2.20+（含 `up --wait`）。以目标机器的实际用户运行，不要用 sudo 混合切换不同 Docker 上下文。准备脚本若由 root 执行会为容器使用 UID/GID 10001，普通用户执行则使用其 UID/GID。
2. 在解压前用同目录 `.sha256` 文件核对源码包；解压到新的空目录，在 `suikouji-版本/` 根目录运行 `sha256sum -c SHA256SUMS`。不要在已有部署上覆盖解压。
3. 如 shell 已设置 `SUIKOUJI_*`，先确认并清理冲突的旧变量：Compose 的宿主环境变量优先于 `--env-file`。选择新的状态目录；下列示例的 `/opt` 父目录需有写权限，普通用户可用其拥有的绝对目录。

```bash
python3 tools/prepare_deploy.py --state-dir /opt/suikouji-state --owner-id owner-1 --workspace-id shop-a --workspace-name 我的账本 --profile restaurant --timezone Asia/Shanghai
```

4. 只校验配置，不打印配置中的真实密钥；生成的 compose.env 本身只存路径与非秘密选项。

```bash
docker compose --env-file /opt/suikouji-state/compose.env config --quiet
```

5. 构建镜像。需要下载 Python 基础镜像及 Debian 时区/证书包；没有预构建镜像，不要假定离线服务器能直接构建。

```bash
docker compose --env-file /opt/suikouji-state/compose.env build
```

6. 启动唯一实例并等待 healthcheck。入口自动创建不存在的新数据库；已有库只读核对 schema、owner、workspace、profile、timezone，配置不符直接失败。

```bash
docker compose --env-file /opt/suikouji-state/compose.env up -d --wait --wait-timeout 90
```

7. 只读验证存活、就绪、认证能力接口和工作空间；该工具不写测试账目、不输出密钥，须由有权读取准备配置和密钥的用户执行。

```bash
python3 tools/verify_api.py --config /opt/suikouji-state/compose.env
```

8. 按[备份恢复说明](README.md#compose-备份与恢复)生成唯一的新备份、校验，并恢复到另一个新文件演练。不得替换当前运行数据库。检查 Compose health 为 healthy，根路径 `/` 返回 404 属于 API_ONLY 的正常行为，不是安装失败。

## 0.1.1-staging 的营业额回归

此版本新增 Agent 先整理口语、保留原话和结构化结果一次提交的调用路径；同时修复旧文本入口“今天的营业额很差，只有1300”跨逗号漏记的问题。发布到 GitHub 后，必须从该 commit 重新 clone 到全新目录执行回归，不能只在最初上传目录测试：

```bash
python3 -m unittest discover -s tests
```

在独立测试账本中提交“今天是周末，但是天气不好，阴天，天气也越来越冷了。外面还有跑马拉松的，可能影响生意。今天的营业额很差，只有1300。”，预期一笔日营业额1300、天气观测、逐字原话；未结构化叙述仍使结果保持 `partial`。同一 `request_id` 重试应返回原结果且不新增账目。此业务写入验收只能用独立测试账本，不向用户运行中的账本补发该句。

更新后的 Agent 应重新读取 `/v1/capabilities`，先遵循 `agent_policy.input_preparation` 整理用户口语，在同一 capture 中保留 `text` 并提交 `structured`；按[结构化调用示例](../docs/agent-capabilities.md#截图原话的调用示例)在独立测试库验证，不能只更新服务却仍让 Agent 改写原话交给旧解析器。再遵循 `agent_policy.capture_result_policy`：HTTP 200 不代表金额已入账；按 `entries` / `sales` 和 `parsed.leftovers` 分别报告。已完成的旧请求会继续重放旧响应，更新不会自动补记历史1300；不要换请求号重发整段原话处理历史漏记。历史核对和补录需单独安排，见[Agent 能力契约](../docs/agent-capabilities.md#写入结果应怎样向用户说明)。

## iLink接入与用户可读回复

外部Grokbot已接入iLink时，由它独占消息接收、整理和回复，按[微信入口约定](../docs/agent-capabilities.md#微信-ilink-入口)调用结构化API，本服务内置worker保持关闭。直接用内置worker时仍为规则解析，没有模型整理层；不要在安装报告中声称已经支持任意口语理解。

调用方同时遵循 `agent_policy.user_presentation`：告诉用户正在核对哪本账、哪个时间范围的营业收入、进货、费用和商品销量；不要把接口名、None/null或服务器路径贴作进度和报表。微信优先简短纯文本，金额和数据缺口用中文说明；完整展示规则见[面向用户的进度和报表](../docs/agent-capabilities.md#面向用户的进度和报表)。本包没有新增固定模板报表生成接口。

## 验收报告

报告源码版本、校验结果、镜像构建结果、容器 health、回环访问地址、工作空间 ID、状态/密钥文件路径（不含内容）、备份恢复演练结果、未验收项。部署失败保留数据并说明原因；不要通过删除状态目录、重新初始化或关闭认证掩盖问题。

服务器成功运行与 Agent 已获得记账工具分别验收。调用方需在自身执行层注入密钥、先读 `GET /v1/capabilities`，再按 [OpenAPI](../docs/openapi.yaml)调用。写入编号稳定重用，HTTP 200 的待确认/未解析状态不等于已入账。公网、真实 iLink 消息及扫码未经过本包离线验证。

当前发布环境没有 Docker/Podman；本包只经过标准库离线测试和静态 YAML 校验，容器实际构建、权限挂载和网络必须在目标环境验证。禁止把此说明当作已经部署成功的证据。
