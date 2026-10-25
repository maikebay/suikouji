"""结构化补充与修正：原话保留，同一请求只改一次原记录。

老板先随口记一句，事后补数量、补平台、改金额时走这里：

- 补充/修正都写回**原来那行账目**，不新增一笔，避免重复入账；
- 每次改动本身留一条原话（`source='review'`），记下改的是哪一行、
  改了哪些字段、依据是什么，原话和旧值都能翻出来；
- 同一个 `request_id` 重复提交只生效一次，网络重试不会改两遍。
"""
from __future__ import annotations

import json
import math
import sqlite3

from .model import (TYPE_EXPENSE, TYPE_PURCHASE, TYPE_REVENUE,
                    month_range, parse_date, today_iso)

ENTRY_FIELDS = {'date', 'type', 'category', 'amount', 'item', 'qty', 'unit',
                'period_month', 'platform', 'channel', 'amount_basis', 'granularity'}
SALE_FIELDS = {'date', 'product_name', 'qty', 'amount', 'platform', 'channel',
               'product_category', 'unit', 'amount_basis', 'granularity'}
TYPES = (TYPE_REVENUE, TYPE_PURCHASE, TYPE_EXPENSE)

# 改一个字段时被顺带改掉的字段（商品名归一 → 挂上规范商品与它的品类），
# 也要留档，否则「改了什么」只答对一半。
_SIDE_EFFECTS = ('product_id', 'product_category')


class _AlreadyApplied(Exception):
    """同一请求号在查重之后、插入之前被别人抢先用了，这次要整个回滚。"""


def review_items(store, shop_id):
    """待核对清单：只列会影响算账、且老板补一句就能定下来的缺口。"""
    items = []
    for row in store.list_entries(shop_id, '0000', '9999'):
        reasons, missing = [], []
        if row['type'] == TYPE_PURCHASE and not row.get('qty'):
            reasons.append('采购还缺数量，暂时算不出原料单价')
            missing += ['qty', 'unit']
        if row['type'] == TYPE_EXPENSE and not row.get('period_month'):
            reasons.append('费用还缺归属月份')
            missing.append('period_month')
        if reasons:
            items.append(_item(store, shop_id, 'entry', row, reasons, missing))
    for row in store.list_sales(shop_id, '0000', '9999'):
        reasons, missing = [], []
        if row.get('amount') is None:
            reasons.append('销量还缺金额')
            missing.append('amount')
        if not row.get('platform'):
            reasons.append('销量还缺平台')
            missing.append('platform')
        if reasons:
            items.append(_item(store, shop_id, 'sale', row, reasons, missing))
    return items


def _history_rows(rows):
    out = []
    for row in rows:
        try:
            detail = json.loads(row.get('correction_detail') or '{}')
        except ValueError:
            detail = {}
        out.append({'id': row['id'], 'raw_text': row['raw_text'],
                    'created_at': row['created_at'],
                    'before': detail.get('before') or {},
                    'after': detail.get('after') or {},
                    'side_effects': detail.get('side_effects') or {}})
    return out


def correction_history(store, shop_id, target_id):
    """这行账目上改过几次、改了什么。原始原话不在其中，单独按来源展示。"""
    return _history_rows(store.corrections_for(shop_id, target_id))


def corrections_map(store, shop_id):
    """被改过的行 → 修正历史，一次取全。

    原话列表只显示最近 100 条，从账目明细点「修正」不能靠它算历史，
    否则同一行在待核对里是「改过 1 次」、在账目明细里是「改过 0 次」。
    """
    return {target: _history_rows(rows)
            for target, rows in store.corrections_map(shop_id).items()}


def _item(store, shop_id, kind, row, reasons, missing):
    label = row.get('item') or row.get('product_name') or row.get('category') or '（未写名称）'
    target_id = f"{kind}:{row['id']}"
    return {'id': target_id, 'kind': kind, 'date': row['date'], 'label': label,
            'amount': row.get('amount'), 'qty': row.get('qty'), 'unit': row.get('unit') or '',
            'reasons': reasons, 'missing': missing, 'fields': _editable(kind, row),
            'history': correction_history(store, shop_id, target_id)}


def _editable(kind, row):
    fields = ENTRY_FIELDS if kind == 'entry' else SALE_FIELDS
    return {k: row.get(k) for k in sorted(fields)}


def apply_review(store, shop_id, request_id, target_id, fields, reason):
    """把补充或修正写回原账目。返回 duplicate=True 表示这次请求此前已经生效。"""
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('缺少修正编号，请刷新后重试')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError('请填写 1 到 500 字的修正依据')
    kind, _, row_id = str(target_id or '').partition(':')
    if kind not in ('entry', 'sale') or not row_id:
        raise ValueError('要修正的记录不存在')
    original = store.get_entry(row_id) if kind == 'entry' else store.get_sale(row_id)
    if not original or original['shop_id'] != shop_id:
        raise ValueError('要修正的记录不属于当前账本')
    clean = _validate(kind, fields)
    if not clean:
        raise ValueError('没有需要保存的修正')
    dedup = f'review:{request_id}'
    # 去重要排在「内容是否相同」之前：响应丢了原样重发时，改的内容和库里已经一样，
    # 那不是「没改动」，是这次请求早就生效了，要按重复告诉用户，不能报错。
    existing = store.record_find(shop_id, dedup)
    if existing:
        return {'duplicate': True, 'record': existing, 'target_id': target_id, 'changed': {}}
    before = _editable(kind, original)
    changed = {k: v for k, v in clean.items() if before.get(k) != v}
    if not changed:
        raise ValueError('修正后的内容与当前记录相同')

    source = store.record_for_origin(shop_id, target_id)
    try:
        with store.transaction():
            if kind == 'entry':
                store.update_entry(row_id, **changed)
            else:
                store.update_sale(row_id, **changed)
            # 留档要写库里真正落下的值：商品名会被别名归一成规范名，product_id 与
            # product_category 也跟着改写，按输入记下来的 after 在库里并不存在。
            written = store.get_entry(row_id) if kind == 'entry' else store.get_sale(row_id)
            after = {k: written.get(k) for k in changed}
            side = {k: {'before': original.get(k), 'after': written.get(k)}
                    for k in _SIDE_EFFECTS if original.get(k) != written.get(k)}
            detail = json.dumps({'before': {k: before.get(k) for k in changed},
                                 'after': after, 'side_effects': side,
                                 'source_record_id': source['id'] if source else None},
                                ensure_ascii=False)
            record = store.record_add(shop_id, reason.strip(), source='review', dedup_ref=dedup,
                                      occurred_at=today_iso(), parse_status='correction',
                                      parser_version='review-1', correction_of=target_id,
                                      correction_detail=detail)
            if record.get('duplicate'):
                # 另一个请求在查重之后、插入之前抢用了同一编号：它已经改过这行，
                # 抛出去把这次的字段更新一并回滚，同一编号只生效一次。
                raise _AlreadyApplied()
    except _AlreadyApplied:
        return {'duplicate': True, 'record': record, 'target_id': target_id, 'changed': {}}
    except sqlite3.IntegrityError:
        # 同一请求号并发到达：唯一索引挡下第二次，按重复处理，不改两遍。
        existing = store.record_find(shop_id, dedup)
        if existing:
            return {'duplicate': True, 'record': existing, 'target_id': target_id, 'changed': {}}
        raise
    return {'duplicate': False, 'record': record, 'target_id': target_id, 'changed': after,
            'history': correction_history(store, shop_id, target_id)}


def _validate(kind, fields):
    if not isinstance(fields, dict):
        raise ValueError('修正内容格式不正确')
    allowed = ENTRY_FIELDS if kind == 'entry' else SALE_FIELDS
    # 不能清空的列：entry.amount、sale.qty 是 NOT NULL。前端空输入框会映射成 null，
    # 写下去会撞完整性约束、变成一次 500，所以在入口就挡掉。
    required = {'amount'} if kind == 'entry' else {'qty'}
    out = {}
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f'不能修正字段：{key}')
        if key in ('amount', 'qty'):
            if value in (None, ''):
                if key in required:
                    raise ValueError('金额和数量不能留空')
                out[key] = None
                continue
            if isinstance(value, bool):
                # true/false 会被 float() 悄悄当成 1.0 写进账目。
                raise ValueError('金额和数量要填数字')
            try:
                number = float(value)
            except OverflowError:
                raise ValueError('金额和数量填得太大，请检查')
            except (TypeError, ValueError):
                raise ValueError('金额和数量要填数字')
            if not math.isfinite(number):
                raise ValueError('金额和数量填得太大，请检查')
            if number < 0:
                raise ValueError('金额和数量必须是非负数')
            out[key] = number
        elif key == 'date':
            date = parse_date(str(value or ''))
            if not str(value or '') or date != str(value or ''):
                raise ValueError('日期格式为 YYYY-MM-DD')
            out[key] = date
        elif key == 'period_month':
            if value in (None, ''):
                out[key] = None
                continue
            if len(str(value)) != 7:
                raise ValueError('费用归属月格式为 YYYY-MM')
            month_range(str(value))
            out[key] = str(value)
        elif key == 'type':
            if value not in TYPES:
                raise ValueError('账目类型只能是收入、采购或支出')
            out[key] = value
        elif key in ('amount_basis', 'granularity'):
            choices = ('gross', 'settlement') if key == 'amount_basis' else ('daily', 'transaction')
            if value not in choices:
                raise ValueError('金额口径或记录粒度无效')
            out[key] = value
        else:
            if not isinstance(value, str) or len(value) > 100:
                raise ValueError('文字字段最长 100 字')
            out[key] = value.strip()
    return out
