"""本地报表：现金收支、归属月费用、标准原料估算分别展示。"""
from __future__ import annotations

import datetime as _dt

from .model import fmt_money, fmt_qty, month_of, month_range
from .accounting import sales_analysis


def period_report(store, shop_id, start, end, label):
    s = store.summary(shop_id, start, end)
    if not s['count'] and not s['accrued_expense']:
        return f'{label}（{start} ~ {end}）还没有记录。'
    basis = '结算实收（已扣平台费用）' if s['amount_basis'] == 'settlement' else '营业额'
    suffix = '（包含按当前售价推算）' if s['estimated_revenue'] else ''
    lines = [f'**{label}**　{start} ~ {end}', '',
             f"- {basis}：{fmt_money(s['revenue'])} 元{suffix}",
             f"- 采购付款：{fmt_money(s['purchase'])} 元",
             f"- 费用付款：{fmt_money(s['expense'])} 元",
             f"- 已记收支差额：{fmt_money(s['cash_balance'])} 元（不是利润；结算收入不再次扣平台费）",
             f"- 所选月份归属费用：{fmt_money(s['accrued_expense'])} 元（整月，不按天分摊）",
             f"- 已记销量的标准原料成本估算：{fmt_money(s['standard_consumption'])} 元",
             '- 实际耗料成本、实际利润、毛利率及保本点：尚未核算，需库存计价与完整费用。',
             '', '| 归属月份 | 费用分类 | 金额 |', '| --- | --- | --- |']
    for row in s['expense_by_month']:
        lines.append(f"| {row['month']} | {row['category']} | {fmt_money(row['amount'])} |")
    lines.extend('- ' + warning for warning in s['warnings'])
    for day in s['daily_revenue']:
        for warning in day['warnings']:
            lines.append(f"- {day['date']}：{warning}")
    lines += ['', s['cost_note']]
    return '\n'.join(lines)


def day_report(store, shop_id, date_iso):
    return period_report(store, shop_id, date_iso, date_iso, date_iso)


def today_report(store, shop_id, date_iso):
    return day_report(store, shop_id, date_iso)


def week_range(today=None, days=7):
    base = _dt.date.fromisoformat(today) if today else _dt.date.today()
    return (base - _dt.timedelta(days=days - 1)).isoformat(), base.isoformat()


def month_report(store, shop_id, month=None):
    month = month or month_of(_dt.date.today().isoformat())
    start, end = month_range(month)
    return period_report(store, shop_id, start, end, f'{month} 月度')


def product_report(store, shop_id, start, end):
    rows = sales_analysis(store, shop_id, start, end)
    if not rows:
        return f'{start} ~ {end} 没有销售明细。'
    lines = [f'**单品标准原料成本估算**　{start} ~ {end}', '',
             '| 商品 / 品类 | 平台 / 渠道 | 销量 | 金额 | 标准原料成本 | 材料毛利估算 |',
             '| --- | --- | --- | --- | --- | --- |']
    for r in rows:
        suffix = '（按售价推算）' if r['estimated'] else ''
        basis = '（结算实收）' if r['amount_basis'] == 'settlement' else ''
        lines.append(f"| {r['product_name']} / {r['product_category'] or '未分类'} | "
                     f"{r['platform'] or '未注明'} / {r['channel'] or '未注明'} | {fmt_qty(r['qty'])} | "
                     f"{fmt_money(r['amount'])}{suffix}{basis} | {fmt_money(r['standard_cost'])} | "
                     f"{fmt_money(r['standard_gross'])} |")
    lines += ['', '— 表示数据不足或金额口径不适用。标准估算使用销售当日配方及最近进价，'
              '不包含库存实际耗用、损耗、租金人工分摊；结算实收不用于材料毛利计算。']
    return '\n'.join(lines)
