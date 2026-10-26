"""随口记 · 命令行入口。

网页、机器人或人工都可调用。所有命令默认输出 Markdown，
加 `--json` 则输出结构化数据，方便测试和以后接云端。

设计上有一条明确的「交棒」：`record` 认不出这句话时不会瞎猜，
而是打印一段说明，告诉上层模型该抽出什么、再用哪个命令录进来。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

from .cost import cost_table, product_cost, render_cost, set_recipe
from .importer import commit_import, plan_import
from .model import (
    SOURCE_MANUAL,
    TYPE_EXPENSE,
    TYPE_PURCHASE,
    TYPE_REVENUE,
    TYPES,
    fmt_money,
    fmt_price,
    fmt_qty,
    month_of,
    month_range,
    parse_date,
    round2,
    today_iso,
)
from .observe import backfill_records, ingest
from .reconcile import persist, render_questions, scan
from .report import (
    day_report,
    month_report,
    period_report,
    product_report,
    week_range,
)
from .store import SqliteStore, default_db_path, sha256_file

UNPARSED_HINT = (
    "原话已保留，暂未识别出可单独记录的账目或随想，涉及的金额未入账。\n"
    "未识别的内容可交给已连接的外部助手整理；请先核对已有记录，不要重发整段原话。"
)


def _thought_receipt(thoughts: list) -> str:
    dates = sorted({row["date"] for row in thoughts if row.get("date")})
    label = (dates[0] if len(dates) == 1 else f"{dates[0]}至{dates[-1]}") if dates else ""
    return f"已保存 {len(thoughts)} 条随想" + (f"（记录日：{label}）" if label else "") + "。"


def _out(args, text: str, payload=None) -> None:
    if getattr(args, "json", False) and payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)


def _period_range(period: str) -> tuple:
    today = today_iso()
    if period == "yesterday":
        d = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        return d, d, "昨天"
    if period == "week":
        start, end = week_range()
        return start, end, "最近 7 天"
    if period == "month":
        start, end = month_range(month_of(today))
        return start, end, f"{month_of(today)} 月度"
    return today, today, "今天"


# ---------------------------------------------------------------- 记账


def cmd_record(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    try:
        receipt = ingest(store, args.shop, args.text, dedup_ref=args.dedup_ref)
    except ValueError:
        _out(args, "日期或金额等内容未通过校验，账目和随想均未写入，请先核对。",
             {"status": "rejected", "thoughts": [], "duplicate": False})
        return 3
    thoughts = receipt.get("thoughts") or []
    status = receipt.get("status")
    if status == "needs_confirmation":
        _out(args, "原话已保存。当前账本的行业尚不支持自动记录账目和随想，需要先确认适用的记录方式。", receipt)
        return 3
    if status == "rejected":
        _out(args, "原话已保存，但日期或金额等内容未通过校验，账目和随想均未写入，请先核对。", receipt)
        return 3
    if status in {"pending", "parsing"}:
        _out(args, "暂时没拿到结果，请使用同一条消息继续核对，不要另发一条重复记录。", receipt)
        return 3
    if receipt.get("duplicate"):
        lines = ["这条消息已记录，没有重复入库。"]
        if thoughts:
            lines.append(_thought_receipt(thoughts))
        if status == "partial":
            lines.append("仍有部分内容待核对，未处理部分涉及的金额未入账；请先核对已有记录，不要重发整段原话。")
        _out(args, "\n".join(lines), receipt)
        return 0
    result = receipt.get("parsed") or {}
    entries = [{**e, "shop_id": args.shop} for e in result.get("entries", [])]
    sales = result.get("sales", [])
    observations = receipt.get("observations") or []

    if entries or sales or observations or thoughts:
        lines = []
        ids = []
        if entries:
            ids = receipt["entry_ids"]
            lines += ["已记账：", "", "| 日期 | 类型 | 分类 | 摘要 | 金额 |",
                      "| --- | --- | --- | --- | --- |"]
            from .model import TYPE_LABEL
            for e in entries:
                qty = f" {fmt_qty(e['qty'])}{e['unit'] or ''}" if e.get("qty") else ""
                lines.append(f"| {e['date']} | {TYPE_LABEL.get(e['type'], e['type'])} | "
                             f"{e['category']} | {e.get('item') or '—'}{qty} | {e['amount']:,.2f} |")

        if sales:
            rows = sales
            if lines:
                lines.append("")
            lines += ["已记销量：", "", "| 日期 | 商品 | 数量 | 金额 |",
                      "| --- | --- | --- | --- |"]
            for s in rows:
                amount = f"{s['amount']:,.2f}" if s.get("amount") is not None else "—"
                lines.append(f"| {s['date']} | {s['product_name']} | "
                             f"{fmt_qty(s['qty'])}{s.get('unit') or ''} | {amount} |")

        if observations:
            lines += ["", "已记录经营信息：", "", "| 日期 | 类型 | 主体 | 指标 | 取值 |",
                      "| --- | --- | --- | --- | --- |"]
            for obs in observations:
                value = (fmt_qty(obs["value_num"]) + obs.get("unit", "")
                         if obs.get("value_num") is not None else obs["value_text"])
                lines.append(f"| {obs['occurred_at']} | {obs['kind']} | {obs.get('subject') or '—'} | "
                             f"{obs['measure']} | {value} |")
        if thoughts:
            if lines:
                lines.append("")
            lines.append(_thought_receipt(thoughts))
        if result.get("leftovers"):
            lines.append("")
            lines.append("以下片段尚待核对，涉及的金额未入账：" + "；".join(f"“{text}”" for text in result["leftovers"]) + "。")
            lines.append("未识别的内容可交给已连接的外部助手整理；请先核对已有记录，不要重发整段原话。")
        elif status == "partial":
            lines.append("还有部分内容待核对，未处理部分涉及的金额未入账。")
        if entries and result.get("confidence", 1) < 0.7:
            lines.append("账目分类有待核对，请先确认已有记录的含义。")
        if any("归档日" in str(warning) for warning in receipt.get("warnings") or []):
            lines.append("指定随想归档日需要外部助手整理，相关随想尚未保存。")
        elif receipt.get("warnings"):
            lines.append("另有待核对提示，请核对已保存内容。")
        _out(args, "\n".join(lines),
             {"recorded": ids, "entries": entries, "sales": sales,
              "record_id": receipt["record"]["id"], "status": receipt["status"], "duplicate": False,
              "observations": observations, "thoughts": thoughts, "leftovers": result.get("leftovers", [])})
        return 0

    if result.get("intent") == "query":
        start, end, label = _period_range(result["period"])
        if result["period"] == "cost":
            start, end = _period_range("month")[:2]
            text = product_report(store, args.shop, start, end)
        elif result["period"] == "month":
            start, end = month_range(month_of(today_iso()))
            label = f"{month_of(today_iso())} 月度"
            text = month_report(store, args.shop)
        else:
            text = period_report(store, args.shop, start, end, label)
        _out(args, text, {"intent": "query", "period": result["period"], "label": label,
                          "start": start, "end": end,
                          "summary": store.summary(args.shop, start, end)})
        return 0

    hint = UNPARSED_HINT
    if any("归档日" in str(warning) for warning in receipt.get("warnings") or []):
        hint += "\n指定随想归档日需要外部助手整理，相关随想尚未保存。"
    if result.get("leftovers"):
        hint += "\n尚待核对的片段：" + "；".join(f"“{text}”" for text in result["leftovers"]) + "。"
    _out(args, hint, {"record_id": receipt["record"]["id"], "status": receipt["status"],
                      "recorded": [], "observations": [], "thoughts": [], "duplicate": False,
                      "leftovers": result.get("leftovers", []), "hint": hint})
    return 3


def cmd_add_json(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    try:
        data = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        print(f"JSON 格式不对：{exc}")
        return 1
    if isinstance(data, dict):
        data = data.get("entries", [data])
    if not isinstance(data, list) or not data:
        print("要录入的条目是空的。")
        return 1

    entries = []
    for i, raw in enumerate(data, 1):
        e = dict(raw)
        if e.get("type") not in TYPES:
            print(f"第 {i} 条的 type 必须是 {'/'.join(TYPES)}，实际是 {e.get('type')!r}")
            return 1
        try:
            e["amount"] = round2(float(e["amount"]))
        except (KeyError, TypeError, ValueError):
            print(f"第 {i} 条缺 amount 或不是数字。")
            return 1
        e["date"] = parse_date(str(e.get("date") or "")) or today_iso()
        e.setdefault("category", {"revenue": "营业额", "purchase": "食材成本"}.get(
            e["type"], "其他支出"))
        e.setdefault("item", "")
        e["shop_id"] = args.shop
        entries.append(e)

    ids = store.add_entries(entries)
    lines = ["已记账：", "", "| 日期 | 类型 | 分类 | 摘要 | 金额 |",
             "| --- | --- | --- | --- | --- |"]
    from .model import TYPE_LABEL
    for e in entries:
        qty = f" {fmt_qty(e['qty'])}{e['unit'] or ''}" if e.get("qty") else ""
        lines.append(f"| {e['date']} | {TYPE_LABEL.get(e['type'], e['type'])} | {e['category']} | "
                     f"{e['item']}{qty} | {fmt_money(e['amount'])} |")
    _out(args, "\n".join(lines), {"recorded": ids, "entries": entries})
    return 0


# ---------------------------------------------------------------- 查询


def _report_scope(args) -> tuple:
    """把 report 的各个分支统一成 (start, end, label)。"""
    if args.what == "day":
        date = (parse_date(args.date) if args.date else None) or today_iso()
        return date, date, date
    if args.what == "month":
        m = args.date or month_of(today_iso())
        start, end = month_range(m)
        return start, end, f"{m} 月度"
    if args.what == "week":
        start, end = week_range()
        return start, end, "最近 7 天"
    start, end, label = _period_range(args.period)
    return start, end, label


def cmd_report(args) -> int:
    store = SqliteStore(args.db)
    start, end, label = _report_scope(args)

    if args.what == "product":
        text = product_report(store, args.shop, start, end)
        payload = {"what": "product", "label": label, "start": start, "end": end,
                   'sales': store.summary(args.shop, start, end)['sales_analysis']}
    elif args.what == "day":
        text = day_report(store, args.shop, start)
        payload = {"what": "day", "label": label, "start": start, "end": end,
                   "summary": store.summary(args.shop, start, end),
                   "entries": store.list_entries(args.shop, start, end)}
    else:
        text = period_report(store, args.shop, start, end, label)
        payload = {"what": args.what, "label": label, "start": start, "end": end,
                   "summary": store.summary(args.shop, start, end)}
    _out(args, text, payload)
    return 0


def cmd_entries(args) -> int:
    store = SqliteStore(args.db)
    if args.date:
        date = parse_date(args.date) or today_iso()
        rows = store.list_entries(args.shop, date, date)
    else:
        end = today_iso()
        start = (_dt.date.today() - _dt.timedelta(days=args.days - 1)).isoformat()
        rows = store.list_entries(args.shop, start, end)
    rows = rows[-args.limit:] if args.limit else rows
    if not rows:
        print("这段时间没有记录。")
        return 0

    from .model import TYPE_LABEL
    lines = ["| 日期 | 类型 | 分类 | 摘要 | 金额 | 编号 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for e in rows:
        qty = f" {fmt_qty(e['qty'])}{e['unit'] or ''}" if e.get("qty") else ""
        lines.append(f"| {e['date']} | {TYPE_LABEL.get(e['type'], e['type'])} | {e['category']} | "
                     f"{e['item'] or ''}{qty} | {fmt_money(e['amount'])} | `{e['id']}` |")
    _out(args, "\n".join(lines), {"entries": rows})
    return 0


def cmd_undo(args) -> int:
    store = SqliteStore(args.db)
    entry = store.get_entry(args.entry_id)
    if not entry or entry["shop_id"] != args.shop:
        print(f"没找到编号 {args.entry_id} 的记录。")
        return 1
    store.delete_entry(args.entry_id)
    print(f"已撤销：{entry['date']} {entry['category']} {fmt_money(entry['amount'])} 元")
    return 0


# ---------------------------------------------------------------- 导入


def cmd_import(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    try:
        plan = plan_import(args.path, header_row=args.header)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"读不了这张表：{exc}")
        return 1

    kind_label = "销售明细" if plan["kind"] == "sales" else "流水账"
    lines = [f"读出来这是**{kind_label}**，共 {plan['row_count']} 行可用"
             + (f"，{len(plan['skipped'])} 行认不出" if plan["skipped"] else "") + "。", ""]
    lines.append("| 用到的列 | 来自表头 |")
    lines.append("| --- | --- |")
    col_label = {"date": "日期", "amount": "金额", "type": "类型", "category": "分类",
                 "item": "商品/摘要", "qty": "数量", "unit": "单位", "note": "备注"}
    for key, header in plan["mapping"].items():
        if header:
            lines.append(f"| {col_label.get(key, key)} | {header} |")
    unmapped = [h for h in plan["headers"] if h not in plan["mapping"].values()]
    if unmapped:
        lines.append("")
        lines.append(f"没用到的列：{'、'.join(unmapped)}")

    lines.append("")
    lines.append("**前 5 行预览**")
    lines.append("")
    for row in plan["rows"][:5]:
        if plan["kind"] == "sales":
            lines.append(f"- {row['date'] or '(无日期)'} {row['product_name']} "
                         f"{fmt_qty(row['qty'])}份"
                         + (f" {fmt_money(row['amount'])}元" if row["amount"] else ""))
        else:
            lines.append(f"- {row['date']} {row['type']} {row['category']} "
                         f"{row['item'] or ''} {fmt_money(row['amount'])}元")
    if plan["skipped"]:
        lines.append("")
        lines.append("**认不出的行**")
        for s in plan["skipped"][:5]:
            lines.append(f"- 第 {s['row']} 行：{s['reason']} —— {s['raw']}")

    if not args.commit:
        lines.append("")
        lines.append("确认无误就加 `--commit` 再跑一次，才会真正入库。")
        _out(args, "\n".join(lines), {"plan": plan, "committed": False})
        return 0

    sha = sha256_file(args.path)
    import os
    result = commit_import(store, args.shop, plan, os.path.basename(args.path), sha)
    if result.get("duplicate"):
        print(f"这张表已经导过了（批次 `{result['batch']['id']}`，"
              f"{result['batch']['rows']} 行），没有重复入库。")
        return 0

    lines.append("")
    lines.append(f"✅ 已入库 {result['imported']} 行，批次 `{result['batch']['id']}`。")
    _out(args, "\n".join(lines), {"plan": plan, "committed": True, "result": result})
    return 0


# ---------------------------------------------------------------- 商品与配方


def cmd_product(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    if args.action == "list":
        products = store.product_list(args.shop)
        if not products:
            print("还没有商品。可以说「牛肉面卖12块，配方是牛肉80克，面条120克」。")
            return 0
        lines = ["| 商品 | 售价 | 原料数 |", "| --- | --- | --- |"]
        for p in products:
            lines.append(f"| {p['name']} | "
                         f"{fmt_money(p['price']) if p['price'] is not None else '—'} | "
                         f"{len(store.recipe_get(args.shop, p['id']))} |")
        _out(args, "\n".join(lines), {"products": products})
        return 0

    if args.action == "set":
        price = float(args.price) if args.price is not None else None
        product = store.product_upsert(args.shop, args.name, price=price, category=args.category)
        msg = f"已建商品「{product['name']}」"
        if price is not None:
            msg += f"，售价 {fmt_money(price)} 元"
        if args.aliases:
            store.product_upsert(args.shop, args.name, aliases=args.aliases.split(","))
        print(msg + "。")
        return 0
    return 1


def cmd_recipe(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    price = float(args.price) if args.price is not None else None
    try:
        result = set_recipe(store, args.shop, args.product, args.text, price=price, units=args.units, category=args.category, effective_date=args.effective_date)
    except ValueError as exc:
        print(str(exc))
        return 1
    lines = [f"已记录「{result['product']['name']}」的配方：", ""]
    lines.append("| 原料 | 用量 |")
    lines.append("| --- | --- |")
    for ln in result["lines"]:
        lines.append(f"| {ln['name']} | {ln['qty']:g}{ln['unit']} |")
    lines.append("")
    lines.append("再说一句「" + result["product"]["name"] + "成本多少」，我就把进价折进去算。")
    _out(args, "\n".join(lines), result)
    return 0


def cmd_cost(args) -> int:
    store = SqliteStore(args.db)
    if args.product:
        product = store.product_find(args.shop, args.product)
        if not product:
            print(f"没找到商品「{args.product}」。")
            return 1
        result = product_cost(store, args.shop, product)
        _out(args, render_cost(result), result)
        return 0

    results = cost_table(store, args.shop)
    if not results:
        print("还没有商品。")
        return 0
    lines = ["| 商品 | 售价 | 标准原料成本估算 | 标价材料毛利估算 | 材料毛利率 |", "| --- | --- | --- | --- | --- |"]
    from .model import fmt_pct
    for r in results:
        if r["missing"] or not r["lines"]:
            lines.append(f"| {r['product']['name']} | "
                         f"{fmt_money(r['price']) if r['price'] is not None else '—'} | "
                         f"缺配方/进价 | — | — |")
            continue
        lines.append(f"| {r['product']['name']} | "
                     f"{fmt_money(r['price']) if r['price'] is not None else '—'} | "
                     f"{fmt_price(r['unit_cost'])} | "
                     f"{fmt_money(r['gross']) if r['gross'] is not None else '—'} | "
                     f"{fmt_pct(r['margin']) if r['margin'] is not None else '—'} |")
    _out(args, "\n".join(lines), {"costs": results})
    return 0


def cmd_sale(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    date = parse_date(args.date) if args.date else today_iso()
    product = store.product_find(args.shop, args.name)
    if not product:
        product = store.product_upsert(args.shop, args.name)
    store.add_sales(args.shop, [{
        "date": date or today_iso(), "product_id": product["id"], "product_name": product["name"],
        "qty": float(args.qty), "amount": float(args.amount) if args.amount else None,
        "platform": args.platform, "channel": args.channel,
        "product_category": args.category, "amount_basis": args.amount_basis,
        'granularity': args.granularity,
    }], source=SOURCE_MANUAL)
    print(f"已记销量：{date} {product['name']} {args.qty} 份"
          + (f"，{fmt_money(float(args.amount))} 元" if args.amount else ""))
    return 0


# ---------------------------------------------------------------- 对账


def cmd_check(args) -> int:
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop)
    start, end, label = _period_range(args.period)
    findings = scan(store, args.shop, start, end)
    created = persist(store, args.shop, findings)
    open_now = store.discrepancy_list(args.shop, "open")

    lines = [f"**对账**　{label}（{start} ~ {end}）", ""]
    if not open_now:
        lines.append("暂时没有对不上的地方。")
    else:
        if created:
            lines.append(f"新发现 {len(created)} 处，连同之前没答的一共 {len(open_now)} 处：")
        else:
            lines.append(f"有 {len(open_now)} 处还没确认：")
        lines.append("")
        for i, q in enumerate(open_now, 1):
            lines.append(f"{i}. {q['question']}")
            lines.append(f"   `{q['id']}`")
        lines.append("")
        lines.append("回答用：`答 <编号> <说明>`，比如「答 d_ab12 是损耗，扔了一批」。")
    _out(args, "\n".join(lines), {"created": created, "open": open_now})
    return 0


def cmd_answer(args) -> int:
    store = SqliteStore(args.db)
    if not store.discrepancy_resolve(args.id, args.text):
        print(f"没找到编号 {args.id} 的待确认项。")
        return 1
    print("已记录，之后不再追问这一条。")
    return 0


def cmd_questions(args) -> int:
    store = SqliteStore(args.db)
    rows = store.discrepancy_list(args.shop, "all" if args.all else "open")
    _out(args, render_questions(rows), {"questions": rows})
    return 0


# ---------------------------------------------------------------- 通用记录


def cmd_backfill(args):
    with_store = SqliteStore(args.db)
    result = backfill_records(with_store, args.shop)
    _out(args, f"已检查 {result['scanned']} 条历史账目，新增 {result['added']} 条原话记录，"
         f"跳过已投影 {result['skipped']} 条。缺失原话 {result['missing_raw_text']} 条，未补造。", result)
    return 0


def cmd_records(args):
    store = SqliteStore(args.db)
    rows = store.record_list(args.shop, args.start, args.end, args.status, args.limit)
    lines = [f"{r['occurred_at']} [{r['parse_status']}] {r['raw_text']} ({r['id']})" for r in rows]
    _out(args, "\n".join(lines) or "没有原话记录。", {"records": rows})
    return 0


def cmd_observations(args):
    store = SqliteStore(args.db)
    if args.action == "series":
        if not args.kind or not args.measure:
            print("趋势查询需要 --kind 和 --measure。")
            return 1
        rows = store.observation_series(args.shop, args.start, args.end,
                                         args.kind, args.measure, args.subject)
    elif args.action == "list":
        rows = store.observation_list(args.shop, args.start, args.end,
                                       args.kind, args.subject, args.measure)
    elif args.action == "kinds":
        rows = store.observation_kinds(args.shop)
    else:
        rows = store.observation_summary(args.shop, args.start, args.end)
    _out(args, json.dumps(rows, ensure_ascii=False, indent=2), {"action": args.action, "rows": rows})
    return 0


def cmd_dimension(args):
    store = SqliteStore(args.db)
    if args.action == "set":
        if not args.kind or not args.canonical:
            print("设置词表需要 kind 和 canonical。")
            return 1
        try:
            row = store.dimension_upsert(args.shop, args.kind, args.canonical,
                                         [x.strip() for x in args.aliases.split(",") if x.strip()])
        except ValueError as exc:
            _out(args, str(exc), {"error": str(exc)})
            return 1
        rows = [row]
    else:
        rows = store.dimension_list(args.shop, args.kind)
    _out(args, json.dumps(rows, ensure_ascii=False, indent=2), {"dimensions": rows})
    return 0


# ---------------------------------------------------------------- 其他


def cmd_export(args) -> int:
    store = SqliteStore(args.db)
    data = store.export_all(args.shop)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        print(f"已导出到 {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_seed(args) -> int:
    """塞一份小吃店样例数据，方便先看看长什么样。"""
    store = SqliteStore(args.db)
    store.ensure_shop(args.shop, args.name)
    if store.list_entries(args.shop, "0000-01-01", "9999-12-31"):
        print("这个店已经有数据了，没有重复写入。")
        return 0

    today = _dt.date.today()
    entries = []
    for i in range(14):
        d = (today - _dt.timedelta(days=13 - i)).isoformat()
        base = 1180 + (i % 5) * 130
        entries += [
            {"date": d, "type": TYPE_REVENUE, "category": "营业额",
             "amount": base, "item": "营业额"},
            {"date": d, "type": TYPE_PURCHASE, "category": "食材成本",
             "amount": round2(base * 0.34), "item": "牛肉", "qty": 22, "unit": "斤"},
            {"date": d, "type": TYPE_PURCHASE, "category": "食材成本",
             "amount": round2(base * 0.08), "item": "面条", "qty": 18, "unit": "斤"},
        ]
    entries += [
        {"date": (today - _dt.timedelta(days=13)).isoformat(), "type": TYPE_EXPENSE,
         "category": "房租", "amount": 8000, "item": "店面租金"},
        {"date": (today - _dt.timedelta(days=13)).isoformat(), "type": TYPE_EXPENSE,
         "category": "人工", "amount": 6000, "item": "员工工资"},
        {"date": (today - _dt.timedelta(days=10)).isoformat(), "type": TYPE_EXPENSE,
         "category": "水电", "amount": 486, "item": "水电费"},
    ]
    for e in entries:
        e["shop_id"] = args.shop
    store.add_entries(entries, source=SOURCE_MANUAL)

    store.product_upsert(args.shop, "牛肉面", price=12.0)
    set_recipe(store, args.shop, "牛肉面", "牛肉80克，面条120克，汤料20克", price=12.0)
    store.product_upsert(args.shop, "小份牛肉面", price=9.0)
    set_recipe(store, args.shop, "小份牛肉面", "牛肉50克，面条80克，汤料15克", price=9.0)

    sales = []
    for i in range(7):
        d = (today - _dt.timedelta(days=6 - i)).isoformat()
        sales += [
            {"date": d, "product_name": "牛肉面", "qty": 60 + (i % 4) * 6, "amount": None},
            {"date": d, "product_name": "小份牛肉面", "qty": 25 + (i % 3) * 4, "amount": None},
        ]
    for s in sales:
        p = store.product_find(args.shop, s["product_name"])
        s["product_id"] = p["id"] if p else None
    store.add_sales(args.shop, sales, source=SOURCE_MANUAL)

    print(f"已写入样例数据：{len(entries)} 笔流水、2 个商品、{len(sales)} 条销量。")
    print("看看效果：`suikouji today`、`suikouji month`、`suikouji cost`、`suikouji check`")
    return 0


# ---------------------------------------------------------------- 解析


def _common_parent(suppress_defaults: bool) -> argparse.ArgumentParser:
    """全局开关（--db / --shop / --json）。

    顶层和子命令各挂一份，这样 `--db X record ...` 和 `record --db X ...` 都能用。
    子命令那份必须用 SUPPRESS 当默认值 —— 否则子解析器会用默认值
    把写在子命令前面的 --db 覆盖掉，这是 argparse 的经典坑。
    """
    p = argparse.ArgumentParser(add_help=False)
    default = (lambda v: argparse.SUPPRESS) if suppress_defaults else (lambda v: v)
    p.add_argument("--db", default=default(default_db_path()), help="数据库路径")
    p.add_argument("--shop", default=default("default"), help="店铺 ID")
    p.add_argument("--json", action="store_true", default=default(False),
                   help="输出 JSON 而不是 Markdown")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="suikouji", description="随口记 · 经营记账",
                                parents=[_common_parent(False)])
    sub = p.add_subparsers(dest="cmd", required=True)
    common = _common_parent(True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    r = add("record", help="用一句话记账")
    r.add_argument("text")
    r.add_argument("--dedup-ref", help="来源唯一 ID，例如 msg:微信消息ID")
    r.set_defaults(func=cmd_record)

    bf = add("backfill", help="幂等补齐历史原话与观测，不修改旧账")
    bf.set_defaults(func=cmd_backfill)

    records = add("records", help="查看原话和解析状态")
    records.add_argument("--start", default="0000")
    records.add_argument("--end", default="9999")
    records.add_argument("--status")
    records.add_argument("--limit", type=int, default=100)
    records.set_defaults(func=cmd_records)

    ob = add("observations", help="通用观测列表、汇总和趋势")
    ob.add_argument("action", choices=["list", "summary", "series", "kinds"], default="summary", nargs="?")
    ob.add_argument("--start", default="0000")
    ob.add_argument("--end", default="9999")
    ob.add_argument("--kind")
    ob.add_argument("--measure")
    ob.add_argument("--subject")
    ob.set_defaults(func=cmd_observations)

    dim = add("dimension", help="查询或补充受控词表")
    dim.add_argument("action", choices=["list", "set"])
    dim.add_argument("kind", nargs="?", choices=["kind", "measure", "subject", "unit"])
    dim.add_argument("canonical", nargs="?")
    dim.add_argument("--aliases", default="", help="逗号分隔的别名")
    dim.set_defaults(func=cmd_dimension)

    a = add("add-json", help="用结构化 JSON 记账（模型解析后走这条）")
    a.add_argument("payload")
    a.set_defaults(func=cmd_add_json)

    rep = add("report", help="看报表")
    rep.add_argument("what", choices=["day", "week", "month", "period", "product"])
    rep.add_argument("date", nargs="?", help="日期或月份，如 2026-10-23 / 2026-10")
    rep.add_argument("--period", default="today",
                     choices=["today", "yesterday", "week", "month"])
    rep.set_defaults(func=cmd_report)

    for alias in ("today", "day"):
        d = add(alias, help="看某天")
        d.add_argument("date", nargs="?")
        d.set_defaults(func=lambda a: cmd_report(_as_report(a, "day")))

    w = add("week", help="看最近 7 天")
    w.set_defaults(func=lambda a: cmd_report(_as_report(a, "week")))

    m = add("month", help="看月度")
    m.add_argument("date", nargs="?")
    m.set_defaults(func=lambda a: cmd_report(_as_report(a, "month")))

    e = add("entries", help="列流水")
    e.add_argument("--date")
    e.add_argument("--days", type=int, default=7)
    e.add_argument("--limit", type=int, default=30)
    e.set_defaults(func=cmd_entries)

    u = add("undo", help="撤销一笔")
    u.add_argument("entry_id")
    u.set_defaults(func=cmd_undo)

    i = add("import", help="导入 Excel/CSV")
    i.add_argument("path")
    i.add_argument("--header", type=int, default=0, help="表头在第几行（0 基）")
    i.add_argument("--commit", action="store_true", help="确认入库")
    i.set_defaults(func=cmd_import)

    pr = add("product", help="商品")
    pr.add_argument("action", choices=["list", "set"])
    pr.add_argument("name", nargs="?")
    pr.add_argument("--price")
    pr.add_argument("--category")
    pr.add_argument("--aliases", help="别名，逗号分隔")
    pr.set_defaults(func=cmd_product)

    rc = add("recipe", help="配方")
    rc.add_argument("product")
    rc.add_argument("text", help="如「牛肉80克，面条120克」")
    rc.add_argument("--price")
    rc.add_argument("--units", type=float, default=1)
    rc.add_argument("--category")
    rc.add_argument("--effective-date")
    rc.set_defaults(func=cmd_recipe)

    c = add("cost", help="单品成本")
    c.add_argument("product", nargs="?")
    c.set_defaults(func=cmd_cost)

    s = add("sale", help="记一笔销量")
    s.add_argument("name")
    s.add_argument("qty", type=float)
    s.add_argument("--amount")
    s.add_argument("--date")
    s.add_argument("--platform", default="")
    s.add_argument("--channel", default="")
    s.add_argument("--category", default="")
    s.add_argument("--amount-basis", choices=["gross", "settlement"], default="gross")
    s.add_argument('--granularity', choices=['daily', 'transaction'], default='daily')
    s.set_defaults(func=cmd_sale)

    ck = add("check", help="对账，找出对不上的地方")
    ck.add_argument("--period", default="week",
                    choices=["today", "yesterday", "week", "month"])
    ck.set_defaults(func=cmd_check)

    an = add("answer", help="回答一条待确认")
    an.add_argument("id")
    an.add_argument("text")
    an.set_defaults(func=cmd_answer)

    q = add("questions", help="看待确认清单")
    q.add_argument("--all", action="store_true")
    q.set_defaults(func=cmd_questions)

    ex = add("export", help="全量导出 JSON")
    ex.add_argument("--out")
    ex.set_defaults(func=cmd_export)

    sd = add("seed", help="写入样例数据")
    sd.add_argument("--name", default="我的店")
    sd.set_defaults(func=cmd_seed)

    return p


def _as_report(args, what: str):
    args.what = what
    if not hasattr(args, "period"):
        args.period = "month" if what == "month" else "today"
    return args


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
