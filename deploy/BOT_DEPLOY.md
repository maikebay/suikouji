# 交给部署 bot 的执行说明

任务：在用户已明确授权的服务器上部署随口记0.2.0-staging源码发布包，启动独立、带认证的经营记录 API。新库直接使用schema v6（含独立随想和标签），已有v5库需要显式副本迁移。当前交付仅是本地发布包，不代表已获服务器操作授权。若没有目标主机访问权限，先向用户确认，不能自行选择服务器。

## 交付边界

- 先阅读本文件、[Compose 操作说明](README.md#docker-compose-发布包)和[Agent 能力契约](../docs/agent-capabilities.md)。当前使用 Bearer 认证 HTTP API；本包没有 MCP 服务或 Grok 专用插件。
- 发布包不含数据库和密钥；不得寻找或上传用户本机旧账本、微信账号或任何其他项目的配置。
- 新安装只用新的源码目录和新的仓外状态目录。升级则保留已有状态、密钥和旧程序，按专门迁移流程操作；不重跑准备脚本覆盖旧状态，不删除、不轮换密钥、不运行清空卷命令。已有数据库不能自动迁移；不能用新版本普通 CLI 直接打开真实旧库当作推荐升级步骤。
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

## 已有 v5 部署的升级

新安装按前面的全新状态目录步骤执行，直接初始化v6；已有v5部署按[完整副本迁移说明](README.md#从-v5-升级到-v6)操作，不重新初始化原库。

1. 保存旧程序/镜像与配置，用**旧版** `python3 -m engine.deployment backup SOURCE BACKUP` 生成新v5备份，并以旧版 `check` 校验。
2. 用**新版** `python3 -m engine.deployment migrate SOURCE DESTINATION` 从v5备份生成v6迁移副本；目标必须不存在，源文件不动。以新版 `check` 验证副本，并在隔离环境核对旧原话、财务汇总、旧四数组/原生partial完整回执、新表/索引和随想能力。已有目标不能原地重复迁移，失败后不得切换未通过检查的文件。
3. 演练通过后，按用户批准的停写窗口停止全部业务写入；旧版工具备份最新v5状态，再用相同步骤生成并校验全新的v6最终副本。停止服务后切换到该副本，保留原库与旧备份；不能用过时的演练副本覆盖后续写入。
4. 新版唯一实例启动后检查 `/readyz`、认证接口和 `verify_api.py`。API、健康探针和容器启动均不执行隐式迁移。
5. 失败时保全v6数据并停止写入，恢复**旧程序加匹配的最终v5备份**；只回滚镜像不足以回滚数据库。上线后新增随想或账目须另行保全处理，不能宣称旧备份回滚会保留这些新记录。

迁移、停写、切换和恢复均需真实环境授权；本文不是线上执行记录。完整SQLite备份/恢复覆盖随想、标签和控制表。JSON export含随想及标签但不是完整数据库备份，不承诺JSON restore；继续保留唯一新目标、校验、异地备份和定期恢复演练标准。

## 0.2.0-staging 的隔离验收

在全新解压目录执行离线回归，若后续发布GitHub则还需在相应commit的干净checkout复验。所有样例只用临时/独立测试账本，不向运行中的真实账本写测试记录：

```bash
python3 -m unittest discover -s tests
```

- 按[混合原话示例](../docs/agent-capabilities.md#混合原话的调用示例)验证收入1300、阴天天气、随想和标签同次写入，随想不增加金额；纯随想和无标签可成功。
- 验证“大概做了1300”“花了两百多”“可能收入一千左右”不精确入账，明确随想可先保存；旧“今天的营业额很差，只有1300”仍只记一笔1300。
- 验证原生有限随想模式、账目在随想前后两种顺序、正文预算不泄漏进财务、工作空间时区归档和跨午夜同号重试。
- 验证独立随想列表日期、AND标签、关键词、分页/非法游标与隔离；timeline仍按原话分页且预览有上限。
- 验证旧v5副本显式迁移、旧完整缓存原样重放、新同号请求不双写、同号改随想冲突；历史回执可能没有 `thoughts`，不能补字段或重解析。

更新后的 Agent 重新读取 `/v1/capabilities`，在同一次capture保留逐字 `text` 并提交 `structured`。新回执顶层 `thoughts` 与结构化 `normalization.thoughts` 表示随想保存，HTTP 200 和随想成功均不能代替 `entries/sales` 的入账证据。partial先说明已存、再说明未入账并追问；未知网络结果同号同请求重试，禁止换号重发整段原话补历史记录。

## iLink接入与用户可读回复

外部Grokbot已接入iLink时，由它独占消息接收、整理和回复，按[微信入口约定](../docs/agent-capabilities.md#微信-ilink-入口)调用结构化API，本服务内置worker保持关闭。直接用内置worker时仍为规则解析，只支持[明确随想模式](../docs/parser-boundaries.md#有限随想识别)，没有模型整理、自动欢迎、查询对话或多轮确认；不要在安装报告中声称支持任意口语理解。

认证和已有工作空间核对完成、capabilities显示能力available后，外部Agent可按[安装介绍](../docs/agent-capabilities.md#安装后介绍与冷启动)简短说明记账和随想查询。已配置的店名/profile不重复问，不询问常用标签表；无标签照常保存。其他行业仍受现有profile边界限制，不自动替用户选餐饮，也不承诺库存、剩料、提醒、撤销、任意改名或固定报表。

调用方同时遵循 `agent_policy.user_presentation`：告诉用户正在核对哪本账、哪个时间范围的营业收入、进货、费用和商品销量；不要把接口名、None/null或服务器路径贴作进度和报表。微信优先简短纯文本，金额和数据缺口用中文说明；完整展示规则见[面向用户的进度和报表](../docs/agent-capabilities.md#面向用户的进度和报表)。本包没有新增固定模板报表生成接口。

## 验收报告

报告源码版本、校验结果、镜像构建结果、容器 health、回环访问地址、工作空间 ID、状态/密钥文件路径（不含内容）、备份恢复演练结果、未验收项。部署失败保留数据并说明原因；不要通过删除状态目录、重新初始化或关闭认证掩盖问题。

服务器成功运行与 Agent 已获得记账工具分别验收。调用方需在自身执行层注入密钥、先读 `GET /v1/capabilities`，再按 [OpenAPI](../docs/openapi.yaml)调用。写入编号稳定重用，HTTP 200 的待确认/未解析状态不等于已入账。公网、真实 iLink 消息及扫码未经过本包离线验证。

当前发布环境没有 Docker/Podman；本包只经过标准库离线测试和静态 YAML 校验，容器实际构建、权限挂载和网络必须在目标环境验证。禁止把此说明当作已经部署成功的证据。
