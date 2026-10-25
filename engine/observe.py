"""原话、观测与账表投影。新增非金额类型只需扩展 RECORD_TYPES。

账表仍是金额计算依据；历史回填按已存流水投影，不重新解析整段原话。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from . import parser
from .model import SOURCE_CHAT, canon_unit

PARSER_VERSION = "observe-1"
KIND_BY_TYPE = {"revenue": "营业额", "purchase": "进货", "expense": "支出"}
NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"
TIME = r"(?:大前天|前天|昨天|昨日|今天|今日|明天|当天)"

# 每条规则指定值组、单位和可选主体组。观测没有任何入账权限。
RECORD_TYPES = {
    "客流": [
        {"pattern": rf"客流(?:量)?\s*(?P<value>{NUMBER})\s*人?", "measure": "人数", "unit": "人"},
        {"pattern": rf"来了\s*(?P<value>{NUMBER})\s*(?:人|位客人|个客人)", "measure": "人数", "unit": "人"},
        {"pattern": rf"(?P<value>{NUMBER})\s*(?:个|位)?客人", "measure": "人数", "unit": "人"},
        {"pattern": rf"上座\s*(?P<value>{NUMBER})\s*桌", "measure": "桌数", "unit": "桌"},
    ],
    "订单": [
        {"pattern": rf"(?P<value>{NUMBER})\s*单(?:外卖|堂食)", "measure": "单量", "unit": "单"},
        {"pattern": rf"(?:外卖|堂食)(?:订单)?\s*(?P<value>{NUMBER})\s*单", "measure": "单量", "unit": "单"},
    ],
    "天气": [
        {"pattern": r"(?P<value>下大雨|下小雨|下雨|暴雨|大雨|小雨|雨天)", "measure": "天气", "text": "雨"},
        {"pattern": r"(?P<value>大晴天|晴天|天晴)", "measure": "天气", "text": "晴"},
        {"pattern": r"(?P<value>阴天)", "measure": "天气", "text": "阴"},
        {"pattern": r"(?P<value>多云)", "measure": "天气", "text": "多云"},
        {"pattern": r"(?P<value>下大雪|下小雪|下雪|大雪|小雪)", "measure": "天气", "text": "雪"},
        {"pattern": rf"(?:天气|{TIME})\s*(?:是|为)?\s*(?P<value>晴|雨|阴|雪)", "measure": "天气", "text": True},
    ],
    "排班": [
        {"pattern": rf"(?P<subject>[一-龥]{{2,4}}?)\s*(?:{TIME}\s*)?(?P<value>请假|请了假|缺勤|迟到|早退|休息|上班|到岗)",
         "measure": "出勤", "text": True},
    ],
    "评价": [
        {"pattern": r"评分\s*(?P<value>\d+(?:\.\d+)?)\s*分?", "measure": "评分", "unit": "分"},
        {"pattern": rf"(?P<value>{NUMBER})\s*个?差评", "measure": "差评数", "unit": "个"},
        {"pattern": rf"差评\s*(?P<value>{NUMBER})\s*个?", "measure": "差评数", "unit": "个"},
    ],
}


def _date_at(text, position, base):
    """取当前位置之前最近的显式日期，同句多个日期不串用。"""
    date = base.isoformat()
    # 与账目共用日期语法；先保留原话，非法日期由parse在投影事务内统一拒绝。
    for token in parser.find_dates(text[:position], base, strict=False):
        date = token['date']
    return date


def find_observations(text, occurred_at):
    base = dt.date.fromisoformat(occurred_at[:10])
    hits = []
    for kind, rules in RECORD_TYPES.items():
        for rule in rules:
            for match in re.finditer(rule["pattern"], text):
                # 否定、条件、疑问不是已发生事实，保留原话等待澄清。
                clause_start = max(text.rfind(ch, 0, match.start()) for ch in "，,。；;！!？?\n") + 1
                clause_end = re.search(r"[，,。；;！!？?\n]", text[match.end():])
                end = match.end() + clause_end.start() if clause_end else len(text)
                context = text[clause_start:end]
                if re.search(r"没有|没下|不是|如果|假如|会不会|是不是|吗|是否", context):
                    continue
                if end < len(text) and text[end] in "？?":
                    continue
                subject = match.groupdict().get("subject")
                if subject:
                    subject = re.sub(r"^(?:" + TIME + r")", "", subject)
                    if len(subject) < 2 or subject in ("我们", "大家", "店里"):
                        continue
                raw = match.group("value")
                text_value = rule.get("text")
                value = None if text_value else (float(raw) if re.fullmatch(r"\d+(?:\.\d+)?", raw)
                                                else parser.cn_to_number(raw))
                hits.append({
                    "kind": kind, "subject": subject, "measure": rule["measure"],
                    "value_num": value,
                    "value_text": (raw if text_value is True else text_value) or None,
                    "unit": rule.get("unit", ""), "confidence": 1.0,
                    "occurred_at": _date_at(text, match.end(), base),
                    "source_span": match.group(), "start": match.start(), "end": match.end(),
                })
    # 先认领长匹配，再恢复原文次序；排班遮掉姓名和状态整段。
    kept = []
    for hit in sorted(hits, key=lambda h: -(h["end"] - h["start"])):
        if not any(hit["start"] < h["end"] and hit["end"] > h["start"] for h in kept):
            kept.append(hit)
    return sorted(kept, key=lambda h: h["start"])


def observations_from_parse(parsed):
    rows = []
    for entry in parsed.get("entries", []):
        kind = KIND_BY_TYPE[entry["type"]]
        subject = None if entry["type"] == "revenue" else entry.get("item") or entry.get("category")
        common = {"kind": kind, "subject": subject, "occurred_at": entry["date"],
                  "source_span": entry.get("note"), "confidence": 1.0}
        rows.append({**common, "measure": "金额", "value_num": entry["amount"], "unit": "元"})
        if entry["type"] == "purchase" and entry.get("qty") is not None:
            rows.append({**common, "measure": "数量", "value_num": entry["qty"], "unit": entry.get("unit") or ""})
    for sale in parsed.get("sales", []):
        common = {"kind": "销量", "subject": sale["product_name"], "occurred_at": sale["date"],
                  "source_span": sale["product_name"], "confidence": 1.0}
        # 老 sale 表未存单位，空单位表示未知，不能推测成份。
        rows.append({**common, "measure": "数量", "value_num": sale["qty"], "unit": sale.get("unit") or ""})
        if sale.get("amount") is not None:
            rows.append({**common, "measure": "金额", "value_num": sale["amount"], "unit": "元"})
    return rows


def normalize(store, shop_id, rows):
    out = []
    for row in rows:
        row = dict(row)
        for field in ("kind", "measure", "subject"):
            if row.get(field):
                row[field] = store.dimension_resolve(shop_id, field, row[field])
        row["unit"] = store.dimension_resolve(shop_id, "unit", canon_unit(row.get("unit") or ""))
        out.append(row)
    return out


def dedup_keys(rows):
    seen, out = {}, []
    for row in rows:
        identity = [row.get(k) for k in ("record_id", "occurred_at", "kind", "subject",
                                         "measure", "value_num", "value_text", "unit")]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        n = seen.get(digest, 0)
        seen[digest] = n + 1
        out.append({**row, "dedup_key": f"record:{digest}:{n}"})
    return out


def _save_record(store, shop_id, text, *, source=SOURCE_CHAT, peer=None, dedup_ref=None,
                 received_at=None, base=None, media=None, context=None,
                 intent=None, routing_status=None, profile_id=None):
    text = str(text or "")
    base = base or (dt.date.fromisoformat(received_at[:10]) if received_at else dt.date.today())
    media = media or {}
    return store.record_add(
        shop_id, text, source=source, peer=peer, dedup_ref=dedup_ref,
        occurred_at=_date_at(text, len(text), base), received_at=received_at,
        parse_base_date=base.isoformat(), context=context, intent=intent,
        routing_status=routing_status, profile_id=profile_id,
        media_path=media.get("path"), media_sha256=media.get("sha256"))


def observe(store, shop_id, text, *, _record=None, **fields):
    """保留原话并提取非金额观测；账表写入集中在 ingest / Store。

    原话先独立持久化；解析失败保留 pending，原请求号可再次处理。
    直接调用 observe 也要把「认领→解析→投影」放在同一事务里，
    不能把 parsing 状态单独提交后在解析失败时卡死。
    """
    if _record is None:
        record = _save_record(store, shop_id, text, **fields)
        try:
            with store.transaction():
                return observe(store, shop_id, text, _record=record, **fields)
        except ValueError:
            # 确定性的输入错误不能让已保存的原话永远停在 pending；
            # 状态更新必须在失败的投影事务之外单独提交。
            store.record_set_status(record["id"], 'rejected', PARSER_VERSION)
            raise
    record = _record
    if record["duplicate"] and record["parse_status"] == "parsing":
        # 另一个连接已经认领但尚未完成；不能把「parsing」当成可继续的 pending，
        # 否则调用方会在半成品状态上重复投影。由原认领者完成后再重试。
        return {"record": record, "duplicate": True, "observations": [],
                "parsed": None, "status": record["parse_status"]}
    if record["duplicate"] and record["parse_status"] == "rejected":
        raise ValueError('这条原话的金额或格式不合法，请修改后换一个记录编号')
    if record["duplicate"] and record["parse_status"] != "pending":
        return {"record": record, "duplicate": True, "observations": [],
                "parsed": None, "status": record["parse_status"]}
    if record["duplicate"]:
        # pending 只说明原话写下来了，账目还没投影完。续跑前先认领，
        # 否则同一请求号的两个写者会各投影一套账目 —— 采购金额直接翻倍。
        with store.transaction():
            if not store.record_claim(record["id"]):
                return {"record": record, "duplicate": True, "observations": [],
                        "parsed": None, "status": record["parse_status"]}
    # 重试必须读取已保存的正文，不采纳同一消息 ID 后续传来的不同正文。
    text = record["raw_text"]
    base = dt.date.fromisoformat(record.get("parse_base_date") or record["received_at"][:10])
    work = text.strip()
    found = find_observations(work, base.isoformat())
    parsed = parser.parse(work, base, extra_mask=found)
    rows = [{k: v for k, v in row.items() if k not in ("start", "end")} for row in found]
    rows = dedup_keys(normalize(store, shop_id, [{**r, "record_id": record["id"]} for r in rows]))
    has_data = bool(rows or parsed["entries"] or parsed["sales"])
    status = ("partial" if parsed.get("leftovers") else "parsed") if has_data else (
        "parsed" if parsed["intent"] == "query" else "unparsed")
    with store.transaction():
        # pending 重试只替换非金额投影，不动已确认账目的投影。
        for old in store.observation_list(shop_id, "0000", "9999"):
            if old["record_id"] == record["id"] and old["dedup_key"].startswith("record:"):
                store.observation_delete_origin(shop_id, old["dedup_key"])
        ids = store.observation_add(shop_id, rows)
        # 金额尚待调用方投影，不提前宣布整条完成。
        if not parsed["entries"] and not parsed["sales"]:
            store.record_set_status(record["id"], status, PARSER_VERSION)
    return {"record": {**record, "parse_status": status, "parser_version": PARSER_VERSION},
            "duplicate": False, "observations": rows, "observation_ids": ids,
            "parsed": parsed, "status": status}


def ingest(store, shop_id, text, **fields):
    """录入入口：先保存原话，再原子提交观测、账目和解析状态。

    当前餐饮解析器只对 restaurant profile 开放。其他行业先保留原话并
    返回待确认，不能因为句子里出现数字就误写成餐饮账目。
    """
    record = _save_record(store, shop_id, text, **fields)
    workspace = store.workspace_get(shop_id)
    if workspace and workspace.get("profile_id", "restaurant") != "restaurant":
        if record["duplicate"] and record["parse_status"] == "needs_confirmation":
            return {"record": record, "duplicate": True, "observations": [],
                    "observation_ids": [], "parsed": None,
                    "entry_ids": [], "sale_ids": [], "status": record["parse_status"]}
        pending = {"intent": "unknown", "routing_status": "needs_confirmation",
                   "profile_id": workspace["profile_id"]}
        # A retry of a record left pending by a crash completes this small
        # routing transaction instead of caching a permanently pending result.
        with store.transaction():
            store.record_set_routing(record["id"], **pending)
            store.record_set_status(record["id"], "needs_confirmation", PARSER_VERSION)
        return {"record": {**record, "parse_status": "needs_confirmation", **pending},
                "duplicate": bool(record["duplicate"]), "observations": [],
                "observation_ids": [],
                "parsed": {"intent": "needs_confirmation", "entries": [], "sales": [],
                           "leftovers": [record["raw_text"]]},
                "entry_ids": [], "sale_ids": [], "status": "needs_confirmation"}
    try:
        with store.transaction():
            result = observe(store, shop_id, text, _record=record)
            if result["duplicate"]:
                return {**result, "entry_ids": [], "sale_ids": []}
            parsed, record = result["parsed"], result["record"]
            entries = [{**e, "shop_id": shop_id} for e in parsed["entries"]]
            eids = store.add_entries(entries, source=record["source"],
                                     raw_text=record["raw_text"], record_id=record["id"])
            sales = []
            for sale in parsed["sales"]:
                product = store.product_find(shop_id, sale["product_name"])
                product = product or store.product_upsert(shop_id, sale["product_name"])
                sales.append({**sale, "product_id": product["id"]})
            sids = store.add_sales(shop_id, sales, source=record["source"], record_id=record["id"])
            store.record_set_status(record["id"], result["status"], PARSER_VERSION)
            return {**result, "entry_ids": eids, "sale_ids": sids}
    except (ValueError, TypeError, OverflowError):
        # 输入已明确不合法（例如非有限金额）：撤销观测/账目投影，
        # 但把原话标成 rejected，避免每次相同 request_id 重试都留下 pending。
        # 状态更新必须在失败的投影事务之外单独提交，否则状态也会随
        # 外层 savepoint 一并回滚。
        try:
            store.record_set_status(record["id"], 'rejected', PARSER_VERSION)
        except Exception:
            # 保留原始输入错误；若状态更新也失败，调用方仍应看到 400。
            pass
        raise


def project_ledger_row(store, shop_id, row, table, record_id=None):
    """一条历史账只生成自己的观测；不可重新解析同一句完整原话。"""
    origin = f"{table}:{row['id']}:"
    existing = store.observation_origin(shop_id, origin)
    record_id = record_id or (existing[0]["record_id"] if existing else None)
    linked = store.record_for_origin(shop_id, origin[:-1]) if not record_id else None
    record_id = record_id or (linked["id"] if linked else None)
    added = False
    if not record_id:
        raw = row.get("raw_text") or ""
        record = store.record_add(
            shop_id, raw, source=row.get("source") or SOURCE_CHAT,
            dedup_ref=f"{table}:{row['id']}", occurred_at=row["date"],
            received_at=row.get("created_at"), batch_id=row.get("batch_id"),
            parse_status="parsed" if raw.strip() else "legacy_structured",
            parser_version="backfill-1")
        record_id, added = record["id"], not record["duplicate"]
    rows = observations_from_parse({"entries" if table == "entry" else "sales": [row]})
    rows = normalize(store, shop_id, rows)
    for i, obs in enumerate(rows):
        obs.update(record_id=record_id, dedup_key=f"{origin}{i}")
    with store.transaction():
        store.record_link(shop_id, record_id, origin[:-1])
        store.observation_delete_origin(shop_id, origin)
        ids = store.observation_add(shop_id, rows)
    return {"added": int(added), "observations": len(ids)}


def backfill_records(store, shop_id, rows=None):
    """幂等回填原话和已确认账表观测；不改旧账、不补造缺失原话。"""
    entries = rows if rows is not None else store.list_entries(shop_id, "0000", "9999")
    sales = [] if rows is not None else store.list_sales(shop_id, "0000", "9999")
    counts = {"scanned": len(entries) + len(sales), "added": 0, "skipped": 0,
              "observations": 0, "missing_raw_text": 0}
    with store.transaction():
        for table, items in (("entry", entries), ("sale", sales)):
            for row in items:
                if row["shop_id"] != shop_id:
                    raise ValueError("不能跨店回填")
                if not (row.get("raw_text") or "").strip():
                    counts["missing_raw_text"] += 1
                if store.observation_origin(shop_id, f"{table}:{row['id']}:"):
                    counts["skipped"] += 1
                    continue
                result = project_ledger_row(store, shop_id, row, table)
                counts["added"] += result["added"]
                counts["observations"] += result["observations"]
    return counts
