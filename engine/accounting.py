"""同范围收入择一核对；采购现金流与配方标准耗料分别计算。"""
from collections import defaultdict

from .cost import product_cost
from .model import round2, sale_amount, FIXED_CATEGORIES


def selected_sales(rows):
    """保留原始行，汇总时宽范围日总量覆盖细分；交叉不明的范围留待核对。"""
    groups = defaultdict(list)
    for row in rows:
        groups[(row['date'], row['product_name'])].append(row)
    selected, warnings = [], []
    for (date, product), group in groups.items():
        daily = {}
        for row in group:
            if row['granularity'] != 'daily':
                continue
            scope = (row['platform'], row['channel'])
            if scope in daily:
                warnings.append({'date': date, 'message': row['product_name'] + ' 同范围多条日销量，采用最后一条'})
            daily[scope] = row
        roots = {scope: row for scope, row in daily.items()
                 if not any(other != scope and _covers(other, scope) for other in daily)}
        ambiguous = any(not any(x and y and x != y for x, y in zip(a, b))
                        for a in roots for b in roots if a != b)
        candidates = list(roots.values())
        for row in group:
            scope = (row['platform'], row['channel'])
            if row['granularity'] == 'daily':
                if scope not in roots:
                    warnings.append({'date': date, 'message': row['product_name'] + ' 的细分销量已包含在宽范围日汇总中，不重复累计'})
                continue
            if any(_covers(r, scope) for r in roots):
                continue
            if any(not any(x and y and x != y for x, y in zip(scope, r)) for r in roots):
                ambiguous = True
            candidates.append(row)
        if ambiguous:
            warnings.append({'date': date, 'message': group[0]['product_name'] + ' 销量范围交叉，金额和成本暂不合计', 'incomplete': True})
        else:
            selected.extend(candidates)
    return selected, warnings


def sales_analysis(store, shop_id, start, end):
    groups = {}
    sales, warnings = selected_sales(store.list_sales(shop_id, start, end))
    for sale in sales:
        product = store.product_find(shop_id, sale['product_name'])
        amount, estimated = sale_amount(sale, product)
        cost = product_cost(store, shop_id, product, as_of=sale['date']) if product else None
        key = (sale['product_name'], sale['platform'], sale['channel'],
               sale['product_category'], sale['amount_basis'])
        group = groups.setdefault(key, dict(zip(
            ('product_name', 'platform', 'channel', 'product_category', 'amount_basis'), key),
            qty=0, amount=0, known_amount=0, estimated=False, missing_amount=False,
            standard_cost=0, cost_complete=True))
        group['qty'] += sale['qty']
        group['known_amount'] += sale['amount'] or 0
        group['estimated'] |= estimated
        group['missing_amount'] |= amount is None
        group['amount'] += amount or 0
        if cost and cost['complete']:
            group['standard_cost'] += sale['qty'] * cost['unit_cost']
        else:
            group['cost_complete'] = False
    for group in groups.values():
        group['amount'] = None if group['missing_amount'] else round2(group['amount'])
        group['known_amount'] = round2(group['known_amount'])
        group['standard_cost'] = round2(group['standard_cost']) if group['cost_complete'] else None
        group['standard_gross'] = (round2(group['amount'] - group['standard_cost'])
            if group['amount'] is not None and group['standard_cost'] is not None
            and group['amount_basis'] == 'gross' else None)
    return list(groups.values())


def _covers(a, b):
    return all(not x or x == y for x, y in zip(a, b))


def revenue_days(entries, sales, store, shop_id):
    days = sorted({r['date'] for r in entries + sales})
    sales, sale_warnings = selected_sales(sales)
    out = []
    for day in days:
        totals, transactions = {}, {}
        warnings = [w['message'] for w in sale_warnings if w['date'] == day]
        for e in entries:
            if e['date'] != day:
                continue
            scope = (e['platform'], e['channel'])
            if e.get('granularity') == 'transaction':
                item = transactions.setdefault(scope, {**e, 'amount': 0})
                item['amount'] += e['amount']
                if item['amount_basis'] != e['amount_basis']:
                    item['amount_basis'] = 'mixed'
                continue
            if scope in totals:
                warnings.append('同范围多条营业总额，采用最后录入的一条，请核对')
            totals[scope] = e
        for scope, entry in transactions.items():
            totals.setdefault(scope, entry)
        # 宽范围的总额覆盖窄范围，防止全店＋平台＋销售明细重复相加。
        roots = {scope: e for scope, e in totals.items()
                 if not any(other != scope and _covers(other, scope) for other in totals)}
        conflict = any(not any(x and y and x != y for x, y in zip(a, b))
                       for a in roots for b in roots if a != b)
        amount = sum(e['amount'] for e in roots.values())
        bases = {e['amount_basis'] for e in roots.values()}
        estimated = False
        complete = not conflict and not any(w.get('incomplete') and w['date'] == day for w in sale_warnings)
        if conflict:
            warnings.append('营业总额范围交叉，需核对平台与渠道后才能汇总')
        comparisons = {scope: {'declared': e['amount'], 'detail': 0, 'has_detail': False,
                               'complete': True, 'estimated': False, 'basis': e['amount_basis']}
                       for scope, e in roots.items()}
        for sale in sales:
            if sale['date'] != day:
                continue
            value, est = sale_amount(sale, store.product_find(shop_id, sale['product_name']))
            scope = (sale['platform'], sale['channel'])
            covered = [r for r in roots if _covers(r, scope)]
            if covered:
                for r in covered:
                    check = comparisons[r]
                    check['detail'] += value or 0
                    check['has_detail'] = True
                    check['complete'] &= value is not None and sale['amount_basis'] == check['basis']
                    check['estimated'] |= est
                continue
            # 未标平台的明细可能与已有平台总额重叠，不能当额外收入。
            if any(_covers(scope, r) or not any(x and y and x != y for x, y in zip(scope, r)) for r in roots):
                complete = False
                warnings.append('销售明细与营业总额范围不明确，暂不合计')
                continue
            amount += value or 0
            complete &= value is not None
            estimated |= est
            bases.add(sale['amount_basis'])
        for check in comparisons.values():
            if check['has_detail']:
                check['difference'] = round2(check['declared'] - check['detail']) if check['complete'] else None
                if check['difference'] is not None and abs(check['difference']) > .01:
                    warnings.append('营业总额与销售明细存在差额（可能明细不全），收入采用总额')
        if len(bases) > 1 or 'mixed' in bases:
            complete = False
            warnings.append('商品销售额与结算实收口径不同，暂不合计')
        out.append({'date': day, 'amount': round2(amount) if complete else None,
                    'amount_basis': next(iter(bases)) if len(bases) == 1 else 'mixed',
                    'estimated': estimated, 'warnings': list(dict.fromkeys(warnings)),
                    'comparisons': list(comparisons.values())})
    return out


def summarize(store, shop_id, start, end):
    entries = store.list_entries(shop_id, start, end)
    sales = store.list_sales(shop_id, start, end)
    revenue_entries = [e for e in entries if e['type'] == 'revenue']
    daily = revenue_days(revenue_entries, sales, store, shop_id)
    revenue = sum(d['amount'] or 0 for d in daily) if all(d['amount'] is not None for d in daily) else None
    purchase = sum(e['amount'] for e in entries if e['type'] == 'purchase')
    expense = sum(e['amount'] for e in entries if e['type'] == 'expense')
    # 月费用只归入所属月份，不把月费任意均摊到某天。
    accrued = [e for e in store.list_entries(shop_id, '0000', '9999', 'expense')
               if start[:7] <= (e['period_month'] or e['date'][:7]) <= end[:7]]
    accrued_total = sum(e['amount'] for e in accrued)
    analysis = sales_analysis(store, shop_id, start, end)
    _, overlap_warnings = selected_sales(sales)
    standard = (sum(r['standard_cost'] for r in analysis)
                if analysis and all(r['standard_cost'] is not None for r in analysis)
                and not any(w.get('incomplete') for w in overlap_warnings) else None)
    basis = {d['amount_basis'] for d in daily}
    if len(basis) > 1 or 'mixed' in basis:
        revenue = None
    # 结算实收已扣佣金，流水里的平台费用可能只是备查，不能再扣一次。
    has_net_fees = 'settlement' in basis and any(e['category'] == '平台佣金' for e in entries)
    cash_balance = revenue - purchase - expense if revenue is not None and not has_net_fees else None
    by_category = defaultdict(float)
    for e in entries:
        if e['type'] != 'revenue':
            by_category[(e['type'], e['category'])] += e['amount']
    if revenue is not None:
        by_category[('revenue', '营业额（去重口径）')] = revenue
    return {'count': len(entries) + len(sales), 'revenue': round2(revenue) if revenue is not None else None,
            'purchase': round2(purchase), 'expense': round2(expense),
            'cash_balance': round2(cash_balance) if cash_balance is not None else None,
            'cash_balance_label': '已记收入减采购和付款（不等于利润）',
            'accrued_expense': round2(accrued_total), 'expense_months': [start[:7], end[:7]],
            'expense_by_month': expense_by_month(accrued),
            'standard_consumption': round2(standard) if standard is not None else None,
            'actual_cost': None, 'gross': None, 'net': None, 'margin': None, 'break_even': None,
            'fixed': round2(sum(e['amount'] for e in accrued if e['category'] in FIXED_CATEGORIES)),
            'amount_basis': next(iter(basis)) if len(basis) == 1 else 'mixed' if basis else 'gross',
            'estimated_revenue': any(d['estimated'] for d in daily),
            'daily_revenue': daily, 'sales_analysis': analysis,
            'sales_breakdown': sales_breakdown(analysis),
            'warnings': (['所选日期包含不同金额口径，收入暂不合计'] if len(basis) > 1 or 'mixed' in basis else [])
                        + (['结算实收已扣平台费用，费用流水中也有平台佣金；收支差额暂不计算，需核对是否另付'] if has_net_fees else []),
            'by_category': [{'type': t, 'category': c, 'amount': round2(v)} for (t, c), v in by_category.items()],
            'days': [d['date'] for d in daily], 'day_count': len(daily),
            'cost_note': '标准成本按销售当日生效配方和截至当日最近进价估算；未做库存实际计价。月费用未分摊到单品。'}


def sales_breakdown(rows):
    result = {}
    for field in ('platform', 'channel', 'product_category'):
        groups = {}
        for row in rows:
            key = (row[field], row['amount_basis'])
            g = groups.setdefault(key, {'name': row[field] or '未注明', 'amount_basis': row['amount_basis'],
                'qty': 0, 'amount': 0, 'standard_cost': 0, 'complete_amount': True, 'complete_cost': True,
                'estimated': False})
            g['qty'] += row['qty']
            g['amount'] += row['amount'] or 0
            g['standard_cost'] += row['standard_cost'] or 0
            g['complete_amount'] &= row['amount'] is not None
            g['complete_cost'] &= row['standard_cost'] is not None
            g['estimated'] |= row['estimated']
        for g in groups.values():
            g['amount'] = round2(g['amount']) if g['complete_amount'] else None
            g['standard_cost'] = round2(g['standard_cost']) if g['complete_cost'] else None
        result[field] = list(groups.values())
    return result


def expense_by_month(entries):
    groups = defaultdict(float)
    for e in entries:
        groups[(e['period_month'] or e['date'][:7], e['category'])] += e['amount']
    return [{'month': month, 'category': category, 'amount': round2(value)}
            for (month, category), value in sorted(groups.items())]
