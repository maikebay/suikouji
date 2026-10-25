"""随口记 · 表格导入。

别的系统导出的表，列名和口径都不统一。这里做两件事：
1. 认表头，猜出这是「流水账」还是「销售明细」，并给出列映射；
2. 归一化每行（金额、日期、类型），把认不出的行单独列出来。

映射结果会回给用户确认，不闷头入库 —— 认错列比不入库更糟。
"""
from __future__ import annotations

import re
import hashlib
import json
import math
from collections import Counter

from .model import (
    SOURCE_IMPORT,
    TYPE_EXPENSE,
    TYPE_PURCHASE,
    TYPE_REVENUE,
    parse_date,
    round2,
)
from .sheet import read_table, to_records

# 表头别名。顺序无所谓，命中即用；越靠前越优先。
COLUMN_ALIASES = {
    "date": ["日期", "交易日期", "营业日期", "订单日期", "时间", "交易时间", "date", "记账日期"],
    "amount": ["金额", "金额(元)", "金额（元）", "实收金额", "实收", "销售额", "营业额",
               "收入", "支出", "合计", "小计", "总额", "总价", "应付", "金额(元)",
               "amount", "金额(元/分)"],
    "type": ["类型", "收支", "收/支", "收支类型", "方向", "交易类型", "科目类型"],
    "category": ["分类", "类别", "科目", "费用类型", "费用类别", "项目分类", "支出类型"],
    "item": ["商品", "商品名称", "品名", "菜品", "商品名", "货品", "产品", "摘要",
             "项目", "名称", "item", "product"],
    "qty": ["数量", "销量", "份数", "销售数量", "数量(份)", "数量（份）", "件数", "qty", "count"],
    "unit": ["单位", "计量单位", "unit"],
    "note": ["备注", "说明", "注释", "note", "remark"],
    'platform': ['平台', '销售平台'], 'channel': ['渠道', '履约方式'],
    'product_category': ['商品品类', '品类', '商品分类'],
    'period_month': ['费用归属月份', '归属月份', '费用月份'],
    'external_id': ['明细ID', '交易ID', '流水号', '订单明细号'],
    'source_system': ['来源系统', '系统'],
    'amount_basis': ['金额口径'], 'granularity': ['数据粒度', '粒度'],
}

_KIND_SALES = ["商品", "商品名称", "品名", "菜品", "商品名", "货品", "产品"]
_KIND_QTY = ["数量", "销量", "份数", "销售数量", "件数"]

_TYPE_MAP = [
    (TYPE_REVENUE, ["收入", "收", "营业额", "销售额", "营收", "进账", "收款", "卖"]),
    (TYPE_PURCHASE, ["进货", "采购", "成本", "原料", "食材", "货款", "拿货"]),
    (TYPE_EXPENSE, ["支出", "支", "费用", "开销", "付款"]),
]


def _norm_header(h: str) -> str:
    return re.sub(r"[\s_\-（）()【】\[\]]", "", str(h or "")).lower()


def _find_column(headers: list, key: str) -> str | None:
    """按别名找列。先精确匹配，再包含匹配，避免「金额」抢到「金额备注」。"""
    norm = {h: _norm_header(h) for h in headers}
    for alias in COLUMN_ALIASES[key]:
        a = _norm_header(alias)
        for h, nh in norm.items():
            if nh == a:
                return h
    for alias in COLUMN_ALIASES[key]:
        a = _norm_header(alias)
        for h, nh in norm.items():
            if a and a in nh:
                return h
    return None


_NUM_RE = re.compile(r"[-+]?[\d,]*\.?\d+")


def parse_number(text) -> float | None:
    """把 '¥1,234.50'、'1234元'、'(123)' 解析成数字。认不出返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    m = _NUM_RE.search(s.replace(",", ""))
    if not m:
        return None
    try:
        value = float(m.group(0))
    except ValueError:
        return None
    if negative:
        value = -value
    return value if math.isfinite(value) else None


def _norm_type(text: str, amount: float | None) -> str | None:
    s = str(text or "").strip()
    for type_, words in _TYPE_MAP:
        for w in words:
            if w and w in s:
                return type_
    # 没有类型列时，用正负号兜底：负数支出，正数收入
    if amount is not None:
        return TYPE_REVENUE if amount >= 0 else TYPE_EXPENSE
    return None


def _norm_date(text: str) -> str | None:
    s = str(text or "").strip()
    if not s:
        return None
    d = parse_date(s)
    if d:
        return d
    m = re.match(r"^(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})", s)
    if m:
        return parse_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return None


def plan_import(path: str, header_row: int = 0) -> dict:
    """读表并给出导入方案（不入库）。"""
    rows = read_table(path)
    if not rows:
        raise ValueError("表格是空的")
    records, headers = to_records(rows, header_row)
    if not records:
        raise ValueError("表格只有表头，没有数据行")

    norm_headers = [_norm_header(h) for h in headers]
    has_item = any(any(_norm_header(k) == nh for k in _KIND_SALES) for nh in norm_headers)
    has_qty = any(any(_norm_header(k) == nh for k in _KIND_QTY) for nh in norm_headers)

    mapping = {k: _find_column(headers, k) for k in COLUMN_ALIASES}

    if has_item and has_qty and not mapping['type']:
        kind = "sales"
    elif mapping["amount"]:
        kind = "entries"
    else:
        raise ValueError(
            "认不出这张表：既没有「商品+数量」的销售明细列，也没有可用的金额列。"
            f"实际表头：{headers}"
        )

    parsed, skipped = [], []
    for i, rec in enumerate(records, start=header_row + 2):
        try:
            row = _parse_sale_row(rec, mapping) if kind == 'sales' else _parse_entry_row(rec, mapping)
            if row is None:
                raise ValueError('缺日期、商品、数量或金额；负销量/退款暂不支持')
            extra = {key: str(rec.get(mapping[key] or '', '') or '').strip()
                     for key in ('platform', 'channel', 'period_month', 'external_id', 'source_system')}
            extra['channel'] = '配送' if extra['channel'] == '外卖' else extra['channel']
            row.update(extra)
            basis = str(rec.get(mapping['amount_basis'] or '', '') or '').strip()
            row['amount_basis'] = {'商品销售额': 'gross', '销售额': 'gross', '结算实收': 'settlement'}.get(basis, basis) or (
                'settlement' if '实收' in (mapping['amount'] or '') else 'gross')
            if row['amount_basis'] not in ('gross', 'settlement'):
                raise ValueError('金额口径需填写商品销售额或结算实收')
            entry_granularity = str(rec.get(mapping['granularity'] or '', '') or '').strip()
            row['granularity'] = {'日汇总': 'daily', '交易明细': 'transaction'}.get(entry_granularity, entry_granularity) or (
                'transaction' if row['external_id'] else 'daily')
            if row['granularity'] not in ('daily', 'transaction'):
                raise ValueError('数据粒度需填写日汇总或交易明细')
            if kind == 'sales':
                row['product_category'] = str(rec.get(mapping['product_category'] or mapping['category'] or '', '') or '').strip()
                granularity = str(rec.get(mapping['granularity'] or '', '') or '').strip()
                row['granularity'] = {'日汇总': 'daily', '交易明细': 'transaction'}.get(granularity, granularity) or (
                    'transaction' if row['external_id'] else 'daily')
                if row['granularity'] not in ('daily', 'transaction'):
                    raise ValueError('销售粒度需填写日汇总或交易明细')
                if row['granularity'] == 'transaction' and not row['external_id']:
                    raise ValueError('交易明细需要唯一明细ID（不能仅用订单号）')
            if row['period_month']:
                from .model import month_range
                if not re.fullmatch(r'\d{4}-\d{2}', row['period_month']):
                    raise ValueError('费用归属月份格式为 YYYY-MM')
                month_range(row['period_month'])
            row['raw_text'] = json.dumps(rec, ensure_ascii=False)
            parsed.append(row)
        except ValueError as exc:
            skipped.append({'row': i, 'raw': rec, 'reason': str(exc)})

    return {
        "kind": kind,
        "headers": headers,
        "mapping": mapping,
        "rows": parsed,
        "skipped": skipped,
        "row_count": len(parsed),
        "source_rows": len(records),
    }


def _parse_entry_row(rec: dict, mapping: dict) -> dict | None:
    date = _norm_date(rec.get(mapping["date"] or "", ""))
    amount = parse_number(rec.get(mapping["amount"] or "", ""))
    if not date or amount is None:
        return None
    type_ = _norm_type(rec.get(mapping["type"] or "", ""), amount)
    category = str(rec.get(mapping["category"] or "", "") or "").strip()
    item = str(rec.get(mapping["item"] or "", "") or "").strip()
    qty = parse_number(rec.get(mapping["qty"] or "", ""))
    unit = str(rec.get(mapping["unit"] or "", "") or "").strip()

    if type_ is None:
        type_ = TYPE_EXPENSE
    if not category:
        category = {"revenue": "营业额", "purchase": "食材成本"}.get(type_, "其他支出")
    return {
        "date": date, "type": type_, "category": category,
        "amount": abs(amount), "item": item or None,
        "qty": abs(qty) if qty else None, "unit": unit,
        "note": str(rec.get(mapping["note"] or "", "") or "").strip() or None,
    }


def _parse_sale_row(rec: dict, mapping: dict) -> dict | None:
    name = str(rec.get(mapping["item"] or "", "") or "").strip()
    qty = parse_number(rec.get(mapping["qty"] or "", ""))
    date = _norm_date(rec.get(mapping['date'] or '', ''))
    amount = parse_number(rec.get(mapping['amount'] or '', ''))
    if not name or qty is None or qty < 0 or not date or (amount is not None and amount < 0):
        return None
    return {'date': date, 'product_name': name, 'qty': qty,
            'unit': str(rec.get(mapping['unit'] or '', '') or ''),
            'amount': round2(amount) if amount is not None else None}


def commit_import(store, shop_id: str, plan: dict, filename: str, sha256: str) -> dict:
    """整批原子提交；日汇总按范围去重，交易按来源明细ID去重。冲突整批退回。"""
    with store.transaction():
        dup = store.batch_find_by_hash(shop_id, sha256)
        if dup:
            return {'duplicate': True, 'batch': dup, 'imported': 0}
        batch = store.batch_add(shop_id, filename, sha256, plan['kind'], 0)
        imported = duplicates = 0
        occurrences = Counter()
        for raw in plan['rows']:
            row = dict(raw)
            if plan['kind'] == 'sales':
                canonical = store.product_find(shop_id, row['product_name'])
                if canonical:
                    row['product_name'] = canonical['name']
            content = {k: v for k, v in row.items() if k != 'raw_text'}
            digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if row.get('external_id'):
                identity = [plan['kind'], row.get('source_system', ''), row.get('platform', ''), row['external_id']]
            elif plan['kind'] == 'sales':
                identity = ['daily', row['date'], row.get('platform', ''), row.get('channel', ''), row['product_name']]
            else:
                # 金额变化应命中同一业务身份；同一文件中的合法同身份多笔
                # 用序号区分。金额仍保留在 fingerprint 中用于冲突检测。
                stable = {k: v for k, v in content.items() if k != 'amount'}
                stable_digest = hashlib.sha256(
                    json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                occurrences[stable_digest] += 1
                identity = ['entry', stable_digest, occurrences[stable_digest]]
            key = json.dumps(identity, ensure_ascii=False)
            existing = store.import_identity_get(shop_id, key)
            if existing:
                if existing['fingerprint'] != digest:
                    raise ValueError('同一来源明细或日汇总数值发生变化，请核对原记录后导入')
                duplicates += 1
                continue
            if plan['kind'] == 'sales':
                for old in store.list_sales(shop_id, row['date'], row['date']):
                    if old['product_name'] != row['product_name']:
                        continue
                    a = (old['platform'], old['channel'])
                    b = (row.get('platform', ''), row.get('channel', ''))
                    overlaps = not any(x and y and x != y for x, y in zip(a, b))
                    if overlaps and (old['granularity'] == 'daily' or row.get('granularity', 'daily') == 'daily'):
                        raise ValueError('销售范围与已有记录重叠，不能把日汇总和交易明细重复导入')
                product = store.product_upsert(shop_id, row['product_name'], category=row.get('product_category') or None)
                row['product_id'] = product['id']
                store.add_sales(shop_id, [row], source=SOURCE_IMPORT, batch_id=batch['id'])
            else:
                store.add_entries([{**row, 'shop_id': shop_id}], source=SOURCE_IMPORT, batch_id=batch['id'])
            store.import_identity_add(shop_id, key, digest, batch['id'])
            imported += 1
        store.batch_set_rows(batch['id'], imported)
        batch['rows'] = imported
        return {'duplicate': False, 'batch': batch, 'imported': imported,
                'duplicates': duplicates, 'kind': plan['kind']}
