"""随口记 · 领域基础：类型、单位、日期、金额。

这一层不碰存储、不碰网络，只提供记账共用的常量与换算。
单位换算是成本核算的地基：进货常按「斤/公斤」，配方常按「克」，
没有统一换算就算不出单品成本。
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid

# ---------------------------------------------------------------- 账目类型

SHOP_DEFAULT = "default"

TYPE_REVENUE = "revenue"
TYPE_PURCHASE = "purchase"
TYPE_EXPENSE = "expense"
TYPES = (TYPE_REVENUE, TYPE_PURCHASE, TYPE_EXPENSE)

TYPE_LABEL = {
    TYPE_REVENUE: "营业额",
    TYPE_PURCHASE: "进货",
    TYPE_EXPENSE: "支出",
}

# 固定成本：算保本点用。其余支出视为随营业额浮动。
FIXED_CATEGORIES = ("房租", "人工", "水电")

SOURCE_CHAT = "chat"
SOURCE_IMPORT = "import"
SOURCE_MANUAL = "manual"

# ---------------------------------------------------------------- 单位

UNIT_CANON = {
    "公斤": "kg", "千克": "kg", "kg": "kg", "KG": "kg", "Kg": "kg",
    "斤": "斤",
    "克": "g", "g": "g", "G": "g",
    "升": "L", "L": "L", "毫升": "ml", "ml": "ml",
    "箱": "箱", "瓶": "瓶", "袋": "袋", "份": "份", "个": "个", "只": "只",
    "条": "条", "桶": "桶", "包": "包", "件": "件", "杯": "杯", "盒": "盒",
    "串": "串", "碗": "碗", "屉": "屉", "位": "位", "桌": "桌", "点": "点",
}

# 只对重量单位做换算；计数单位（份/瓶/个）之间不可换算。
_WEIGHT_IN_G = {"kg": 1000.0, "斤": 500.0, "g": 1.0}


def canon_unit(unit: str) -> str:
    """把别名归一到规范单位名；未知单位原样返回。"""
    u = (unit or "").strip()
    return UNIT_CANON.get(u, u)


def is_weight(unit: str) -> bool:
    return canon_unit(unit) in _WEIGHT_IN_G


def to_grams(qty: float, unit: str) -> float | None:
    """重量换算成克；非重量单位返回 None。"""
    u = canon_unit(unit)
    factor = _WEIGHT_IN_G.get(u)
    if factor is None:
        return None
    return float(qty) * factor


def convert(qty: float, from_unit: str, to_unit: str) -> float | None:
    """同族单位换算。重量互通；其余仅同名可换算，否则 None。"""
    fu, tu = canon_unit(from_unit), canon_unit(to_unit)
    if fu == tu:
        return float(qty)
    fg, tg = _WEIGHT_IN_G.get(fu), _WEIGHT_IN_G.get(tu)
    if fg is not None and tg is not None:
        return float(qty) * fg / tg
    volume = {'L': 1000.0, 'ml': 1.0}
    if fu in volume and tu in volume:
        return float(qty) * volume[fu] / volume[tu]
    return None


# ---------------------------------------------------------------- 日期

_ISO_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_MD_RE = re.compile(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?$")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def today_iso() -> str:
    return _dt.date.today().isoformat()


def iso(d: _dt.date) -> str:
    return d.isoformat()


def shift_days(days: int, base: _dt.date | None = None) -> str:
    return ((base or _dt.date.today()) + _dt.timedelta(days=days)).isoformat()


def parse_date(text: str, base: _dt.date | None = None) -> str | None:
    """尽量把一段文本认成日期，认不出返回 None。

    支持 2026-10-23 / 2026/10/23 / 20261023 / 10月23日 / 10-23。
    """
    s = (text or "").strip()
    if not s:
        return None
    now = base or _dt.date.today()

    m = _ISO_RE.match(s) or _COMPACT_RE.match(s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    m = _MD_RE.match(s)
    if m:
        try:
            return _dt.date(now.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        try:
            return _dt.date(now.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    return None


def month_of(date_iso: str) -> str:
    """'2026-10-23' -> '2026-10'"""
    return (date_iso or "")[:7]


def month_range(month: str) -> tuple[str, str]:
    """'2026-10' -> ('2026-10-01', '2026-10-31')"""
    year, mon = int(month[:4]), int(month[5:7])
    first = _dt.date(year, mon, 1)
    nxt = _dt.date(year + (mon // 12), (mon % 12) + 1, 1)
    return first.isoformat(), (nxt - _dt.timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- 金额


def round2(x: float) -> float:
    """四舍五入到分，避开浮点尾差。"""
    return round(float(x) + 1e-9, 2)


def fmt_money(x: float | None) -> str:
    """金额展示：整数不带小数，其余保留两位。"""
    if x is None:
        return "—"
    v = round2(x)
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.2f}"


def fmt_price(x: float | None) -> str:
    """单价展示：按量级给有效位。

    单价常常远小于 1 元（0.0485 元/克），按金额的两位小数会显示成 0.05，
    看着像另一回事，所以小额单价要留够位数。
    """
    if x is None:
        return "—"
    v = float(x)
    if v == 0:
        return "0"
    if abs(v) >= 1:
        return fmt_money(v)
    digits = 4 if abs(v) >= 0.01 else 6
    return f"{v:.{digits}f}".rstrip("0").rstrip(".")


def sale_amount(sale: dict, product: dict | None) -> tuple:
    """一笔销量的销售额，返回 (金额, 是否推算)。

    商家常常只报份数（「牛肉面卖了30份」），没报金额。这时候按商品售价推算，
    并标出这是推算值 —— 报表要注明，别让人当成实测数。
    """
    amount = sale.get("amount")
    if amount is not None:
        return round2(float(amount)), False
    price = (product or {}).get("price")
    qty = sale.get("qty")
    if price is None or not qty:
        return None, False
    return round2(float(price) * float(qty)), True


def fmt_qty(x: float | None) -> str:
    if x is None:
        return "—"
    v = float(x)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:g}"


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


# ---------------------------------------------------------------- 标识


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    # 必须带微秒：同一次调用里连续插入的多行（比如一份配方）如果时间戳相同，
    # 按 created_at 排序就没了确定顺序，配方行会随机换位。
    return _dt.datetime.now().isoformat(timespec="microseconds")
