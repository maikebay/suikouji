"""行业 profile 注册表。

profile 只描述一个工作空间采用的业务模块，不把所有行业字段塞进通用账表。
当前先提供声明式元数据；解析和报表仍由现有餐饮代码负责，后续模块可以按
profile_id 注册自己的投影、导入器和词典，而不改变旧账本的三种金额类型。
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class IndustryProfile:
    id: str
    label: str
    description: str
    ledger_categories: tuple[str, ...] = ()
    observation_kinds: tuple[str, ...] = ()

    def __post_init__(self):
        if not _PROFILE_ID.fullmatch(self.id):
            raise ValueError("行业 profile ID 必须是小写字母、数字、下划线或连字符")
        if not self.label.strip():
            raise ValueError("行业 profile 名称不能为空")


class ProfileRegistry:
    def __init__(self, profiles: tuple[IndustryProfile, ...] = ()):
        self._profiles: dict[str, IndustryProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: IndustryProfile, *, replace: bool = False) -> IndustryProfile:
        if not isinstance(profile, IndustryProfile):
            raise TypeError("只能注册 IndustryProfile")
        if profile.id in self._profiles and not replace:
            raise ValueError(f"行业 profile 已存在：{profile.id}")
        self._profiles[profile.id] = profile
        return profile

    def get(self, profile_id: str) -> IndustryProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise ValueError(f"未知行业 profile：{profile_id}") from exc

    def maybe_get(self, profile_id: str | None) -> IndustryProfile | None:
        return self._profiles.get(profile_id or "")

    def list(self) -> list[IndustryProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]


PROFILES = ProfileRegistry((
    IndustryProfile(
        "general", "个人事务与事业", "适用于个人想法、通用记事和尚未选定行业的事业记录",
    ),
    IndustryProfile(
        "restaurant", "餐饮经营", "沿用当前收入、采购、费用、销量和配方成本模型",
        ledger_categories=("营业额", "食材成本", "房租", "人工", "水电"),
        observation_kinds=("客流", "订单", "天气", "排班", "评价"),
    ),
    IndustryProfile(
        "retail", "零售经营", "为商品、采购、销售和库存模块预留的行业边界",
        ledger_categories=("销售收入", "进货", "经营费用"),
        observation_kinds=("客流", "订单", "库存"),
    ),
    IndustryProfile(
        "services", "服务业务", "为项目、客户、工时和服务收入模块预留的行业边界",
        ledger_categories=("服务收入", "采购", "经营费用"),
        observation_kinds=("工时", "订单", "客户评价"),
    ),
    IndustryProfile(
        "project", "项目事业", "为项目、里程碑、报价和收款模块预留的行业边界",
        ledger_categories=("项目收入", "项目成本", "经营费用"),
        observation_kinds=("工时", "进度", "里程碑"),
    ),
))


def register_profile(profile: IndustryProfile, *, replace: bool = False) -> IndustryProfile:
    """注册进程内的行业模块；持久化的工作空间只保存 profile_id。"""
    return PROFILES.register(profile, replace=replace)


def get_profile(profile_id: str) -> IndustryProfile:
    return PROFILES.get(profile_id)


def list_profiles() -> list[IndustryProfile]:
    return PROFILES.list()
