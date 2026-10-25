# Agent 能力契约

Agent 在处理用户原话前，先带 Bearer key 读取 `GET /v1/capabilities`，了解产品当前支持、规划中和明确不支持的能力。接口合同见 [OpenAPI](openapi.yaml)。本地 demo 的 `/api/capabilities` 继续兼容；它不提供 staging 接入所需的认证边界。

## 必须遵守

- 能力状态为 `available` 才能直接使用；`planned` 只能向用户说明尚未实现，不能伪装成已执行；`unsupported` 必须明确告知边界。
- 工作空间、行业 profile、实际/计划/预算、日期、金额归属和金额口径不明确时，先追问。
- “新品售价30元”“预计花300元”“以后可能采购100斤”不能仅凭数字直接记成正式账。
- 个人微信、客如云和外卖平台直连当前都不能承诺已支持。
- 标准成本只能标为估算，不得称为实际耗料成本或实际利润。

## 当前基础

当前实现保留 `record` 原话、`entry`/`sale`/`observation` 投影和旧 `shop_id` 兼容；profile 作为工作空间元数据和模块边界。REST 和 iLink Bot 共用 `AgentService`，工作空间必须属于服务端配置的 owner。

## 写入与重试

- `request_id` 是业务幂等键，限定在 principal、workspace 和 operation 下；同号不同内容返回 `409 idempotency_conflict`。服务端生成的 `X-Request-ID` 只作请求追踪，不能代替业务编号。
- capture、修正和导入的业务写入与完整响应在同一事务提交。相同请求重试保留原响应的内容，只把顶层 `duplicate` 设为 `true`；导入内层的 `imported` 仍表示原请求导入的行数。工作空间仍须属于当前 owner；行业 profile 后续变更或修正目标后续删除，不阻止已完成请求重放，新的写入仍校验当前 profile 和目标归属。
- `/healthz` 不访问数据库；`/readyz` 只读检查完整性、版本、必需表与字段，以及配置的工作空间和 worker 心跳。未初始化或缺少控制表的数据库返回 503，API 请求不会替它建表。升级/初始化需在服务停止后显式执行。
- `pending` / `parsing` 不能视为完成，返回 `409 request_in_progress` 后应使用原业务编号稍后重试。`rejected` 表示原话已保留、没有写入账目，修改原话后需要新编号。
- 旧版本如果留下“业务已提交、响应未保存”的请求，无法保证重建首次完整响应；对可识别的旧原话、修正和导入返回 `409 recovery_needs_review`，保留数据供人工核对，不重新写账或静默缓存缩减结果。即使旧认领已释放，也检查业务证据；已完成的旧请求仍重放完整缓存。仅认领后退出、尚未写入业务的旧请求可在租期过后重试。新原话和修正使用无歧义的 principal/request 编码，避免含冒号的不同组合共享业务身份。
- CSV 先调用 `/v1/imports/preview` 展示精确预览，再明确确认并调用 commit。预览绑定 principal、workspace 和 CSV 的 SHA-256，有效期 1800 秒，成功提交时绑定一个 request_id。过期或消费后的 token 不能用于新的提交；原提交的相同请求仍可重放缓存响应。预览同时记录当时已有的同文件批次，并用 `duplicate_file` 和 `existing_batch`（编号、文件名、行数）展示，Agent 应向用户说明：已有批次可确认重复导入且不增加账目；预览之后才出现批次、但本请求没有完整响应时，返回 `409 recovery_needs_review`，核对后需重新预览。旧预览没有批次快照且文件已导入时也适用这一规则。
- 信息不足或能力不支持时先追问。Bot 当前只做单聊文本 capture；任务提醒、平台直连、普通个人微信和媒体处理不能被当成已完成能力。
