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
            "clarification_template": "我目前支持{supported}; 这条需求属于{gap}，不能安全地直接记账。请确认{question}。",
        },
    }
