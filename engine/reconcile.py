"""随口记 · 差异检查。

AI 负责整理，整理不了的就问。这里只产出「值得问一句」的差异，
不自动改数、不猜。问过并回答的差异会被关掉，不会反复骚扰。
"""
from __future__ import annotations

from .cost import product_cost, usage_variance
from .model import TYPE_REVENUE, fmt_money, fmt_pct, round2, sale_amount

# 用量差异超过这个比例才值得问；太小是正常的损耗和称量误差
VARIANCE_THRESHOLD = 0.15
# 营业额对不上的绝对下限，避免几块钱的尾差天天报警
REVENUE_TOLERANCE = 10.0


def scan(store, shop_id: str, start: str, end: str,
         variance_threshold: float = VARIANCE_THRESHOLD) -> list:
    """扫出这个区间的所有差异。返回待问清单（未落库）。"""
    findings = []

    # 1. 卖了但没配方 —— 算不出成本，也就谈不上精细
    sales = store.sales_by_product(shop_id, start, end)
    for sale in sales:
        product = store.product_find(shop_id, sale["product_name"])
        if not product:
            findings.append({
                "date": end, "kind": "unmapped_product", "subject": sale["product_name"],
                "question": f"「{sale['product_name']}」卖出 {sale['qty']:g} 份，但还没建这个商品。"
                            f"要按这个名称建吗？",
            })
            continue
        if not store.recipe_get(shop_id, product["id"]):
            findings.append({
                "date": end, "kind": "missing_recipe", "subject": product["name"],
                "question": f"「{product['name']}」本期卖出 {sale['qty']:g} 份，但还没有配方，算不出成本。"
                            f"它的原料和用量是什么？",
            })

    # 2. 配方里的原料没有进价 —— 成本会算缺
    for product in store.product_list(shop_id):
        result = product_cost(store, shop_id, product)
        if result["missing"] and result["lines"]:
            names = "、".join(m["name"] for m in result["missing"])
            findings.append({
                "date": end, "kind": "missing_price", "subject": product["name"],
                "question": f"「{product['name']}」的原料 {names} 还没有带数量的进货记录，"
                            f"进价算不出来。最近一次买这些花了多少、买了多少？",
            })

    # 3. 理论耗用 vs 实际进货
    for row in usage_variance(store, shop_id, start, end):
        if abs(row["variance"]) < variance_threshold:
            continue
        direction = "多" if row["diff"] > 0 else "少"
        findings.append({
            "date": end, "kind": "usage_variance", "subject": row["ingredient"],
            "expected": row["theoretical"], "actual": row["actual"], "unit": row["unit"],
            "question": (
                f"按销量和配方，本期「{row['ingredient']}」应该用 "
                f"{row['theoretical']:g}{row['unit']}，但实际进了 {row['actual']:g}{row['unit']}，"
                f"{direction}了 {abs(row['diff']):g}{row['unit']}（{fmt_pct(abs(row['variance']))}）。"
                '采购不等于耗用，差额不能判定为损耗；请核对期初期末库存、采购和配方。'
            ),
        })

    # 4. 营业额和销售明细对不上
    from .accounting import revenue_days
    entries = store.list_entries(shop_id, start, end, type_=TYPE_REVENUE)
    for day in revenue_days(entries, store.list_sales(shop_id, start, end), store, shop_id):
        for index, check in enumerate(day['comparisons']):
            diff = check.get('difference')
            if diff is None or abs(diff) <= REVENUE_TOLERANCE:
                continue
            if abs(diff) / max(check['detail'], 1) <= .05:
                continue
            findings.append({'date': day['date'], 'kind': 'revenue_mismatch',
                'subject': f"营业额 {day['date']} 范围{index + 1}",
                'expected': round2(check['detail']), 'actual': check['declared'], 'unit': '元',
                'question': f"{day['date']} 同范围营业额 {fmt_money(check['declared'])} 元，"
                    f"销售明细 {fmt_money(check['detail'])} 元"
                    + ('（含售价推算）' if check['estimated'] else '')
                    + '。请核对总额和明细是否完整。'})

    return findings


def persist(store, shop_id: str, findings: list) -> list:
    """把差异落库。同一对象同一类型的未答问题不重复建。"""
    open_now = {(d["kind"], d["subject"]) for d in store.discrepancy_list(shop_id, "open")}
    created = []
    for f in findings:
        if (f["kind"], f["subject"]) in open_now:
            continue
        created.append(store.discrepancy_add(shop_id, **f))
        open_now.add((f["kind"], f["subject"]))
    return created


def render_questions(questions: list) -> str:
    if not questions:
        return "暂时没有对不上的地方。"
    lines = [f"有 {len(questions)} 处需要你确认：", ""]
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}. {q['question']}")
        lines.append(f"   `{q['id']}`")
    return "\n".join(lines)
