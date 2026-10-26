# 随口记经营助手

独立的本地经营记录工具：保存逐字原话，分别记录收支、销量、经营观测与生意随想，支持随想按日期、标签或关键词回看，以及既有账目修正、CSV导入和标准配方成本估算。随想不计入财务，不生成任务或提醒。

## 交给 bot 自动部署

下载0.2.0-staging干净源码发布包并解压后，让部署 bot 先阅读 [自动部署说明](deploy/BOT_DEPLOY.md)。包内提供 Dockerfile、Docker Compose、状态目录与密钥准备工具；默认只启动带认证的 Agent API，端口仅发布到服务器本机。首次启动直接创建schema v6新账本，API、探针和容器重启不会自动迁移已有库。

发布包不包含本机 `data/`、真实密钥、微信账号或开发配置。数据与密钥保存到源码外的持久化目录；详细操作及验收边界见 [部署说明](deploy/README.md)。本包不含预构建镜像；GitHub发布状态应以实际交付为准。本机没有 Docker，容器构建和运行需在获授权的 Docker 目标环境验收，真实iLink与公网未实际验收。

Agent 连接采用 Bearer 认证 HTTP API，契约见 [OpenAPI](docs/openapi.yaml)。支持 HTTP 工具调用的 bot 可由执行层注入密钥来访问；仅支持 MCP 的客户端还需要额外适配，本发布包不提供 MCP 服务。部署模式关闭本地 demo 首页，访问 `/` 返回 404 是预期行为。

0.2.0-staging 推荐调用方 Agent 先整理混合口语，在同一次 `/v1/captures` 提交逐字原话和 `structured`。旧四个数组仍必填，可选 `thoughts` 用于随想，四类领域投影合计最多50行。工具验证来源及字段，并将原话、各投影、标签与完整回执原子写入；不重复解析原话。纯随想和无标签均可保存，仍限餐饮profile，不能自动将其他行业选为餐饮。详细字段、混合示例和安装后介绍见[Agent调用约定](docs/agent-capabilities.md)。

例如“今天营业额1300元，阴天。记个想法：明天试南瓜甜品 #新品”可分别保存营业额、天气与随想。随想默认按首次收到消息的工作空间时区日期归档，正文“明天”“下周”“昨天”不改变归档日，也不产生提醒。原生文本入口只支持有限明确标记与句首模式，见[解析边界](docs/parser-boundaries.md)，不内置模型或查询对话；外部 Agent 负责更自由的口语整理及查询。

`GET /v1/thoughts` 返回独立随想，支持记录日闭区间、重复标签AND、标题/正文关键词和稳定游标分页。`GET /v1/timeline?types=thought` 仍返回含随想的原话条目，混合条目保留账目预览；完整随想也在原话详情中。HTTP 200、随想已存和天气已存都不代表金额已入账；partial先说明已存内容，再说明未入账片段。“大概做了1300”不能成为精确1300账目。未知结果按同号和原完整请求重试，禁止换号重发整段；历史缓存原样重放，可能没有 `thoughts`，不会自动补账。

已有v5库先用**旧版本工具备份**，再用新版 `python3 -m engine.deployment migrate SOURCE DESTINATION` 生成v6新副本（目标须不存在，源库不动）；演练通过后停写，迁移最终副本并切换，再验readiness。失败需恢复旧程序加匹配的旧备份，不能仅回滚镜像。不要用新版普通CLI打开真实旧库代替升级，完整步骤见[升级说明](deploy/README.md#从-v5-升级到-v6)。真实环境的部署、迁移、停写和切换需用户授权。

## 启动本地试用版

在项目根目录运行：

```bash
python3 -m engine.web --port 8766 --db data/accounting-demo.db
```

打开 http://127.0.0.1:8766 。页面由 `web/demo.html` 提供，使用 Python／SQLite；服务仅监听本机。仅不存在的账本路径会在启动前初始化 v6 新库，已有旧库不会自动升级，页面数据接口返回503并要求先按升级说明迁移副本。

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

原话存在 `record`；收入／采购／费用存在 `entry`；商品销量存在 `sale`；经营观测存在 `observation`；随想存在独立 `thoughts` 表，规范化标签在可索引的 `thought_tags` 关联表中。随想经 `capture_id` 关联来源，不塞入旧账目引用。财务核算以业务账表为准，不累计派生观测或随想。修正写回原业务行，另存依据和前后值，不覆盖最初原话；首版不提供随想编辑、删除或撤销。

JSON export包含随想与标签，但不是完整数据库备份，也不承诺JSON restore。完整恢复继续使用[SQLite backup/restore](deploy/README.md#备份与恢复)，保留原话、财务、随想、标签及控制表和完整回执；每次恢复到新文件，校验和演练后再考虑切换。

当前基础架构保留旧 `shop_id` 作为兼容的工作空间标识，并在 `workspace_meta` 中记录 owner、行业 profile 和时区；原话增加 context、intent、routing_status、profile_id 元数据，但不改变旧账务语义。行业 profile 注册表见 `engine/profile.py`。外部 Agent 使用 Bearer 认证的 `/v1/capabilities`、`/v1/workspaces` 与 capture、修正、导入和查询接口，合同见 [OpenAPI](docs/openapi.yaml) 与 [Agent 能力契约](docs/agent-capabilities.md)。Agent 必须先读取能力契约：不支持的需求要主动说明并追问，信息不足时不得猜着入账。本地 demo 继续保留 `/api/*` 兼容接口；[staging 运行方式](deploy/README.md) 默认只开放 v1 与探针。

当前阶段仍未实现个人微信直连、任务提醒、客如云/外卖平台直连或独立的个人复式账本；这些只能在能力契约中标为规划中，不能对用户伪装成已支持。

## 使用与核算约定

- [使用及核算规则](docs/accounting-v1.md)
- [补充、修正与待核对](docs/review-and-correction.md)
- [连续口语解析边界](docs/parser-boundaries.md)
- [生意随想首版验收记录](docs/thoughts-release-verification.md)
- [维护经验与失败证据](docs/engineering/lessons.md)
- [微信协议参考](docs/ilink-protocol-notes.md)

采购付款不等于耗料成本；营业总额与销售明细不重复相加；付款日与费用归属月分开；标准配方成本不等于实际成本或利润。当前没有完整库存、应收应付、退款、多端同步；iLink Bot 入口已实现离线消息处理，真实扫码、真实消息格式和服务器联调尚未验收。

## 回归验证

```bash
python3 -m unittest discover -s tests
```

离线回归覆盖既有业务、工作空间/profile、Agent HTTP、iLink fake client、进程崩溃与完整响应重放、导入预览有效期、随想校验与分页、有限原生随想、v5副本迁移及SQLite备份恢复。旧四数组请求和原生partial缓存使用固定合成v5 fixture验证，不读取真实账本。测试只使用内存或临时库；最终测试数和结果以该次发布验收为准，不代表任意自然语言无缺陷，也不等于真实 iLink、Docker、平台直连或公网部署已经可用。

## 文件精简

2026-10-24按用户要求精简重复的阶段进度／结果报告、过时设计调研稿和 `.DS_Store`，以本文件作为当前入口。保留运行代码、账本、启动配置、测试、当前使用规则和维护证据。删除前将7份报告合并为一个压缩恢复包 `cleanup-recovery-2026-10-24.zip`，内含文件清单、大小与SHA-256，校验后删除原件；不复制整个项目。文件精简未改业务行为，也未重新执行整套业务测试。
