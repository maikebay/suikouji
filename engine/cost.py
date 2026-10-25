"""随口记 · 商品成本核算（配方 / BOM）。

回答三个问题：
  1. 这个商品由哪几种原料组成？
  2. 每种原料按最近进价算多少钱？
  3. 单品成本、毛利、毛利率是多少？

原料进价从进货流水倒推（金额 ÷ 数量），不单独维护价目表——
商家已经在记进货了，不该让他再录一遍价格。
"""
from __future__ import annotations

import re

from .model import (
    TYPE_PURCHASE,
    canon_unit,
    convert,
    fmt_money,
    fmt_pct,
    fmt_price,
    round2,
)

# 「牛肉80克」「面条 120 g」「汤料20克」
_LINE_RE = re.compile(r"^(.*?)\s*(\d+(?:\.\d+)?)\s*([^\d\s]*)$")


def parse_recipe_text(text: str) -> list:
    """把「牛肉80克，面条120克」解析成配方行。"""
    out = []
    parts = re.split(r"[，,、;；\n]+", str(text or ""))
    for part in parts:
        s = part.strip()
        if not s:
            continue
        m = _LINE_RE.match(s)
        if not m:
            raise ValueError(f'无法识别配方片段：{s}')
        name = m.group(1).strip(" 　:：的")
        qty = float(m.group(2))
        unit = canon_unit(m.group(3).strip())
        if not name or qty <= 0 or not unit or name.endswith('-'):
            raise ValueError(f'配方需要原料名、正数和单位：{s}')
        out.append({"name": name, "qty": qty, "unit": unit})
    return out


def _price_in_unit(store, shop_id: str, ingredient: dict, target_unit: str,
                   as_of: str | None = None) -> dict | None:
    """把原料进价换算成目标单位下的单价。换算不了返回 None。"""
    price = store.ingredient_price(shop_id, ingredient["id"], as_of=as_of)
    if not price:
        return None
    price_unit = price['unit'] or ''
    if not target_unit or not price_unit:
        return None

    factor = convert(1, price_unit, target_unit)
    if factor is None:
        return None
    return {
        **price,
        "unit_price": price["unit_price"] / factor,
        "unit": target_unit,
        "converted": canon_unit(price_unit) != canon_unit(target_unit),
    }


def product_cost(store, shop_id: str, product: dict, as_of: str | None = None) -> dict:
    """算一个商品的成本。缺配方或缺进价的原料会列进 missing，不假装算得出来。"""
    from .model import today_iso
    as_of = as_of or today_iso()
    lines = store.recipe_at(shop_id, product['id'], as_of)
    units = float(lines[0]["units"]) if lines else 1.0
    ingredients = {i["id"]: i for i in store.ingredient_list(shop_id)}

    out_lines = []
    missing = []
    total = 0.0

    for line in lines:
        ing = ingredients.get(line["ingredient_id"])
        if not ing:
            missing.append({"name": "(未知原料)", "reason": "原料已删除"})
            continue
        qty = float(line["qty"])
        unit = ing.get("base_unit") or ""

        priced = _price_in_unit(store, shop_id, ing, unit, as_of=as_of)
        if not priced:
            missing.append({"name": ing["name"], "reason": "没有带数量的进货记录，算不出进价"})
            out_lines.append({
                "ingredient": ing["name"], "qty": qty, "unit": unit,
                "unit_price": None, "cost": None,
            })
            continue

        cost = qty * priced["unit_price"]
        total += cost
        out_lines.append({
            "ingredient": ing["name"], "qty": qty, "unit": unit,
            "unit_price": round(priced["unit_price"], 6),
            "price_unit": priced["unit"],
            "price_date": priced["date"],
            "converted": priced.get("converted", False),
            "cost": round(cost, 6),
        })

    complete = not missing and bool(lines)
    for line in out_lines:
        line['per_portion_qty'] = line['qty'] / units
        line['per_portion_cost'] = line['cost'] / units if line['cost'] is not None else None
    unit_cost = total / units if complete else None
    price = product.get("price")
    gross = (price - unit_cost) if price is not None and complete else None
    margin = (gross / price) if (price and gross is not None) else None

    return {
        "product": product,
        "units": units,
        "lines": out_lines,
        'cost_basis': 'standard_recipe_latest_purchase',
        'basis_label': '标准原料成本估算（按生效配方和截至当日最近进价）',
        'as_of': as_of, 'actual_cost': None,
        'known_subtotal': round2(total),
        "total": round2(total) if complete else None,
        "unit_cost": round(unit_cost, 6) if complete else None,
        "price": price,
        "gross": round2(gross) if gross is not None else None,
        "margin": margin,
        "missing": missing,
        "complete": not missing and bool(lines),
    }


def cost_table(store, shop_id: str, as_of: str | None = None) -> list:
    return [product_cost(store, shop_id, p, as_of=as_of)
            for p in store.product_list(shop_id)]


def set_recipe(store, shop_id: str, product_name: str, recipe_text: str,
               price: float | None = None, units=1, category=None, effective_date=None) -> dict:
    """整份配方原子替换，数量统一到原料基础单位。"""
    import math
    parsed = parse_recipe_text(recipe_text)
    units = float(units)
    if not product_name.strip() or not parsed or not math.isfinite(units) or units <= 0:
        raise ValueError('请填写商品、完整配方和大于零的产出份数')
    if price is not None and (not math.isfinite(float(price)) or float(price) < 0):
        raise ValueError('售价必须是非负数')
    with store.transaction():
        product = store.product_upsert(shop_id, product_name.strip(), price=price, category=category)
        lines = []
        for item in parsed:
            ing = store.ingredient_upsert(shop_id, item['name'], base_unit=item['unit'])
            qty = convert(item['qty'], item['unit'], ing['base_unit'])
            if qty is None:
                raise ValueError(f"{item['name']}：{item['unit']}不能换算为{ing['base_unit']}")
            if any(line['ingredient_id'] == ing['id'] for line in lines):
                raise ValueError(f"配方中原料重复：{item['name']}")
            lines.append({'ingredient_id': ing['id'], 'qty': qty})
        store.recipe_set(shop_id, product['id'], lines, units=units, effective_date=effective_date)
    return {'product': product, 'lines': parsed, 'units': units}


def usage_variance(store, shop_id: str, start: str, end: str) -> list:
    """按当前配方估算耗用与采购量比较；差额不是实际消耗或损耗。

    历史成本报表另按销售当日生效配方计算，此辅助检查用于核对库存与采购。
    """
    sales = store.sales_by_product(shop_id, start, end)
    if not sales:
        return []

    products = {p["name"]: p for p in store.product_list(shop_id)}
    ingredients = {i["id"]: i for i in store.ingredient_list(shop_id)}
    theoretical: dict = {}

    for sale in sales:
        product = products.get(sale["product_name"])
        if not product:
            continue
        for line in store.recipe_get(shop_id, product["id"]):
            ing = ingredients.get(line["ingredient_id"])
            if not ing:
                continue
            qty = float(line['qty']) / float(line['units']) * float(sale['qty'] or 0)
            entry = theoretical.setdefault(
                ing["id"], {"ingredient": ing, "qty": 0.0, "unit": ing.get("base_unit") or ""})
            entry["qty"] += qty

    purchases = store.list_entries(shop_id, start, end, type_=TYPE_PURCHASE)
    actual: dict = {}
    for row in purchases:
        if not row.get("qty"):
            continue
        item = str(row.get("item") or "")
        for ing in ingredients.values():
            names = {ing["name"]} | set(ing.get("aliases") or [])
            if item.strip() in names:
                entry = actual.setdefault(ing['id'], {'qty': 0.0, 'unit': row.get('unit') or '', 'complete': True})
                qty = convert(row['qty'], row.get('unit') or '', entry['unit'])
                if qty is None or not entry['unit']:
                    entry['complete'] = False
                else:
                    entry['qty'] += qty
                break

    out = []
    for iid, theory in theoretical.items():
        ing = theory["ingredient"]
        got = actual.get(iid)
        if not got or not got['complete'] or theory["qty"] <= 0:
            continue
        # 用进货时的单位报，商家对「斤」比对「克」有概念
        unit = got["unit"] or theory["unit"]
        factor = convert(1, theory["unit"], unit) if theory["unit"] else 1
        if factor is None:
            continue
        theory_qty = theory["qty"] * factor
        if theory_qty <= 0:
            continue
        diff = got["qty"] - theory_qty
        out.append({
            "ingredient_id": iid,
            "ingredient": ing["name"],
            "unit": unit,
            "theoretical": round2(theory_qty),
            "actual": round2(got["qty"]),
            "diff": round2(diff),
            "variance": diff / theory_qty,
        })
    out.sort(key=lambda x: -abs(x["variance"]))
    return out


def render_cost(result: dict) -> str:
    """把单品成本渲染成一段 Markdown。"""
    product = result["product"]
    lines = [f"**{product['name']}**"]
    if result["price"] is not None:
        lines[0] += f"　售价 {fmt_money(result['price'])} 元"

    if not result["lines"]:
        lines.append("还没有配方。可以这样说：`{0}的配方是 牛肉80克，面条120克`".format(product["name"]))
        return "\n".join(lines)

    lines.append("")
    lines.append("| 原料 | 用量 | 进价 | 成本 |")
    lines.append("| --- | --- | --- | --- |")
    for ln in result["lines"]:
        if ln["unit_price"] is None:
            price_text = "缺进价"
            cost_text = "—"
        else:
            price_text = f"{fmt_price(ln['unit_price'])} 元/{ln['unit']}"
            if ln.get("converted"):
                price_text += "（换算）"
            cost_text = f"{fmt_price(ln['cost'])}"
        lines.append(
            f"| {ln['ingredient']} | {ln['qty']:g}{ln['unit']} | {price_text} | {cost_text} |"
        )

    lines.append("")
    lines.append(f"- 每份标准原料成本估算：{fmt_price(result['unit_cost'])} 元")
    lines.append(f"- 配方产出 {result['units']:g} 份；进价截止 {result['as_of']}。实际耗料成本尚未核算。")
    if result["gross"] is not None:
        lines.append(f"- 按标价估算材料毛利：{fmt_money(result['gross'])} 元　材料毛利率：{fmt_pct(result['margin'])}")
    if result["missing"]:
        names = "、".join(m["name"] for m in result["missing"])
        lines.append(f"- ⚠️ 缺进价：{names}（这些原料还没记过带数量的进货）")
    return "\n".join(lines)
