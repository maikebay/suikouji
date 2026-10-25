"""Validate Agent-normalized facts and persist them without parsing the raw text.

The Agent handles colloquial language, dates, and uncertainty before invoking the
capture tool. Quotes establish provenance presence, not semantic truth. This
module deliberately does not attempt to infer facts from arbitrary narrative.
"""
from __future__ import annotations

import datetime as dt
import math
import re

from .observe import RECORD_TYPES, dedup_keys, normalize


PARSER_VERSION = "agent-structured-1"
MAX_ROWS = 50
MAX_NUMBER = 1_000_000_000_000
MAX_LABEL = 100
MAX_QUOTE = 2000
SECTIONS = {"entries", "sales", "observations", "leftovers"}
COMMON = {"date", "source_quote", "assertion"}
FINANCIAL = {"amount_basis", "granularity"}
ENTRY_REQUIRED = COMMON | FINANCIAL | {"type", "category", "amount"}
ENTRY_OPTIONAL = {"item", "qty", "unit", "period_month", "platform", "channel"}
SALE_REQUIRED = COMMON | FINANCIAL | {"product_name", "qty", "amount", "unit"}
SALE_OPTIONAL = {"platform", "channel", "product_category"}
OBS_REQUIRED = COMMON | {"kind", "measure"}
OBS_OPTIONAL = {"value_num", "value_text", "unit", "subject"}
EXPENSE_CATEGORIES = {"房租", "人工", "水电", "平台佣金", "包装耗材", "推广", "维修", "税费", "其他支出"}
WEATHER = {"阴", "晴", "雨", "多云", "雪"}
ATTENDANCE = {"请假", "请了假", "缺勤", "迟到", "早退", "休息", "上班", "到岗"}


def _fields(value, required, optional, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    if required - value.keys():
        raise ValueError(f"{label} 缺少字段：{', '.join(sorted(required - value.keys()))}")
    if value.keys() - required - optional:
        raise ValueError(f"{label} 含有未允许的字段")


def _text(value, label, limit=MAX_LABEL):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} 必须是 1 到 {limit} 个字符的非空文本")
    return value


def _number(value, label):
    # Check the range before isfinite so unbounded Python ints cannot overflow
    # during float conversion. bool and numeric strings are not JSON numbers.
    if type(value) not in (int, float) or not 0 <= value <= MAX_NUMBER or not math.isfinite(value):
        raise ValueError(f"{label} 必须是 0 到 {MAX_NUMBER} 之间的有限数字，不能是布尔值或字符串")
    return value


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{label} 必须是 YYYY-MM-DD 日期")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 日期不存在") from exc
    return value


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{label} 不在允许范围内")


def _quote(value, raw_text, label):
    _text(value, label, MAX_QUOTE)
    if value not in raw_text:
        raise ValueError(f"{label} 必须是原话中连续、完全一致的片段")
    return value


def _common(row, raw_text, label):
    _date(row["date"], f"{label}.date")
    _quote(row["source_quote"], raw_text, f"{label}.source_quote")
    if row["assertion"] != "actual":
        raise ValueError(f"{label}.assertion 必须是 actual；计划、预测、假设和不确定叙述请放入 leftovers")


def _financial(row, label):
    _choice(row["amount_basis"], {"gross", "settlement"}, f"{label}.amount_basis")
    _choice(row["granularity"], {"daily", "transaction"}, f"{label}.granularity")


def _optional_text(row, fields, label):
    for field in fields:
        if field in row:
            _text(row[field], f"{label}.{field}")


def _entry(row, raw_text, label):
    _fields(row, ENTRY_REQUIRED, ENTRY_OPTIONAL, label)
    _common(row, raw_text, label)
    _financial(row, label)
    _choice(row["type"], {"revenue", "purchase", "expense"}, f"{label}.type")
    _text(row["category"], f"{label}.category")
    categories = ({"营业额"} if row["type"] == "revenue" else {"食材成本"}
                  if row["type"] == "purchase" else EXPENSE_CATEGORIES)
    _choice(row["category"], categories, f"{label}.category")
    _number(row["amount"], f"{label}.amount")
    _optional_text(row, {"item", "unit", "platform", "channel"}, label)
    if ("qty" in row) != ("unit" in row):
        raise ValueError(f"{label}.qty 和 unit 必须一起提供，未知时都不提供")
    if "qty" in row:
        if row["type"] != "purchase":
            raise ValueError(f"{label}.qty 和 unit 仅用于采购数量，商品销量请放入 sales")
        _number(row["qty"], f"{label}.qty")
    if "period_month" in row:
        if row["type"] != "expense":
            raise ValueError(f"{label}.period_month 仅用于费用归属月")
        period = row["period_month"]
        if not isinstance(period, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period):
            raise ValueError(f"{label}.period_month 必须是 YYYY-MM")
        _date(period + "-01", f"{label}.period_month")
    return dict(row)


def _sale(row, raw_text, label):
    _fields(row, SALE_REQUIRED, SALE_OPTIONAL, label)
    _common(row, raw_text, label)
    _financial(row, label)
    _text(row["product_name"], f"{label}.product_name")
    if row["product_name"] != row["product_name"].strip():
        raise ValueError(f"{label}.product_name 不能有首尾空白")
    _text(row["unit"], f"{label}.unit")
    _number(row["qty"], f"{label}.qty")
    if row["amount"] is not None:
        _number(row["amount"], f"{label}.amount")
    _optional_text(row, SALE_OPTIONAL, label)
    return dict(row)


def _observation_rule(row):
    # Use only the existing, nonfinancial observation vocabulary. The regexes
    # are never executed here: the Agent has already supplied the domain facts.
    kind, measure = row["kind"], row["measure"]
    if not isinstance(kind, str) or kind not in RECORD_TYPES or not isinstance(measure, str):
        raise ValueError("observations.kind 必须是已支持的非金额观测类型")
    rules = [rule for rule in RECORD_TYPES[kind] if rule["measure"] == measure]
    if not rules:
        raise ValueError("observations.measure 与 kind 不匹配")
    return rules[0]


def _observation(row, raw_text, label):
    _fields(row, OBS_REQUIRED, OBS_OPTIONAL, label)
    _common(row, raw_text, label)
    rule = _observation_rule(row)
    if ("value_num" in row) == ("value_text" in row):
        raise ValueError(f"{label} 必须且只能提供 value_num 或 value_text")
    text_kind = bool(rule.get("text"))
    if text_kind:
        value = _text(row.get("value_text"), f"{label}.value_text")
        _choice(value, WEATHER if row["kind"] == "天气" else ATTENDANCE, f"{label}.value_text")
    else:
        _number(row.get("value_num"), f"{label}.value_num")
    if "unit" in row and row["unit"] != rule.get("unit", ""):
        raise ValueError(f"{label}.unit 与 kind/measure 不匹配")
    _optional_text(row, {"subject"}, label)
    return dict(row)


def validate_structured(raw_text, structured):
    """Return a detached, validated payload; no defaults, truncation, or rounding."""
    _fields(structured, SECTIONS, set(), "structured")
    for key in SECTIONS:
        if not isinstance(structured[key], list):
            raise ValueError(f"structured.{key} 必须是数组")
    row_count = sum(len(structured[key]) for key in ("entries", "sales", "observations"))
    if row_count > MAX_ROWS or len(structured["leftovers"]) > MAX_ROWS:
        raise ValueError(f"结构化记录合计不能超过 {MAX_ROWS} 条，leftovers 也不能超过 {MAX_ROWS} 条")
    if not row_count and not structured["leftovers"]:
        raise ValueError("structured 至少需要一条实际记录或原话待处理片段")
    return {
        "entries": [_entry(row, raw_text, f"entries[{i}]") for i, row in enumerate(structured["entries"])],
        "sales": [_sale(row, raw_text, f"sales[{i}]") for i, row in enumerate(structured["sales"])],
        "observations": [_observation(row, raw_text, f"observations[{i}]")
                         for i, row in enumerate(structured["observations"])],
        "leftovers": [_quote(value, raw_text, f"leftovers[{i}]")
                      for i, value in enumerate(structured["leftovers"])],
    }


def persist_structured(store, shop_id, raw_text, normalized, *, source="agent", peer=None,
                       dedup_ref=None, received_at=None, context=None, intent=None, profile_id=None):
    """Persist validated data via Store; the caller atomically finishes the receipt.

    New structured captures commit in one transaction, so a pre-existing raw
    record without its response is legacy evidence requiring review, not a safe
    opportunity to reinterpret the original message with new normalized data.
    """
    with store.transaction():
        dates = [row["date"] for key in ("entries", "sales", "observations") for row in normalized[key]]
        occurred_at = max(dates) if dates else (received_at[:10] if received_at else dt.date.today().isoformat())
        base_date = received_at[:10] if received_at else occurred_at
        record = store.record_add(
            shop_id, raw_text, source=source, peer=peer, dedup_ref=dedup_ref,
            received_at=received_at, occurred_at=occurred_at, parse_base_date=base_date,
            parser_version=PARSER_VERSION, context=context, intent=intent or "record",
            profile_id=profile_id, routing_status="classified")
        if record["duplicate"]:
            # AgentService checks existing records before this call. Retain a
            # fail-closed guard for other callers and future Store backends.
            raise ValueError("已存在的原话不能再次投影，请核对原请求的完整响应")
        entries = [{**row, "shop_id": shop_id, "note": row["source_quote"]} for row in normalized["entries"]]
        entry_ids = store.add_entries(entries, source=source, raw_text=raw_text, record_id=record["id"])
        sales = []
        for row in normalized["sales"]:
            product = store.product_find(shop_id, row["product_name"])
            product = product or store.product_upsert(shop_id, row["product_name"])
            sales.append({**row, "product_id": product["id"], "raw_text": raw_text})
        sale_ids = store.add_sales(shop_id, sales, source=source, record_id=record["id"])
        observations = []
        for row in normalized["observations"]:
            observations.append({
                "record_id": record["id"], "occurred_at": row["date"],
                "kind": row["kind"], "measure": row["measure"], "subject": row.get("subject"),
                "value_num": row.get("value_num"), "value_text": row.get("value_text"),
                "unit": row.get("unit", _observation_rule(row).get("unit", "")),
                "source_span": row["source_quote"], "confidence": 1.0,
            })
        rows = dedup_keys(normalize(store, shop_id, observations))
        observation_ids = store.observation_add(shop_id, rows)
        status = "partial" if normalized["leftovers"] else "parsed"
        store.record_set_status(record["id"], status, PARSER_VERSION)
        parsed = {"intent": "record", "entries": normalized["entries"], "sales": normalized["sales"],
                  "leftovers": normalized["leftovers"], "mode": "agent_structured", "parser_version": PARSER_VERSION}
        return {"record": {**record, "parse_status": status}, "duplicate": False, "status": status,
                "parsed": parsed, "entry_ids": entry_ids, "sale_ids": sale_ids,
                "observations": rows, "observation_ids": observation_ids, "normalization": normalized}
