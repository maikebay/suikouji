# 随口记经营助手

独立的本地经营记录工具：保存原话，提取收支、销量与经营信息，支持原行补充修正、CSV导入及标准配方成本估算。

## 交给 bot 自动部署

下载干净源码发布包并解压后，让部署 bot 先阅读 [自动部署说明](deploy/BOT_DEPLOY.md)。包内提供 Dockerfile、Docker Compose、状态目录与密钥准备工具；默认只启动带认证的 Agent API，端口仅发布到服务器本机。首次启动创建新账本，重启不会自动迁移已有库。

发布包不包含本机 `data/`、真实密钥、微信账号或开发配置。数据与密钥保存到源码外的持久化目录；详细操作及验收边界见 [部署说明](deploy/README.md)。当前没有已发布的 GitHub 地址或预构建镜像；本机没有 Docker，容器构建和运行需在有 Docker 的目标环境验收。

Agent 连接采用 Bearer 认证 HTTP API，契约见 [OpenAPI](docs/openapi.yaml)。支持 HTTP 工具调用的 bot 可由执行层注入密钥来访问；仅支持 MCP 的客户端还需要额外适配，本发布包不提供 MCP 服务。部署模式关闭本地 demo 首页，访问 `/` 返回 404 是预期行为。

0.1.1-staging 推荐由调用方 Agent 先整理混合口语，再在同一次 `/v1/captures` 请求中提交逐字原话和 `structured` 数据。能力接口直接提供字段结构与有效示例，并声明中文业务进度、报表金额和数据缺口的展示要求。工具校验账目、销量、非金额观测及每条来源片段，原子写入，不再重复运行原话解析。旧文本入口继续兼容，并修复“今天的营业额很差，只有1300”跨逗号漏记。`partial` 可以包含已成功入账的金额，HTTP 200 本身不能作为入账凭据。详细字段、示例与历史记录边界见[Agent调用约定](docs/agent-capabilities.md)。

## 启动本地试用版

在项目根目录运行：

```bash
python3 -m engine.web --port 8766 --db data/accounting-demo.db
```

打开 http://127.0.0.1:8766 。页面由 `web/demo.html` 提供，使用 Python／SQLite；服务仅监听本机。

CLI帮助：

```bash
python3 -m engine.cli --help
```

CLI与网页必须指定同一个数据库和店铺ID才会使用同一本账；网页的店铺ID固定为 `web-demo`。

## 数据与文件

- `engine/`、`web/`：新版业务逻辑、接口和网页。
- `channel/`：腾讯 iLink Bot 客户端与文本记账 worker，通过共享 AgentService 写入；协议 decoder 目前只经过离线 synthetic fixture 验证。
- `.claude/launch.json`：本地预览启动配置。
- `data/accounting-demo.db`：新版试记账本。
- `data/web-demo.db`、`data/demo.db`：已有独立账本，不自动同步，不应作为缓存删除。
- `data/accounting-migration.json`：既有账本复制与恢复信息。
- 根目录 `index.html`、`app.js`、`parser.js`、`store.js`、`styles.css`：旧版浏览器localStorage原型，与新版账本分离，暂保留以便访问旧版。
- `tests/`：回归测试和历史schema fixture，用于维护与升级验证，不是可重建缓存。

原话存在 `record`；收入／采购／费用存在 `entry`；商品销量存在 `sale`；经营观测存在 `observation`。财务核算以业务账表为准，不再累计派生观测。修正写回原业务行，另存依据和前后值，不覆盖最初原话。

当前基础架构保留旧 `shop_id` 作为兼容的工作空间标识，并在 `workspace_meta` 中记录 owner、行业 profile 和时区；原话增加 context、intent、routing_status、profile_id 元数据，但不改变旧账务语义。行业 profile 注册表见 `engine/profile.py`。外部 Agent 使用 Bearer 认证的 `/v1/capabilities`、`/v1/workspaces` 与 capture、修正、导入和查询接口，合同见 [OpenAPI](docs/openapi.yaml) 与 [Agent 能力契约](docs/agent-capabilities.md)。Agent 必须先读取能力契约：不支持的需求要主动说明并追问，信息不足时不得猜着入账。本地 demo 继续保留 `/api/*` 兼容接口；[staging 运行方式](deploy/README.md) 默认只开放 v1 与探针。

当前阶段仍未实现个人微信直连、任务提醒、客如云/外卖平台直连或独立的个人复式账本；这些只能在能力契约中标为规划中，不能对用户伪装成已支持。

## 使用与核算约定

- [使用及核算规则](docs/accounting-v1.md)
- [补充、修正与待核对](docs/review-and-correction.md)
- [连续口语解析边界](docs/parser-boundaries.md)
- [维护经验与失败证据](docs/engineering/lessons.md)
- [微信协议参考](docs/ilink-protocol-notes.md)

采购付款不等于耗料成本；营业总额与销售明细不重复相加；付款日与费用归属月分开；标准配方成本不等于实际成本或利润。当前没有完整库存、应收应付、退款、多端同步；iLink Bot 入口已实现离线消息处理，真实扫码、真实消息格式和服务器联调尚未验收。

## 回归验证

```bash
python3 -m unittest discover -s tests
```

离线回归覆盖既有业务、工作空间/profile、Agent HTTP、iLink fake client、进程崩溃与完整响应重放、导入预览有效期及备份恢复。测试只使用内存或临时库；通过不代表任意自然语言无缺陷，也不等于真实 iLink、平台直连或公网部署已经可用。

## 文件精简

2026-10-24按用户要求精简重复的阶段进度／结果报告、过时设计调研稿和 `.DS_Store`，以本文件作为当前入口。保留运行代码、账本、启动配置、测试、当前使用规则和维护证据。删除前将7份报告合并为一个压缩恢复包 `cleanup-recovery-2026-10-24.zip`，内含文件清单、大小与SHA-256，校验后删除原件；不复制整个项目。文件精简未改业务行为，也未重新执行整套业务测试。
