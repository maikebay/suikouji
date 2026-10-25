"""给外部 Agent 的产品能力契约。

这不是业务写入接口，而是 Agent 在决定如何回应用户前应先读取的能力说明。
能力状态必须如实反映当前实现；未实现的能力不能因为存在一个相似字段就被猜测成已支持。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .profile import list_profiles


@dataclass(frozen=True)
class Capability:
    id: str
    status: str
    description: str
    input_shape: str
    confirmation: str
    limitations: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


_CAPABILITIES = (
    Capability(
        "capture.text",
        "available",
        "保存用户原话，并记录来源、时间、去重编号和解析状态",
        "text + source + dedup_ref",
        "不需要；原话先保存",
        ("网页、CLI、认证 Agent API 与 iLink Bot 文本 worker 共用业务入口；iLink 仅通过离线样本验证，真实扫码及消息格式尚未验收",),
    ),
    Capability(
        "capture.structured",
        "available",
        "Agent 先整理混合口语，提交逐字原话及结构化账目、销量和非金额观测；工具校验并原子写入",
        "text + structured(entries, sales, observations, leftovers) + request_id",
        "事实明确可提取；真正缺失或冲突的金额归属、日期、工作空间和金额口径必须先询问",
        ("每条结构化事实必须带原文中的 source_quote 和 assertion=actual",
         "原话不可改写；不把计划、推测、因果猜测写成实际账目",
         "服务端校验字段与来源片段，不能证明 Agent 的语义判断一定正确",
         "当前只允许餐饮 profile；不额外解析原话生成第二套账目"),
    ),
    Capability(
        "ledger.restaurant",
        "available",
        "在餐饮 profile 下提取收入、采购、费用、销量和经营观测",
        "自然语言原话或结构化账目",
        "金额归属、日期、金额口径不明确时必须先询问",
        ("当前解析器是受控规则，不是任意自然语言理解", "账表是金额权威，观测不能回加到账务"),
    ),
    Capability(
        "ledger.correction",
        "available",
        "补充或修正已有账目，并保留修正原话、依据和前后值",
        "target + changed_fields + reason",
        "必须确认目标行和修正含义",
        ("修正写回原业务行，不新增重复账目",),
    ),
    Capability(
        "import.csv",
        "available",
        "预览并导入销售或费用 CSV，保留来源批次和去重身份",
        "CSV text + mapping",
        "提交前必须展示预览和重复/冲突结果",
        ("当前没有客如云、美团或饿了么直连连接器",),
    ),
    Capability(
        "cost.standard",
        "available",
        "按配方、采购单价和生效日期估算标准原料成本",
        "recipe + purchase price + as_of",
        "缺单位、价格或配方时必须询问或标记待补",
        ("标准成本不是实际耗料成本，也不是实际利润",),
    ),
    Capability(
        "workspace.profile",
        "available",
        "为个人事业建立多个相互隔离的工作空间，并为每个空间选择行业 profile",
        "workspace_id + profile_id",
        "首次选择或切换 profile 时必须让用户确认",
        ("当前仍兼容旧 shop_id；尚未提供多用户权限模型",),
    ),
    Capability(
        "task.reminder",
        "planned",
        "任务、截止时间、重复规则和通知送达",
        "title + due_at + timezone + recurrence",
        "必须询问时间、时区和提醒渠道",
        ("当前没有 Task 表、scheduler 或通知渠道",),
    ),
    Capability(
        "platform.connectors",
        "planned",
        "从客如云或外卖平台自动拉取并对账",
        "connector account + import policy",
        "必须先确认来源账号、金额口径和导入范围",
        ("当前只有通用 CSV 导入",),
    ),
    Capability(
        "wechat.personal",
        "unsupported",
        "普通个人微信自动读写和历史消息同步",
        "personal WeChat account",
        "不能承诺或静默替代",
        ("当前 iLink 代码面向 Bot 身份，不是普通个人微信",),
    ),
)


def _structured_schema() -> dict:
    text = {"type": "string", "minLength": 1, "maxLength": 100}
    number = {"type": "number", "minimum": 0, "maximum": 1_000_000_000_000}
    common = {
        "date": {"type": "string", "format": "date", "description": "严格 YYYY-MM-DD，不猜日期"},
        "source_quote": {"type": "string", "minLength": 1, "maxLength": 2000,
                         "description": "逐字原话中的连续片段"},
        "assertion": {"type": "string", "enum": ["actual"]},
    }
    financial = {
        "amount_basis": {"type": "string", "enum": ["gross", "settlement"]},
        "granularity": {"type": "string", "enum": ["daily", "transaction"]},
    }

    def row(required, properties, description):
        return {"type": "object", "additionalProperties": False,
                "required": required, "properties": properties, "description": description}

    entry = row(
        ["date", "type", "category", "amount", "amount_basis", "granularity", "source_quote", "assertion"],
        {**common, **financial, "type": {"type": "string", "enum": ["revenue", "purchase", "expense"]},
         "category": {"type": "string", "enum": ["营业额", "食材成本", "房租", "人工", "水电", "平台佣金",
                                                   "包装耗材", "推广", "维修", "税费", "其他支出"]},
         "amount": number, "item": text, "qty": number, "unit": text,
         "period_month": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}$"},
         "platform": text, "channel": text},
        "收入分类营业额，采购分类食材成本，其余分类限费用。采购qty/unit成对提供，未知均省略；"
        "period_month仅用于费用归属月。gross为流水总额，settlement为结算实收，不能混加。")
    sale = row(
        ["date", "product_name", "qty", "amount", "unit", "amount_basis", "granularity", "source_quote", "assertion"],
        {**common, **financial, "product_name": text, "qty": number,
         "amount": {"anyOf": [number, {"type": "null"}]}, "unit": text,
         "platform": text, "channel": text, "product_category": text},
        "商品名不得有首尾空白；金额未知填null，不传product_id。")
    observation = row(
        ["date", "kind", "measure", "source_quote", "assertion"],
        {**common, "kind": {"type": "string", "enum": ["客流", "订单", "天气", "排班", "评价"]},
         "measure": {"type": "string", "enum": ["人数", "桌数", "单量", "天气", "出勤", "评分", "差评数"]},
         "value_num": number, "value_text": text, "unit": {"type": "string", "maxLength": 100}, "subject": text},
        "value_num/value_text精确一个。客流人数/人、桌数/桌；订单单量/单；评价评分/分、差评数/个。"
        "天气天气使用阴/晴/雨/多云/雪；排班出勤使用请假/请了假/缺勤/迟到/早退/休息/上班/到岗。"
        "天气和排班不填unit或填空串。不得提供财务派生观测。")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["entries", "sales", "observations", "leftovers"],
        "description": "餐饮profile；三个领域数组合计最多50行，leftovers最多50个，不能全部为空。"
                       "数字不接受布尔值或数字字符串；来源片段和leftovers必须逐字出现在text中。"
                       "字段间约束和来源存在性由服务端另行校验。",
        "properties": {
            "entries": {"type": "array", "maxItems": 50, "items": entry},
            "sales": {"type": "array", "maxItems": 50, "items": sale},
            "observations": {"type": "array", "maxItems": 50, "items": observation},
            "leftovers": {"type": "array", "maxItems": 50,
                          "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
        },
    }


def _structured_example() -> dict:
    return {
        "workspace_id": "replace-with-authorized-workspace-id",
        "request_id": "stable-message-id-001",
        "text": "今天的营业额很差，只有1300。阴天，马拉松可能影响生意。",
        "received_at": "2026-10-25T20:00:00+08:00",
        "structured": {
            "entries": [{"date": "2026-10-25", "type": "revenue", "category": "营业额", "amount": 1300,
                         "amount_basis": "gross", "granularity": "daily", "assertion": "actual",
                         "source_quote": "今天的营业额很差，只有1300"}],
            "sales": [],
            "observations": [{"date": "2026-10-25", "kind": "天气", "measure": "天气",
                              "value_text": "阴", "assertion": "actual", "source_quote": "阴天"}],
            "leftovers": ["马拉松可能影响生意"],
        },
    }


def product_capabilities() -> dict:
    """返回可直接提供给 Agent 的 JSON 兼容能力说明。"""
    return {
        "product": "随口记经营助手",
        "contract_version": "foundation-1",
        "scope": "单个用户的个人记账与个人事业；可有多个工作空间和行业 profile",
        "capabilities": [item.as_dict() for item in _CAPABILITIES],
        "profiles": [asdict(profile) for profile in list_profiles()],
        "agent_policy": {
            "read_before_action": True,
            "ask_before_unsupported": True,
            "ask_before_ambiguous_financial_write": True,
            "never_guess": [
                "工作空间或行业 profile",
                "实际发生、计划、预算、报价之间的区别",
                "收入、采购、费用、销量的金额归属",
                "付款日期与费用归属月份",
                "营业总额、销售明细、结算实收之间的金额口径",
            ],
            "input_preparation": {
                "user_speech_may_be_messy": True,
                "responsible_party": "calling_agent",
                "preferred_operation": "capture_text",
                "preferred_payload_field": "structured",
                "structured_schema": _structured_schema(),
                "request_example": _structured_example(),
                "preserve_verbatim_text": True,
                "separate_actual_from_forecast_and_opinion": True,
                "require_source_quote": True,
                "ask_only_when_information_missing_or_conflicting": True,
                "guidance": (
                    "用户口语可能有语气词、跳跃叙述、多个事件和推测。调用方 Agent 先整理，"
                    "将明确发生的账目、销量、非金额观测分开，解析相对日期并标明金额口径，"
                    "保留逐字原话与每行来源片段；无法确定或不支持的片段放入 leftovers 并说明。"
                    "信息明确时直接提取，不因说法口语化而要求用户按固定句式重说；"
                    "真正缺少归属或存在冲突时先追问，不能擅自补值。"
                    "提交 structured 后工具只校验并写入整理结果，不再跑规则解析器生成另一套账目。"
                ),
            },
            "user_presentation": {
                "show_internal_field_names": False,
                "show_server_paths_as_downloads": False,
                "state_scope_before_read": True,
                "progress_labels": {
                    "entries": "营业收入、进货支出和各项费用",
                    "sales": "各商品的销量和销售金额",
                    "summary": "所选时间内的收支汇总",
                    "observations": "天气、客流等经营记录",
                },
                "guidance": (
                    "先用用户语言说明正在核对哪个账本、哪段时间、哪些数据，"
                    "只描述实际开始的操作；查询范围尚未确定时不能声称已核对。"
                    "结果先展示日期范围和已记录数据，再说明缺失项；"
                    "不输出 entries/sales/summary、None/null、接口路由或原始JSON等调试信息。"
                    "不把服务器文件路径当成用户可下载的链接；附件必须通过当前通道实际发送。"
                ),
                "report_policy": {
                    "required_context": ["workspace", "date_range", "coverage", "amount_basis"],
                    "preferred_sections": ["收支概览", "主要支出", "商品销售", "待补充信息"],
                    "currency": "CNY",
                    "money_decimals": 2,
                    "unavailable_value_text": "暂无法计算",
                    "missing_is_zero": False,
                    "cash_balance_is_profit": False,
                    "source_of_numbers": "authoritative_business_rows",
                    "guidance": (
                        "营业额、采购付款、费用付款和收支差额分别展示，金额使用人民币和千位分隔。"
                        "收支差额仅为已记收入减采购及费用，不称为利润；采购付款不等于耗料成本。"
                        "缺少成本/库存等依据时，利润显示暂无法计算并解释缺什么，不能显示0或None。"
                        "日总额和销售明细、派生观测不得重复相加；尚未记录的日期不当作零营业。"
                        "微信默认发送简短纯文本，避免宽表；网页或文件可用表格并保留同一统计口径。"
                        "本策略约束调用方展示；当前API仍返回业务JSON，尚未提供固定模板报表生成接口。"
                    ),
                },
            },
            "ilink_integration": {
                "recommended_flow": ["ilink_receive", "calling_agent_prepare", "structured_capture", "ilink_reply"],
                "builtin_worker_uses_llm": False,
                "single_poller_per_bot_account": True,
                "external_agent_should_keep_builtin_worker_disabled": True,
                "guidance": (
                    "外部 Agent 已负责 iLink 时，由该 Agent 收消息、整理、调用 structured capture 并回复；"
                    "随口记自带 worker 保持关闭，同一 Bot 账号不要同时由两端拉取。"
                    "内置 worker 当前直接使用受控规则，没有模型整理能力。"
                    "消息身份要含账号、peer、message_id，结构化请求须固定保存后再发送，重试使用同一正文和请求号。"
                ),
            },
            "capture_result_policy": {
                "http_200_means_financial_write": False,
                "report_financial_rows_from": ["entries", "sales"],
                "report_unparsed_fragments_from": "parsed.leftovers",
                "partial_may_include_financial_rows": True,
                "preserve_original_text": True,
                "do_not_rewrite_and_resubmit_to_force_parsing": True,
                "parser_miss_is_not_user_ambiguity": True,
                "guidance": (
                    "分别报告原话保存、实际账目/销量和未解析片段；观测已保存不能说成金额已入账。"
                    "明确原话未被规则识别时应说明解析能力缺口，不能声称用户没有说清金额归属。"
                    "允许 Agent 整理为 structured 并与逐字原话同次提交；"
                    "不要改写原话再当作用户新记录提交；已有结果按原 request_id 重放，"
                    "实际补录须核对已有账目并保留对原记录的依据。"
                ),
            },
            "clarification_template": "我目前支持{supported}; 这条需求属于{gap}，不能安全地直接记账。请确认{question}。",
        },
    }
