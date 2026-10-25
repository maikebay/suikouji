"""随口记 · 中文口语记账解析（规则版）。

输入一句中文口语，例如
    「今天卖了1280，买了50斤牛肉花了680，交房租8000」
输出
    {"intent": "entries", "date": "...", "entries": [...], "confidence": 0.8}

这是不联网、不调模型的确定性解析器，负责绝大多数日常说法。
认不出来时返回 intent="unknown"，保留原话供补充确认。
结构化补充走同一个 entry 结构落库。
"""
from __future__ import annotations

import datetime as _dt
import math
import re

from .model import TYPE_EXPENSE, TYPE_PURCHASE, TYPE_REVENUE, canon_unit, shift_days

# ---------------------------------------------------------------- 中文数字

_CN_DIGIT = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_ZERO = "零〇"
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def cn_to_number(s: str) -> float:
    """口语中文数字转数值，处理省略说法。

    一千二 = 1200，三百五 = 350，一万二 = 12000，五十 = 50。
    「零」要单独处理：它既表示这一位是 0，也取消末尾省略位的补零
    （三百零五 = 305，不是 350）。
    """
    total = 0.0
    num = 0
    last_unit = 0
    tail = -1
    zero_seen = False
    for ch in s:
        if ch in _CN_ZERO:
            zero_seen = True
            num = 0
            tail = -1
            continue
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
            tail = num
            continue
        unit = _CN_UNIT.get(ch)
        if not unit:
            continue
        if unit >= 10000:
            total = (total + num) * unit
        else:
            if num == 0:
                num = 1
            total += num * unit
        num = 0
        last_unit = unit
        tail = -1
    if tail >= 0 and last_unit >= 100 and not zero_seen:
        return total + (tail * last_unit) / 10
    return total + num


# ---------------------------------------------------------------- 词表

# 顺序即优先级：支出名词 > 进货 > 收入 > 支出动词
_LEXICON = [
    (TYPE_EXPENSE, "房租", ["店面租金", "房租", "租金", "铺租"]),
    (TYPE_EXPENSE, "人工", ["员工工资", "兼职工资", "工资", "人工", "社保"]),
    (TYPE_EXPENSE, "水电", ["水电费", "燃气费", "电费", "水费", "煤气", "水电"]),
    (TYPE_EXPENSE, "平台佣金", ["平台抽成", "美团抽成", "外卖抽成", "平台费", "服务费", "抽成", "佣金"]),
    (TYPE_EXPENSE, "包装耗材", ["打包盒", "餐盒", "包装", "耗材", "纸巾"]),
    (TYPE_EXPENSE, "推广", ["推广", "广告", "宣传", "营销"]),
    (TYPE_EXPENSE, "维修", ["维修", "修理"]),
    (TYPE_EXPENSE, "税费", ["税费", "税"]),
    (TYPE_PURCHASE, "食材成本", ["进货", "采购", "拿货", "买进", "食材", "原料", "货款", "成本", "买了", "买"]),
    (TYPE_REVENUE, "营业额", ["营业额", "销售额", "营收", "流水", "收入", "收款", "进账", "堂食", "外卖", "卖了", "卖出", "卖掉", "卖"]),
    (TYPE_EXPENSE, "其他支出", ["交了", "付了", "花了", "支出", "扣了", "开销"]),
]


def _rank(type_: str, category: str) -> int:
    if type_ == TYPE_EXPENSE and category != "其他支出":
        return 4
    if type_ == TYPE_PURCHASE:
        return 3
    if type_ == TYPE_REVENUE:
        return 2
    return 1


_WORDS = []
for _t, _c, _ws in _LEXICON:
    for _w in _ws:
        _WORDS.append((_w, _t, _c, _rank(_t, _c)))
# 长词优先，避免「营业额」被「营业」这类短词切走
_WORDS.sort(key=lambda x: -len(x[0]))
_WORD_RE = re.compile("|".join(re.escape(w[0]) for w in _WORDS))

# 数量单位：用于识别「50斤」「三箱」
# 「人」「次」也算数量单位：没有它，「客流120人」里的 120 会被当成金额，
# 凭空记出一笔 120 元的支出 —— 记不下比记错好，所以宁可让它落到 leftovers。
QUANTITY_UNIT = "公斤|千克|kg|KG|斤|克|箱|瓶|袋|份|个|只|条|桶|包|件|杯|盒|串|碗|屉|位|桌|点|人(?!工|手)|次"
# 金额单位：用于识别「1.2万」「三百块」
MONEY_UNIT = "万|千|k|K|w|W|毛|块|元"

_DATE_WORDS = [
    ("大前天", -3), ("前天", -2), ("昨天", -1), ("昨日", -1),
    ("今天", 0), ("今日", 0), ("当天", 0), ("明天", 1),
]

_STOPWORDS = re.compile(
    "(今天|今日|昨天|昨日|前天|大前天|当天|早上|上午|中午|下午|晚上|夜里|我们|我|店里|"
    "一共|总共|大概|大约|差不多|左右|然后|还有|另外|帮我|记一下|记一笔|记录一下|一下|"
    "了|的|是|有|又|也|都|就|再|块|元|钱|毛|和|跟)"
)

_QUERY_RE = re.compile("(多少|怎么样|咋样|如何|看看|查一下|统计|汇总|赚了|亏了|毛利|净利|利润)")

# 只含中文数字的串，用于找「三斤」「八百块」
_CN_NUM_RE = re.compile("[零〇一二两三四五六七八九十百千万]+")
_CN_DIGITS = "零〇一二两三四五六七八九十百千万"
_NUM_TOKEN = r"(?:\d+(?:\.\d+)?|[" + _CN_DIGITS + r"]+)"
_CN_FRACTION_RE = re.compile(
    r"(" + _NUM_TOKEN + r")\s*(?:块|元)\s*"
    r"([" + _CN_DIGITS + r"]|\d)"
    r"(?:\s*(?:角|毛)|"
    r"(?!\s*(?:角|毛|块|元)|[" + _CN_DIGITS + r"\d.千百千万]))"
)


def _mask(text: str, spans) -> str:
    """把已认领的片段换成空格，保持索引对齐，避免重复认领。"""
    chars = list(text)
    for s in spans:
        start, end = s["start"], s["end"]
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


# ---------------------------------------------------------------- 日期


def find_date(text: str, base: _dt.date | None = None) -> dict:
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            date = _dt.date.fromisoformat(match.group()).isoformat()
            return {"date": date, "span": {"start": match.start(), "end": match.end()}}
        except ValueError as exc:
            raise ValueError('日期不合法：' + match.group()) from exc
    for word, offset in _DATE_WORDS:
        idx = text.find(word)
        if idx >= 0:
            return {"date": shift_days(offset, base), "span": {"start": idx, "end": idx + len(word)}}

    m = re.search(r'(?:(\d{4})年)?(\d{1,2})\s*月\s*(\d{1,2})(?:\s*[日号]|(?![\d千万百元块毛]))', text)
    if m:
        now = base or _dt.date.today()
        try:
            d = _dt.date(int(m.group(1) or now.year), int(m.group(2)), int(m.group(3)))
            return {"date": d.isoformat(), "span": {"start": m.start(), "end": m.end()}}
        except ValueError as exc:
            raise ValueError('日期不合法：' + m.group()) from exc

    m = re.search(r"(\d{1,2})\s*[日号]", text)
    if m:
        now = base or _dt.date.today()
        try:
            d = _dt.date(now.year, now.month, int(m.group(1)))
            return {"date": d.isoformat(), "span": {"start": m.start(), "end": m.end()}}
        except ValueError as exc:
            raise ValueError('日期不合法：' + m.group()) from exc

    return {"date": shift_days(0, base), "span": None}


def find_periods(text, base=None):
    """费用归属月独立于付款日期；先遮蔽月份数字，避免误当金额。"""
    now = base or _dt.date.today()
    pattern = r"(?:(\d{4})年)?(\d{1,2}|[一二三四五六七八九十]{1,3})月(?:份)?|(?<!\d)(\d{4})-(\d{2})(?![-\d])|本月|这个月|上月|下月"
    out = []
    for m in re.finditer(pattern, text):
        if m.group(1) or m.group(2):
            year = int(m.group(1) or now.year)
            raw = m.group(2)
            month = int(raw) if raw.isdigit() else int(cn_to_number(raw))
        elif m.group(3):
            year, month = int(m.group(3)), int(m.group(4))
        else:
            delta = {'上月': -1, '下月': 1}.get(m.group(), 0)
            absolute = now.year * 12 + now.month - 1 + delta
            year, month = absolute // 12, absolute % 12 + 1
        valid = 1 <= month <= 12 and 1 <= year <= 9999
        out.append({'start': m.start(), 'end': m.end(),
                    'period_month': f'{year:04d}-{month:02d}' if valid else None})
    return out


def sales_dimensions(text):
    platform = next((p for p in ('饿了么', '美团', '抖音', '微信', '门店') if p in text), '')
    channel = next((c for c in ('堂食', '自取', '配送', '外卖') if c in text), '')
    return {'platform': platform, 'channel': '配送' if channel == '外卖' else channel}


# ---------------------------------------------------------------- 数量


def find_quantities(text: str) -> list:
    out = []
    keywords = find_keywords(text)

    def is_amount(start, unit_start, end):
        if any(unit_start == h['start'] and h['end'] > end for h in keywords):
            return True
        prior = [h for h in keywords if h['end'] <= start]
        # 「水电200包80」不是已证明的200包采购，不能遮掉水电200。
        return bool(prior and prior[-1]['type'] == TYPE_EXPENSE
                    and prior[-1]['category'] != '其他支出'
                    and re.match(r'\s*' + _NUM_TOKEN, text[end:]))

    re_ar = re.compile(r"(\d+(?:\.\d+)?)\s*(" + QUANTITY_UNIT + ")", re.I)
    for m in re_ar.finditer(text):
        if is_amount(m.start(), m.start(2), m.end(2)):
            continue
        out.append({
            "value": float(m.group(1)), "unit": canon_unit(m.group(2)),
            "raw": m.group(0), "start": m.start(), "end": m.end(),
        })

    # 中文数量：三斤、两瓶
    for m in _CN_NUM_RE.finditer(text):
        after = text[m.end():]
        unit_m = re.match(r"^\s*(" + QUANTITY_UNIT + ")", after, re.I)
        if not unit_m:
            continue
        if is_amount(m.start(), m.end() + unit_m.start(1), m.end() + unit_m.end(1)):
            continue
        if any(m.start() < q["end"] and m.end() > q["start"] for q in out):
            continue
        out.append({
            "value": cn_to_number(m.group(0)), "unit": canon_unit(unit_m.group(1)),
            "raw": m.group(0) + unit_m.group(0),
            "start": m.start(), "end": m.end() + unit_m.end(),
        })

    out.sort(key=lambda q: q["start"])
    return out


# ---------------------------------------------------------------- 金额


def find_amounts(text: str) -> list:
    out = []
    claimed = []
    for m in _CN_FRACTION_RE.finditer(text):
        whole = _number_value(m.group(1))
        fraction = _number_value(m.group(2)) / 10
        out.append({'value': round(whole + fraction, 2), 'start': m.start(), 'end': m.end(), 'unit': '元'})
        claimed.append({'start': m.start(), 'end': m.end()})
    re_ar = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s*(" + MONEY_UNIT + "))?", re.I)
    for m in re_ar.finditer(text):
        if any(m.start() < a['end'] and m.end() > a['start'] for a in claimed):
            continue
        value = float(m.group(1).replace(',', ''))
        unit = m.group(2) or ""
        if unit == "万":
            value *= 10000
        elif unit in ("千", "k", "K", "w", "W"):
            value *= 1000
        elif unit == "毛":
            value *= 0.1
        if not math.isfinite(value):
            raise ValueError('金额必须是非负有限数')
        if value <= 0:
            continue
        out.append({
            "value": round(value + 1e-9, 2), "start": m.start(), "end": m.end(), "unit": unit,
        })

    # 中文金额：八百块、一千二、三百五
    for m in _CN_NUM_RE.finditer(text):
        if any(m.start() < a["end"] and m.end() > a["start"] for a in out):
            continue
        after = text[m.end():]
        money_m = re.match(r"^\s*(万|千|块|元|毛)", after)
        value = cn_to_number(m.group(0))
        if not money_m:
            # 没跟钱单位时，≥2 的数才当金额；「一下」「一点」不算
            if value < 2:
                continue
            out.append({"value": value, "start": m.start(), "end": m.end(), "unit": ""})
            continue
        unit = money_m.group(1)
        if unit == "万":
            value *= 10000
        elif unit == "千":
            value *= 1000
        elif unit == "毛":
            value *= 0.1
        out.append({
            "value": value, "start": m.start(),
            "end": m.end() + money_m.end(), "unit": unit,
        })

    out.sort(key=lambda a: a["start"])
    return out


# ---------------------------------------------------------------- 关键词


def find_keywords(text: str) -> list:
    hits = []
    for m in _WORD_RE.finditer(text):
        for word, type_, category, rank in _WORDS:
            if word == m.group(0):
                hits.append({
                    "word": word, "type": type_, "category": category, "rank": rank,
                    "start": m.start(), "end": m.end(),
                })
                break
    return hits


def classify(clause: str) -> dict | None:
    """取优先级最高的关键词决定这笔账的性质。"""
    hits = find_keywords(clause)
    if not hits:
        return None
    best = hits[0]
    for h in hits:
        if h["rank"] > best["rank"]:
            best = h
    return best


# ---------------------------------------------------------------- 分句


def split_clauses(text: str) -> list:
    clean = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    clean = re.sub(r"[。；;！!？?]", "|", clean)
    clean = re.sub(r"[，,、]", "|", clean)
    return [s.strip() for s in clean.split("|") if s.strip()]


def _is_noise(fragment: str, base=None) -> bool:
    """只剩时间词和虚词的碎片不算「没看懂」。

    遮掉观测层认领的片段之后，「今天下雨」会剩一个「今天」。
    把它列进「没看懂的片段」是在误导人 —— 那句话其实看懂了。
    """
    rest = _mask(fragment, [s for s in (find_date(fragment, base)["span"],) if s])
    rest = _STOPWORDS.sub(" ", rest)
    return not re.search(r"[一-龥A-Za-z0-9]", rest)


_DATE_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}|大前天|前天|昨天|昨日|今天|今日|当天|明天|"
    r"(?:(?:\d{4})年)?\d{1,2}\s*月\s*\d{1,2}(?:\s*[日号]|(?![\d千万百元块毛]))|"
    r"\d{1,2}\s*[日号]"
)
_CONTEXT = re.compile(
    r"(?:\s|然后|还有|另外|再|又|交了?|付了?|发了?|花了|的|"
    r"美团|饿了么|抖音|微信|门店|堂食|配送|外卖|自取)*"
)


def find_dates(text, base=None, *, strict=True):
    """顺序扫描账目与观测共用的日期；仅来源预存允许跳过非法日历日期。"""
    out = []
    for match in _DATE_TOKEN.finditer(text):
        try:
            info = find_date(match.group(), base)
        except ValueError:
            if strict:
                raise
            continue
        if info['span']:
            out.append({'start': match.start(), 'end': match.end(), 'date': info['date']})
    return out


def _ledger_amounts(text, base=None):
    """日期、费用月份和采购数量不是金额；所有分段共用这个口径。"""
    return find_amounts(_mask(text, find_dates(text, base) + find_periods(text, base) + find_quantities(text)))


def _entry_boundary(clause, lower, end, base=None):
    """下一笔的日期、月份、支付动词和连接词随下一笔走，不粘进上一笔品名。"""
    gap = clause[lower:end]
    masked = _mask(gap, find_dates(gap, base) + find_periods(gap, base))
    return lower if _CONTEXT.fullmatch(masked) else end


def split_by_type_switch(clause: str, base: _dt.date | None = None) -> list:
    """按独立的记账锚点分段，而不是按类型优先级分段。

    房租/水电甚至重复房租都各有金额；买了50斤牛肉花了680只有一个金额，
    买与花不能拆。后置「300元成本」只认领紧邻的300，不能改掉更早的营业额。
    """
    dates = find_dates(clause, base)
    if any(d['start'] > 0 for d in dates):
        cuts = [0] + [d['start'] for d in dates if d['start'] > 0] + [len(clause)]
        return [segment for lower, upper in zip(cuts, cuts[1:])
                for segment in split_by_type_switch(clause[lower:upper], base)]
    periods = find_periods(clause, base)
    for period in periods:
        prefix, suffix = clause[:period['start']], clause[period['start']:]
        if (period['start'] > 0 and find_keywords(prefix) and _ledger_amounts(prefix, base)
                and find_keywords(suffix) and _ledger_amounts(suffix, base)):
            # 「10月8000房租11月9000水电」的11月属于下一笔，不能
            # 等到水电才切，让前段因含两个月份而整笔丢失。
            return (split_by_type_switch(prefix, base)
                    + split_by_type_switch(suffix, base))
    amounts = _ledger_amounts(clause, base)
    segments, start = [], 0
    for hit in find_keywords(clause):
        prior = [a for a in amounts if start <= a['start'] and a['end'] <= hit['start']]
        if not prior:
            continue
        if (len(prior) >= 2 and not clause[prior[-1]['end']:hit['start']].strip()
                and not find_keywords(clause[prior[-2]['end']:prior[-1]['start']])
                and find_keywords(clause[start:prior[-2]['start']])):
            cut = prior[-1]['start']
        elif find_keywords(clause[start:prior[-1]['start']]):
            cut = _entry_boundary(clause, prior[-1]['end'], hit['start'], base)
        else:
            continue
        if cut > start:
            segments.append(clause[start:cut])
            start = cut
    segments.append(clause[start:])
    return [s.strip() for s in segments if s.strip()]


# ---------------------------------------------------------------- 条目名


def _build_item(clause: str, spans) -> str:
    rest = _mask(clause, spans)
    rest = _STOPWORDS.sub(" ", rest)
    rest = re.sub(r"[^一-龥A-Za-z0-9%]", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip()
    return rest[:14]


def _default_item(type_: str, category: str) -> str:
    if type_ == TYPE_REVENUE:
        return "营业额"
    if type_ == TYPE_PURCHASE:
        return "进货"
    return category or "支出"


# ---------------------------------------------------------------- 销量

_SALE_VERB = "卖了|卖出|卖掉|销售了|销售|售出"
_SALE_UNITS = "份|个|碗|杯|只|条|盒|瓶|串|屉|桌|位"
_SALE_NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"

# 前缀里的时间词不能当成商品名
_LEAD_TIME = re.compile(
    r"^(今天|今日|昨天|昨日|前天|大前天|当天|早上|上午|中午|下午|晚上|夜里|刚刚|刚才|"
    r"我们|我|店里|一共|总共|又|再|大概|大约)+"
)
# 平台/渠道词也不能当商品名
_LEAD_PLATFORM = re.compile(r"^(?:(?:美团|饿了么|抖音|微信|门店|堂食|自取|配送|外卖|的)\s*)+")
# 「营业额1280牛肉面」：商品名前面粘着记账词和数字，那是另一笔账，不是商品名。
# 只从名字里去掉，原文里留着 —— 遮掉它那 1280 就凭空消失了。
_LEAD_ACCOUNTING = re.compile(
    r"^(?:营业额|销售额|营收|流水|收入|收款|进账|支出|开销|成本|进货|采购|人工|工资|房租|水电|"
    r"卖了|卖出|交了|付了|花了|买了|买)\s*"
    r"(?:" + _NUM_TOKEN + r")\s*(?:公斤|千克|斤|克|箱|袋|份|个|块|元|毛)?\s*"
)
# 商品名里粘着的中文金额（「牛肉面三百元」）：从名字里截掉，金额交给下面认领。
_MONEY_IN_NAME = re.compile(
    r"(?:\d+(?:,\d{3})*(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)\s*(?:" + MONEY_UNIT + r")"
)
_LEAD_PARTICLE = re.compile(r"^(的|了|和|跟)+")
# 「牛肉面卖了30份300元」：紧贴在销量后面的金额是这笔的合计，不是另一笔支出。
# 合计说法要认全（合计/总计/共计/拢共/一共是…），中文数字（三百块）也要认，
# 漏认会走 analyze_clause 的兜底，凭空多记一笔「其他支出」。
_TOTAL_MARK = r"(?:(?:一共|总共|合计|总计|共计|拢共|统共|共|卖了|卖|是)\s*)*"
_CLAUSE_BREAK = re.compile(r"[，,、；;。!！?？\n]")


def _number_value(raw: str) -> float:
    return float(raw) if raw and raw[0].isdigit() else float(cn_to_number(raw))


def _clean_product_name(name: str) -> tuple:
    """返回（商品名, 从原串前面去掉的字数）。

    去掉的字在原文里仍然留着：「营业额1280」是另一笔账，不能被销量遮掉，
    调用方按偏移量把这条销量的起点往后挪，把那笔账让回给分句解析。
    """
    s = str(name or "")
    offset = len(s) - len(s.lstrip(" 　"))
    s = s.lstrip(" 　")
    patterns = (_DATE_TOKEN, _LEAD_TIME, _LEAD_PLATFORM, _LEAD_ACCOUNTING, _LEAD_PARTICLE)
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            m = pattern.match(s)
            if m:
                offset += m.end()
                s = s[m.end():]
                changed = True
                break
    head = s.lstrip(" 　的")
    offset += len(s) - len(head)
    return head.rstrip(" 　的"), offset


def _tail_belongs_to_spending(text: str, pos: int, base=None) -> bool:
    """金额后面紧跟支出/进货说法时，这笔钱属于那句支出，销量不能认领。

    「牛肉面卖了30份，300元成本」里的 300 是成本；「卖了30份，300元买了50斤牛肉」
    里的 300 是货款。只看金额前面会把这两种都认成销量，支出整笔丢掉，
    账面上还看不出来。中间隔了逗号就是另一句（「300元，今天买菜」），照常认领。
    """
    m = _CLAUSE_BREAK.search(text, pos)
    tail = text[pos:m.start()] if m else text[pos:]
    hits = find_keywords(tail)
    if not hits or hits[0]['type'] == TYPE_REVENUE:
        return False
    hit = hits[0]
    gap = tail[:hit['start']]
    if find_dates(gap, base):
        return False
    if not _CONTEXT.fullmatch(_mask(gap, find_periods(gap, base))):
        return False
    # 「200元房租8000」中房租有自己的金额；「300元交房租8000」的
    # 支付动词却可能在解释前面的300，仍保守留待核对，不替老板猜。
    has_own_amount = any(a['start'] >= hit['end'] for a in _ledger_amounts(tail, base))
    payment_verb = re.search(r'交|付|发|花', gap)
    return not has_own_amount or bool(payment_verb) or hit['category'] == '食材成本'


_SALE_ANCHOR = re.compile(r"(?:" + _SALE_VERB + r")\s*(" + _SALE_NUM + r")\s*(" + _SALE_UNITS + r")")
_PRICE_MARK = re.compile(r"\s*[，,、]?\s*(?:一份|每份|单价|单件|每个)\s*(?:卖|是)?\s*")
_TOTAL_PREFIX = re.compile(r"\s*[，,、]?\s*" + _TOTAL_MARK)
_EXPLICIT_TOTAL = re.compile(r"一共|总共|合计|总计|共计|拢共|统共|共")
_NAME_STOP = re.compile(
    r"[，,、；;。!！?？\n]|(?:一份|每份|单价|单件|每个|合计|总计|共计|拢共|统共|总共|一共|共)\s*(?:卖|是)?\s*(?=" + _NUM_TOKEN + r")"
)


def _sale_name_before(text, lower, end, base=None):
    prefix = text[lower:end]
    breaks = list(_CLAUSE_BREAK.finditer(prefix))
    start = lower + (breaks[-1].end() if breaks else 0)
    prefix = text[start:end]
    # 未完成的前一销量不能被当作下一笔的商品名前缀；标点后可独立恢复。
    if _SALE_ANCHOR.search(prefix):
        return None
    # 前面的采购/营业额只让出金额后的文本，不吞掉另一笔账。
    amounts = _ledger_amounts(prefix, base)
    if amounts and (find_keywords(prefix) or any(a['unit'] for a in amounts)):
        start += amounts[-1]['end']
    name, skipped = _clean_product_name(text[start:end])
    if not re.fullmatch(r"[一-龥A-Za-z0-9]{2,12}", name):
        return None
    if find_keywords(name) or _MONEY_IN_NAME.search(name):
        return None
    return name, start + skipped


def _sale_name_after(text, start, limit, base=None):
    tail = text[start:limit]
    stops = [m.start() for m in _NAME_STOP.finditer(tail)]
    stops += [m.start() for m in _MONEY_IN_NAME.finditer(tail)]
    stops += [d['start'] for d in find_dates(tail, base)]
    _, skipped = _clean_product_name(tail)
    stops += [m.start() for m in re.finditer(r'美团|饿了么|抖音|微信|门店|堂食|配送|外卖|自取', tail)
              if m.start() >= skipped]
    stops += [h['start'] for h in find_keywords(tail)
              if h['word'] not in ('堂食', '外卖')]
    raw = tail[:min(stops, default=len(tail))].rstrip()
    name, _ = _clean_product_name(raw)
    if not re.fullmatch(r"[一-龥A-Za-z0-9]{2,12}", name):
        return None
    return name, start + len(raw)


def _sale_payment(text, start, limit, base=None):
    """单价必须明说；显式合计优先；裸金额遇支出语义时不抢占。"""
    tail = text[start:limit]
    price = _PRICE_MARK.match(tail)
    prefix = price or _TOTAL_PREFIX.match(tail)
    offset = prefix.end()
    amounts = find_amounts(tail[offset:])
    if not amounts or amounts[0]['start'] != 0:
        return None
    money = amounts[0]
    explicit_total = not price and _EXPLICIT_TOTAL.search(prefix.group())
    if not money['unit'] and not explicit_total:
        return None
    end = start + offset + money['end']
    if not price and not explicit_total and _tail_belongs_to_spending(text[:limit], end, base):
        return None
    return money['value'], end, bool(price)


def find_sales(text: str, base: _dt.date | None = None) -> list:
    """从左到右扫描销量锚点，两种语序不再抢占同一个数量。"""
    anchors = list(_SALE_ANCHOR.finditer(text))
    out, cursor = [], 0
    for index, anchor in enumerate(anchors):
        limit = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
        before = _sale_name_before(text, cursor, anchor.start(), base)
        if before:
            name, start = before
            end = anchor.end()
        else:
            after = _sale_name_after(text, anchor.end(), limit, base)
            if not after:
                continue
            name, end = after
            start = anchor.start()
            if index + 1 < len(anchors) and not text[end:limit].strip():
                next_anchor = anchors[index + 1]
                next_limit = anchors[index + 2].start() if index + 2 < len(anchors) else len(text)
                if not _sale_name_after(text, next_anchor.end(), next_limit, base):
                    # 「卖了30份牛肉面炒饭卖了20份」没有商品分界，不能
                    # 猜成一个长商品，也不能凭词典拆成两个；整段留待识别。
                    continue
        qty = _number_value(anchor.group(1))
        amount, unit_price = None, None
        payment = _sale_payment(text, end, limit, base)
        if payment:
            value, end, is_price = payment
            unit_price = value if is_price else None
            amount = round(qty * value, 2) if is_price else value
        if not math.isfinite(qty) or (amount is not None and not math.isfinite(amount)):
            raise ValueError('销量和金额必须是非负有限数')
        # 日期按原文位置继承，平台/渠道只从本笔取，不串到下一笔。
        prefix = text[cursor:start]
        breaks = list(_CLAUSE_BREAK.finditer(prefix))
        context_start = cursor + (breaks[-1].end() if breaks else 0)
        gap = text[context_start:start]
        if _CONTEXT.fullmatch(_mask(gap, find_dates(gap, base))):
            start = context_start
        dates = find_dates(text[:anchor.start()], base)
        out.append({
            'date': dates[-1]['date'] if dates else find_date('', base)['date'],
            'product_name': name, 'qty': qty, 'unit': anchor.group(2),
            'unit_price': unit_price, 'amount': amount,
            'start': start, 'end': end, **sales_dimensions(text[start:end]),
        })
        cursor = end
    return out


# ---------------------------------------------------------------- 单句


def analyze_clause(clause: str, base: _dt.date | None = None,
                   fallback_date: str | None = None) -> dict:
    """解析一个片段。

    fallback_date 用于按类型切开后的后半段 —— 「昨天营业额1200成本350」
    切开后「成本350」自己不带日期，要继承前半段的「昨天」。
    """
    date_info = find_date(clause, base)
    date_spans = [date_info["span"]] if date_info["span"] else []
    date_value = date_info["date"] if date_info["span"] else (fallback_date or date_info["date"])
    periods = find_periods(_mask(clause, date_spans), _dt.date.fromisoformat(date_value))
    if any(p['period_month'] is None for p in periods) or len(periods) > 1:
        return {'date': date_value, 'entries': []}
    date_spans += periods
    work = _mask(clause, date_spans)

    quantities = find_quantities(work)
    work = _mask(work, quantities)

    amounts = find_amounts(work)
    if not amounts:
        return {"date": date_value, "entries": []}

    keyword = classify(clause)
    # 一个锚点最多认领一个金额；有后置金额取第一个，否则取紧邻的前置金额。
    # 「300元交房租8000」不把300猜成另一笔；「房租300元8000」也不复制分类。
    following = [a for a in amounts if keyword and a['start'] >= keyword['end']]
    chosen = following[0] if following else (amounts[-1] if keyword else amounts[0])
    unresolved = [a for a in amounts if a is not chosen]
    leftovers = [clause[a['start']:a['end']].strip() for a in unresolved]
    amounts = [chosen]
    type_ = keyword["type"] if keyword else TYPE_EXPENSE
    category = keyword["category"] if keyword else "其他支出"

    if type_ == TYPE_REVENUE:
        category = "营业额"
    elif type_ == TYPE_PURCHASE:
        category = "食材成本"

    item = _build_item(clause, date_spans + quantities + amounts + unresolved + find_keywords(clause))
    if len(item) < 2:
        # 残留只剩「交」这类动词时，用命中的分类词更贴切；
        # 完全没残留（整句都是关键词）就交给默认值，别把「卖了」当摘要。
        item = keyword["word"] if (item and keyword and keyword["category"] != "其他支出") else ""
    qty = quantities[0]["value"] if quantities else None
    unit = quantities[0]["unit"] if quantities else ""

    entries = []
    for amount in amounts:
        entries.append({
            "date": date_value,
            "type": type_,
            "category": category,
            "amount": amount["value"],
            "item": item or _default_item(type_, category),
            # 数量只对进货有意义；营业额记「卖了1280」时数量不是成本单位
            "qty": qty if type_ == TYPE_PURCHASE else None,
            "unit": unit if type_ == TYPE_PURCHASE else "",
            "note": clause,
            "period_month": periods[0]['period_month'] if periods and type_ == TYPE_EXPENSE else None,
            **sales_dimensions(clause),
            'granularity': 'daily' if re.search(r'营业额|销售额|营收|流水|总额', clause) else 'transaction',
        })
    return {"date": date_value, "entries": entries, "leftovers": leftovers}


# ---------------------------------------------------------------- 查询意图


def detect_query(text: str) -> str | None:
    if not _QUERY_RE.search(text):
        return None
    if re.search(r"(这个月|本月|这月)", text):
        return "month"
    if re.search(r"(这周|本周|这星期|最近7天|近7天|近一周|一周)", text):
        return "week"
    if re.search(r"(昨天|昨日)", text):
        return "yesterday"
    if re.search(r"(这个产品|单品|成本|配方|原料)", text):
        return "cost"
    return "today"


# ---------------------------------------------------------------- 对外


def parse(text: str, base: _dt.date | None = None, extra_mask=None) -> dict:
    """解析一句口语。永远返回 dict，认不出时 intent='unknown'。

    先把销量句摘出去（「卖了30份」是销量不是支出），剩下的再当账目解析。

    extra_mask 是观测层已经认领的片段（客流 / 天气 / 排班…）。
    必须在找金额之前遮掉，否则「评分4.8」里的 4.8 没有单位，
    会被 find_amounts 当成 4.8 元记成一笔支出 —— 和「客流120人」
    记成 120 元是同一类错误：不是记不下，是记错了，账面上看不出来。

    entries / sales 在任何分支都存在（认不出时是空列表），
    调用方不必再 .get() 兜底。
    """
    text = str(text or "").strip()
    if not text:
        return {"intent": "unknown", "entries": [], "sales": [], "raw": text}

    # 先验证原文所有显式日期，不能遮掉非法日期后退回今天或只取日号。
    find_dates(text, base)
    # 非金额观测先认领，避免其数字或主体又被销量规则重复识别。
    masked = _mask(text, extra_mask or [])
    sales = find_sales(masked, base)
    unresolved_sales = []
    for index, sale in enumerate(sales):
        if sale['amount'] is None:
            continue
        cursor = sale['end']
        limit = sales[index + 1]['start'] if index + 1 < len(sales) else len(masked)
        while True:
            extra = _sale_payment(masked, cursor, limit, base)
            if not extra:
                break
            # 单价和合计同时出现时不静默覆盖；额外金额也不能兜底成支出。
            unresolved_sales.append({'start': cursor, 'end': extra[1]})
            cursor = extra[1]
    work_text = _mask(masked, sales + unresolved_sales)
    # 销量遮蔽不能吃掉日期的后续作用域；保留等长原位日期供账目顺序继承。
    for date in find_dates(masked, base):
        work_text = work_text[:date['start']] + masked[date['start']:date['end']] + work_text[date['end']:]

    entries = []
    leftovers = [text[s['start']:s['end']].strip(' ，,、') for s in unresolved_sales]
    inherited_date = find_date('', base)['date']
    for clause in split_clauses(work_text):
        clause_date = inherited_date
        for segment in split_by_type_switch(clause, base):
            result = analyze_clause(segment, base, fallback_date=clause_date)
            clause_date = inherited_date = result['date']
            if extra_mask is not None:
                # 分层入口不把任意数字猜成支出。至少需要金额单位或明确记账动词。
                keyword = classify(segment)
                explicit_money = re.search(r"(?:\d|[一二两三四五六七八九十百千])\s*(?:块|元)", segment)
                if not keyword and not explicit_money:
                    result["entries"] = []
            if result["entries"]:
                entries.extend(result["entries"])
                leftovers.extend(result.get('leftovers', []))
            elif not _is_noise(segment, base):
                # 全被遮掉的片段（「今天下雨」遮掉「下雨」后只剩「今天」）
                # 不是「没看懂」，不该混进没看懂的清单里
                leftovers.append(segment)

    if entries or sales:
        # 有金额但一条关键词都没命中时，类型是「其他支出」的兜底，可信度低
        confident = all(e["category"] != "其他支出" for e in entries) if entries else True
        return {
            "intent": "entries" if entries else "sales",
            "date": entries[0]["date"] if entries else sales[0]["date"],
            "entries": entries,
            "sales": sales,
            "leftovers": leftovers,
            "confidence": 0.9 if confident else 0.5,
            "raw": text,
        }

    period = detect_query(text)
    if period:
        return {"intent": "query", "period": period, "entries": [], "sales": [],
                "leftovers": leftovers, "raw": text}

    return {"intent": "unknown", "entries": [], "sales": [],
            "leftovers": leftovers, "raw": text}
