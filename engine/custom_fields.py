"""随口记 · 工作空间自定义字段。

通用账目仍是财务事实。这里只校验和保存业务属性，
不把自定义数字加入收入、成本或利润，也不把建议当成已生效配置。
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import re
import unicodedata
from decimal import Decimal

from .model import TYPES, new_id, now_iso, round2


VALUE_TYPES = ("text", "number", "enum", "date")
RECORD_TYPES = ("entry", "sale", "observation", "thought")
SUGGESTION_TYPES = ("field", "mapping", "alias", "config", "value_revision")
SUGGESTION_STATES = ("pending", "accepted", "rejected", "stale")
MAX_ENABLED_FIELDS = 20
MAX_FIELDS = 100
MAX_ENABLED_OPTIONS = 50
MAX_OPTIONS = 200
MAX_PENDING_SUGGESTIONS = 50
MAX_REVISION_TARGETS = 100
PREVIEW_TTL_SECONDS = 1800
RESERVED_KEYS = {
    "id", "shop_id", "workspace_id", "date", "type", "category", "amount",
    "item", "qty", "unit", "note", "source", "raw_text", "batch_id",
    "platform", "channel", "amount_basis", "external_id", "source_system",
    "granularity", "period_month", "product_id", "product_name",
    "product_category", "kind", "measure", "subject", "value_num",
    "value_text", "confidence", "title", "body", "tags", "source_quote",
    "custom_fields", "custom_fields_version", "request_id", "confirmed",
}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUMBER_RE = re.compile(r"^-?(?:0|[1-9]\d{0,17})(?:\.\d{1,6})?$")
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f]")
_SCOPE_CONDITIONS = {
    "entry": ("entry_types", "categories"),
    "sale": ("product_categories",),
    "observation": ("kinds",),
    "thought": (),
}
_VALUE_TABLES = {
    "entry": "custom_entry_value",
    "sale": "custom_sale_value",
    "observation": "custom_observation_value",
    "thought": "custom_thought_value",
}


class CustomFieldError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _reject(code: str, message: str):
    raise CustomFieldError(code, message)


def _text(value, *, minimum: int, maximum: int, label: str,
          controls: bool = False) -> str:
    if not isinstance(value, str):
        _reject("invalid_value", f"{label}必须是文本")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _reject("invalid_value", f"{label}不是有效 UTF-8")
    if "\u0000" in value or (controls and _CONTROL_RE.search(value)):
        _reject("invalid_value", f"{label}含有禁止字符")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not minimum <= len(normalized) <= maximum:
        _reject("invalid_value", f"{label}长度必须为 {minimum}–{maximum} 字符")
    return normalized


def canonical_number(value) -> str:
    """保留十进制字符串；拒绝浮点、布尔、指数和非有限数。"""
    if isinstance(value, bool) or not isinstance(value, str):
        _reject("invalid_value", "自定义数字必须使用十进制字符串")
    text = value.strip()
    if not _NUMBER_RE.fullmatch(text):
        _reject("invalid_value", "自定义数字格式不合法")
    sign, digits = ("-", text[1:]) if text.startswith("-") else ("", text)
    whole, _, fraction = digits.partition(".")
    if len(whole + fraction) > 18:
        _reject("invalid_value", "自定义数字最多 18 位")
    canonical = whole if not fraction else f"{whole}.{fraction}"
    return "0" if canonical in ("0", "0.0") else f"{sign}{canonical}"


def canonical_date(value) -> str:
    text = _text(value, minimum=10, maximum=10, label="日期")
    match = _DATE_RE.fullmatch(text)
    if not match:
        _reject("invalid_value", "日期必须为 YYYY-MM-DD")
    year, month, day = (int(part) for part in match.groups())
    try:
        import datetime as _dt
        _dt.date(year, month, day)
    except ValueError:
        _reject("invalid_value", "日期不存在")
    return text


def normalize_key(value) -> str:
    key = _text(value, minimum=1, maximum=48, label="内部键", controls=True)
    if not _KEY_RE.fullmatch(key) or key in RESERVED_KEYS:
        _reject("invalid_key", "内部键不合法或与通用字段冲突")
    return key


def display_identity(value) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def source_identity(value) -> str:
    """来源原文只统一 NFC 和首尾空白，不用大小写把两个业务实体合成一个。"""
    return unicodedata.normalize("NFC", value).strip()


def normalize_scopes(scopes) -> list:
    if not isinstance(scopes, list) or not scopes:
        _reject("invalid_scope", "字段至少需要一个适用范围")
    seen, normalized = set(), []
    for scope in scopes:
        if not isinstance(scope, dict):
            _reject("invalid_scope", "适用范围格式不正确")
        record_type = scope.get("record_type")
        if record_type not in RECORD_TYPES or record_type in seen:
            _reject("invalid_scope", "适用目标重复或不受支持")
        seen.add(record_type)
        allowed = set(_SCOPE_CONDITIONS[record_type])
        if set(scope) - {"record_type", *allowed}:
            _reject("invalid_scope", "适用范围包含不支持的条件")
        item = {"record_type": record_type}
        for name in allowed:
            values = scope.get(name, [])
            if values is None:
                values = []
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                _reject("invalid_scope", "范围条件必须是非空文本列表")
            cleaned = [_text(v, minimum=1, maximum=40, label="范围条件", controls=True) for v in values]
            if len(set(cleaned)) != len(cleaned):
                _reject("invalid_scope", "范围条件不能重复")
            if record_type == "entry" and name == "entry_types" and any(v not in TYPES for v in cleaned):
                _reject("invalid_scope", "账目类型不受支持")
            item[name] = cleaned
        normalized.append(item)
    return normalized


def scope_matches(scopes, record_type: str, attributes: dict | None = None) -> bool:
    """多个目标按 OR；一个目标内部条件按 AND，同一条件的多个值按 OR。"""
    attributes = attributes or {}
    for scope in normalize_scopes(scopes) if not isinstance(scopes, list) else scopes:
        if scope["record_type"] != record_type:
            continue
        matched = True
        for name in _SCOPE_CONDITIONS[record_type]:
            allowed = scope.get(name) or []
            if allowed and attributes.get(name) not in allowed:
                matched = False
        if matched:
            return True
    return False


def _load_json(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _sort_key(row):
    """领域日期、写入时间、稳定 ID。观测用发生日期，不要求它有账目的 date 列。"""
    occurred = row.get("occurred_at") or ""
    if not isinstance(occurred, str):
        occurred = ""
    date = row.get("date") or occurred[:10]
    created = row.get("created_at") or ""
    return (str(date or ""), str(created or ""), str(row["id"]))


def _query_binding(record_type, start, end, filters, version):
    payload = {"record_type": record_type, "start": start, "end": end,
               "filters": filters, "custom_fields_version": version}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _spreadsheet_text(text):
    """表格导出的显示文本。不改库内原值。"""
    if text[:1] in "=+-@":
        return "'" + text
    return text


def _csv_text(headers, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue()


_METRICS = {
    "entry": {"count", "purchase", "expense", "revenue"},
    "sale": {"count", "qty", "amount"},
    "observation": {"count"},
    "thought": {"count"},
}
_FILTER_OPS = {
    "text": {"eq", "contains", "missing", "not_applicable"},
    "enum": {"in", "missing", "not_applicable"},
    "number": {"between", "missing", "not_applicable"},
    "date": {"between", "missing", "not_applicable"},
}


def _public_field(row) -> dict:
    return {
        "field_id": row["id"],
        "workspace_id": row["shop_id"],
        "key": row["key"],
        "display_name": row["display_name"],
        "value_type": row["value_type"],
        "scopes": _load_json(row["scopes"], []),
        "required": bool(row["required"]),
        "unit": row["unit"],
        "enabled": bool(row["enabled"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _definition_snapshot(field, options=None) -> dict:
    return {**field, "options": options or []}


class CustomFieldStore:
    """覆盖在 SqliteStore 上的字段事务；调用方必须已验证工作空间归属。"""

    def __init__(self, store):
        self.store = store

    def config_version(self, workspace_id: str) -> int:
        row = self.store.conn.execute(
            "SELECT version FROM custom_field_config WHERE shop_id=?",
            (workspace_id,)).fetchone()
        return int(row[0]) if row else 0

    def list_fields(self, workspace_id: str, *, include_disabled: bool = True) -> dict:
        sql = "SELECT * FROM custom_field WHERE shop_id=?"
        args = [workspace_id]
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY created_at, id"
        fields = []
        for row in self.store.conn.execute(sql, args):
            field = _public_field(row)
            if field["value_type"] == "enum":
                field["options"] = self._options(workspace_id, field["field_id"])
            fields.append(field)
        return {"custom_fields_version": self.config_version(workspace_id), "fields": fields}

    def create_field(self, workspace_id: str, spec: dict, *, actor: str,
                     confirmation: dict) -> dict:
        key = normalize_key(spec.get("key"))
        name = _text(spec.get("display_name"), minimum=1, maximum=40,
                     label="显示名", controls=True)
        value_type = spec.get("value_type")
        if value_type not in VALUE_TYPES:
            _reject("invalid_type", "字段类型不受支持")
        scopes = normalize_scopes(spec.get("scopes"))
        required = bool(spec.get("required", False))
        unit = spec.get("unit")
        if unit is not None:
            unit = _text(unit, minimum=1, maximum=20, label="单位", controls=True)
        if unit and value_type != "number":
            _reject("invalid_value", "只有数字字段可以设置单位")
        options = spec.get("options") or []
        if value_type != "enum" and options:
            _reject("invalid_value", "只有枚举字段可以设置选项")
        if value_type == "enum" and not options:
            _reject("invalid_value", "枚举字段至少需要一个选项")
        self._assert_capacity(workspace_id, enabling=True)
        self._assert_unique(workspace_id, key, name)
        field_id = new_id("cf")
        created = now_iso()
        with self.store.transaction():
            self.store.conn.execute(
                """INSERT INTO custom_field(
                    id, shop_id, key, display_name, name_identity, value_type,
                    scopes, required, unit, enabled, revision, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,1,?,?)""",
                (field_id, workspace_id, key, name, display_identity(name),
                 value_type, json.dumps(scopes, ensure_ascii=False),
                 int(required), unit, created, created))
            public_options = self._insert_options(workspace_id, field_id, options, created)
            field = self._field(workspace_id, field_id)
            field["options"] = public_options
            version = self._bump(workspace_id, created)
            self._audit(workspace_id, "field.create", field_id, None, field,
                        actor, confirmation, version, created)
        return {"custom_fields_version": version, "field": field}

    def update_field(self, workspace_id: str, field_id: str, changes: dict, *,
                     actor: str, confirmation: dict) -> dict:
        current = self._require(workspace_id, field_id)
        if any(name in changes for name in ("key", "value_type", "field_id")):
            _reject("immutable_field", "字段键和类型不能修改")
        name = current["display_name"]
        if "display_name" in changes:
            name = _text(changes["display_name"], minimum=1, maximum=40,
                         label="显示名", controls=True)
            self._assert_unique(workspace_id, None, name, ignore=field_id)
        scopes = current["scopes"]
        if "scopes" in changes:
            scopes = normalize_scopes(changes["scopes"])
        required = current["required"] if "required" not in changes else bool(changes["required"])
        enabled = current["enabled"] if "enabled" not in changes else bool(changes["enabled"])
        if enabled and not current["enabled"]:
            self._assert_capacity(workspace_id, enabling=True)
        updated = now_iso()
        with self.store.transaction():
            self.store.conn.execute(
                """UPDATE custom_field SET display_name=?, name_identity=?, scopes=?,
                          required=?, enabled=?, revision=revision+1, updated_at=?
                   WHERE shop_id=? AND id=?""",
                (name, display_identity(name), json.dumps(scopes, ensure_ascii=False),
                 int(required), int(enabled), updated, workspace_id, field_id))
            after = self._field(workspace_id, field_id)
            if after["value_type"] == "enum":
                after["options"] = self._options(workspace_id, field_id)
            version = self._bump(workspace_id, updated)
            self._audit(workspace_id, "field.update", field_id, current, after,
                        actor, confirmation, version, updated)
        return {"custom_fields_version": version, "field": after}

    def add_option(self, workspace_id: str, field_id: str, display_name: str, *,
                   actor: str, confirmation: dict) -> dict:
        field = self._require(workspace_id, field_id)
        if field["value_type"] != "enum":
            _reject("invalid_type", "只有枚举字段可以增加选项")
        name = _text(display_name, minimum=1, maximum=40, label="选项名称", controls=True)
        counts = self._option_counts(workspace_id, field_id)
        if counts["total"] >= MAX_OPTIONS or counts["enabled"] >= MAX_ENABLED_OPTIONS:
            _reject("limit_exceeded", "枚举选项数量已达上限")
        self._assert_option_unique(workspace_id, field_id, name)
        created = now_iso()
        option_id = new_id("opt")
        with self.store.transaction():
            self.store.conn.execute(
                """INSERT INTO custom_field_option(
                    id, shop_id, field_id, display_name, name_identity, enabled,
                    position, created_at, updated_at)
                   VALUES(?,?,?,?,?,1,?,?,?)""",
                (option_id, workspace_id, field_id, name, display_identity(name),
                 counts["total"], created, created))
            self.store.conn.execute(
                "UPDATE custom_field SET revision=revision+1, updated_at=? WHERE shop_id=? AND id=?",
                (created, workspace_id, field_id))
            after = self._field(workspace_id, field_id)
            after["options"] = self._options(workspace_id, field_id)
            version = self._bump(workspace_id, created)
            self._audit(workspace_id, "option.create", field_id, field, after,
                        actor, confirmation, version, created)
        return {"custom_fields_version": version, "field": after}

    def set_option_enabled(self, workspace_id: str, field_id: str, option_id: str,
                           enabled: bool, *, actor: str, confirmation: dict) -> dict:
        field = self._require(workspace_id, field_id)
        option = self._option(workspace_id, field_id, option_id)
        if enabled and not option["enabled"]:
            counts = self._option_counts(workspace_id, field_id)
            if counts["enabled"] >= MAX_ENABLED_OPTIONS:
                _reject("limit_exceeded", "启用选项数量已达上限")
        updated = now_iso()
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE custom_field_option SET enabled=?, updated_at=? WHERE shop_id=? AND id=?",
                (int(enabled), updated, workspace_id, option_id))
            self.store.conn.execute(
                "UPDATE custom_field SET revision=revision+1, updated_at=? WHERE shop_id=? AND id=?",
                (updated, workspace_id, field_id))
            after = self._field(workspace_id, field_id)
            after["options"] = self._options(workspace_id, field_id)
            version = self._bump(workspace_id, updated)
            self._audit(workspace_id, "option.update", field_id, field, after,
                        actor, confirmation, version, updated)
        return {"custom_fields_version": version, "field": after}

    def validate_assignment(self, workspace_id: str, record_type: str,
                            attributes: dict, assignments, *,
                            config_version: int | None) -> list:
        """校验新记录的字段值。缺少必填不拒绝，非法显式值拒绝。"""
        if config_version is not None and config_version != self.config_version(workspace_id):
            _reject("config_version_conflict", "字段配置已变更，请重新读取后提交")
        if assignments is None:
            assignments = []
        if not isinstance(assignments, list) or len(assignments) > MAX_ENABLED_FIELDS:
            _reject("invalid_value", "自定义字段列表不合法")
        seen, accepted = set(), []
        catalog = {field["field_id"]: field for field in
                   self.list_fields(workspace_id)["fields"]}
        for item in assignments:
            if not isinstance(item, dict):
                _reject("invalid_value", "字段值格式不正确")
            field_id = item.get("field_id")
            if not isinstance(field_id, str) or field_id in seen:
                _reject("duplicate_field", "同一目标不能重复提交字段")
            seen.add(field_id)
            field = catalog.get(field_id)
            if not field:
                _reject("unknown_field", "字段不存在或不属于本工作空间")
            if not field["enabled"]:
                _reject("field_disabled", "停用字段不能写入新记录")
            if not scope_matches(field["scopes"], record_type, attributes):
                _reject("scope_mismatch", "字段不适用于这条记录")
            accepted.append(self._typed_value(field, item))
        return accepted

    def write_values(self, workspace_id: str, record_type: str, target_id: str,
                     assignments, *, attributes: dict, config_version: int | None,
                     source: dict | None = None) -> dict:
        """在调用方事务内写入已校验的值，并记录适用必填缺口。"""
        if record_type not in _VALUE_TABLES:
            _reject("invalid_scope", "记录类型不受支持")
        accepted = self.validate_assignment(
            workspace_id, record_type, attributes, assignments, config_version=config_version)
        gaps = self.missing_required(workspace_id, record_type, attributes, assignments)
        table = _VALUE_TABLES[record_type]
        created = now_iso()
        source_json = json.dumps(source or {}, ensure_ascii=False, sort_keys=True)
        for item in accepted:
            if item["source_quote"] is not None and source and "raw_text" in source:
                if item["source_quote"] not in source["raw_text"]:
                    _reject("invalid_source", "字段来源必须是原话中的连续片段")
            self.store.conn.execute(
                f"""INSERT INTO {table}(
                    shop_id, target_id, field_id, value_type, text_value, number_value,
                    option_id, date_value, source_quote, source_json, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (workspace_id, target_id, item["field_id"], item["value_type"],
                 item["text_value"], item["number_value"], item["option_id"],
                 item["date_value"], item["source_quote"], source_json, created, created))
        version = self.config_version(workspace_id)
        for gap in gaps:
            self.store.conn.execute(
                """INSERT INTO custom_field_gap(
                    id, shop_id, record_type, target_id, field_id, status,
                    config_version, created_at, updated_at)
                   VALUES(?,?,?,?,?,'missing',?,?,?)""",
                (new_id("gap"), workspace_id, record_type, target_id,
                 gap["field_id"], version, created, created))
        return {"written": len(accepted), "missing_required": gaps,
                "custom_fields_version": version}

    def query_records(self, workspace_id: str, record_type: str, *, start="0000", end="9999",
                      filters=None, page_size=30, before=None) -> dict:
        if record_type not in RECORD_TYPES:
            _reject("invalid_scope", "记录类型不受支持")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            _reject("invalid_page", "page_size 必须是 1 到 100")
        if not isinstance(start, str) or not isinstance(end, str) or start > end:
            _reject("invalid_range", "日期范围不正确")
        version = self.config_version(workspace_id)
        filters = self._normalize_filters(filters)
        binding = _query_binding(record_type, start, end, filters, version)
        if before is not None:
            if (not isinstance(before, dict) or before.get("binding") != binding
                    or not isinstance(before.get("key"), list) or len(before["key"]) != 3
                    or not all(isinstance(part, str) for part in before["key"])):
                _reject("cursor_stale", "查询条件或字段配置已变化，请重新查询")
        compiled = [self._compile_filter(workspace_id, record_type, item) for item in filters]
        rows = self._domain_rows(workspace_id, record_type, start, end)
        matched = [row for row in rows if all(check(row) for check in compiled)]
        matched.sort(key=_sort_key, reverse=True)
        if before:
            key = tuple(before["key"])
            matched = [row for row in matched if _sort_key(row) < key]
        page = matched[:page_size]
        next_before = None
        if len(matched) > page_size and page:
            next_before = {"binding": binding, "key": list(_sort_key(page[-1]))}
        return {"record_type": record_type, "custom_fields_version": version,
                "items": [self._query_item(workspace_id, record_type, row) for row in page],
                "next_before": next_before}

    def group_records(self, workspace_id: str, record_type: str, field_id: str, *,
                      start="0000", end="9999", metric="count") -> dict:
        if record_type not in RECORD_TYPES:
            _reject("invalid_scope", "记录类型不受支持")
        if metric not in _METRICS[record_type]:
            _reject("invalid_metric", "该记录类型不支持这个汇总指标")
        if not isinstance(start, str) or not isinstance(end, str) or start > end:
            _reject("invalid_range", "日期范围不正确")
        field = self._require(workspace_id, field_id)
        if field["value_type"] not in {"text", "enum"}:
            _reject("invalid_group", "首版只能按短文本或枚举分组")
        rows = self._domain_rows(workspace_id, record_type, start, end)
        groups = {}
        for row in rows:
            applicable = scope_matches(field["scopes"], record_type, self._attributes(record_type, row))
            value = self._current_value(workspace_id, record_type, row["id"], field_id)
            if not applicable:
                label, state = "不适用", "not_applicable"
            elif value is None:
                label, state = "未填写", "missing"
            else:
                label, state = self._group_label(field, value), "valued"
            group = groups.setdefault((state, label), {"label": label, "state": state, "count": 0,
                                                       "amount": 0, "qty": 0, "ids": set()})
            if row["id"] in group["ids"]:
                continue
            group["count"] += 1
            group["ids"].add(row["id"])
            if record_type == "entry" and metric in {"purchase", "expense"} and row["type"] == metric:
                group["amount"] += row["amount"]
            if record_type == "sale" and metric in {"qty", "amount"}:
                group["qty"] += row["qty"]
                if row["amount"] is not None:
                    group["amount"] += row["amount"]
        result = []
        for group in groups.values():
            item = {"label": group["label"], "state": group["state"], "count": group["count"]}
            if record_type == "entry" and metric in {"purchase", "expense"}:
                item["amount"] = round2(group["amount"])
                item["basis"] = "payment_date"
            if record_type == "sale" and metric == "qty":
                item["qty"] = group["qty"]
            if record_type == "sale" and metric == "amount":
                item["amount"] = round2(group["amount"])
                item["basis"] = "tagged_sale_amount"
            result.append(item)
        result.sort(key=lambda item: (item["state"] != "valued", item["label"]))
        revenue = self._revenue_split(workspace_id, record_type, field, rows, start, end, metric)
        return {"record_type": record_type, "field_id": field_id, "metric": metric,
                "groups": result, "revenue": revenue,
                "note": "自定义数字不自动求和；营业额仍按既有覆盖规则计算。"}

    def export_records(self, workspace_id: str, record_type: str, *, start="0000", end="9999") -> dict:
        query = self.query_records(workspace_id, record_type, start=start, end=end, page_size=100)
        items = list(query["items"])
        while query["next_before"]:
            query = self.query_records(workspace_id, record_type, start=start, end=end,
                                       page_size=100, before=query["next_before"])
            items.extend(query["items"])
        fields = self.list_fields(workspace_id)["fields"]
        headers = ["记录ID", "日期"] + [self._export_header(field) for field in fields]
        rows = []
        for item in items:
            values = {value["field_id"]: value for value in item["custom_fields"]}
            row = [item["id"], item["date"]]
            for field in fields:
                value = values.get(field["field_id"])
                text = "" if value is None else str(value["display_value"])
                if field["value_type"] == "text":
                    text = _spreadsheet_text(text)
                row.append(text)
            rows.append(row)
        return {"headers": headers, "rows": rows, "count": len(rows),
                "csv": _csv_text(headers, rows),
                "note": "表格文本已加前缀保护，库内原值没有改变。"}

    def values_for(self, workspace_id: str, record_type: str, target_id: str) -> list:
        table = _VALUE_TABLES[record_type]
        rows = self.store.conn.execute(
            f"SELECT * FROM {table} WHERE shop_id=? AND target_id=? ORDER BY field_id",
            (workspace_id, target_id))
        return [self._public_value(row) for row in rows]

    def submit_suggestion(self, workspace_id: str, suggestion: dict, *, actor: str) -> dict:
        kind = suggestion.get("suggestion_type")
        if kind not in SUGGESTION_TYPES:
            _reject("invalid_suggestion", "建议类型不受支持")
        payload = suggestion.get("payload")
        evidence = suggestion.get("evidence") or {}
        if not isinstance(payload, dict) or not isinstance(evidence, dict):
            _reject("invalid_suggestion", "建议内容和证据必须是对象")
        if kind == "value_revision":
            targets = payload.get("targets")
            if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_REVISION_TARGETS:
                _reject("invalid_suggestion", "属性修订候选最多 100 个目标")
        pending = self.store.conn.execute(
            "SELECT COUNT(*) FROM custom_field_suggestion WHERE shop_id=? AND state='pending'",
            (workspace_id,)).fetchone()[0]
        fingerprint = hashlib.sha256(json.dumps(
            {"type": kind, "payload": payload, "evidence": evidence},
            ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        existing = self.store.conn.execute(
            """SELECT * FROM custom_field_suggestion
               WHERE shop_id=? AND fingerprint=? AND state='pending'""",
            (workspace_id, fingerprint)).fetchone()
        if existing:
            return self._public_suggestion(existing)
        if pending >= MAX_PENDING_SUGGESTIONS:
            _reject("limit_exceeded", "待处理建议已达上限")
        created = now_iso()
        suggestion_id = new_id("cfs")
        version = self.config_version(workspace_id)
        with self.store.transaction():
            self.store.conn.execute(
                """INSERT INTO custom_field_suggestion(
                    id, shop_id, suggestion_type, revision, state, payload_json,
                    evidence_json, fingerprint, config_version, created_at, updated_at)
                   VALUES(?,?,?,1,'pending',?,?,?,?,?,?)""",
                (suggestion_id, workspace_id, kind,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                 fingerprint, version, created, created))
            self._audit(workspace_id, "suggestion.create", None, None,
                        {"suggestion_id": suggestion_id, "actor": actor},
                        actor, {}, version or 1, created)
        return self._public_suggestion(self.store.conn.execute(
            "SELECT * FROM custom_field_suggestion WHERE id=?", (suggestion_id,)).fetchone())

    def preview_change(self, workspace_id: str, principal_id: str, change: dict) -> dict:
        if not isinstance(change, dict) or change.get("action") not in {"create", "update"}:
            _reject("invalid_change", "变更预览只支持创建或修改字段")
        if change["action"] == "create":
            normalize_key(change.get("spec", {}).get("key"))
            normalize_scopes(change.get("spec", {}).get("scopes"))
        token = new_id("cfp")
        created = now_iso()
        expires = (_dt.datetime.fromisoformat(created) + _dt.timedelta(seconds=PREVIEW_TTL_SECONDS)).isoformat()
        version = self.config_version(workspace_id)
        payload = json.dumps(change, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.store.transaction():
            self.store.conn.execute(
                """INSERT INTO custom_field_preview(
                    token, shop_id, principal_id, preview_type, fingerprint,
                    payload_json, config_version, created_at, expires_at)
                   VALUES(?,?,?,'config',?,?,?,?,?)""",
                (token, workspace_id, principal_id, fingerprint, payload,
                 version, created, expires))
        return {"preview_token": token, "expires_at": expires,
                "custom_fields_version": version, "change": change}

    def commit_change(self, workspace_id: str, principal_id: str, token: str, *,
                      actor: str, confirmation: dict) -> dict:
        row = self.store.conn.execute(
            """SELECT * FROM custom_field_preview
               WHERE token=? AND shop_id=? AND principal_id=? AND preview_type='config'""",
            (token, workspace_id, principal_id)).fetchone()
        if not row:
            _reject("preview_invalid", "确认预览不存在或不属于当前主体")
        if row["consumed_request_id"]:
            _reject("preview_consumed", "确认预览已经使用")
        if row["expires_at"] <= now_iso() or row["config_version"] != self.config_version(workspace_id):
            _reject("preview_stale", "确认预览已过期或字段配置已变化")
        change = json.loads(row["payload_json"])
        with self.store.transaction():
            if change["action"] == "create":
                result = self.create_field(workspace_id, change["spec"], actor=actor,
                                           confirmation=confirmation)
            else:
                result = self.update_field(workspace_id, change["field_id"], change["changes"],
                                           actor=actor, confirmation=confirmation)
            consumed = self.store.conn.execute(
                """UPDATE custom_field_preview SET consumed_request_id=?
                   WHERE token=? AND consumed_request_id IS NULL""",
                (confirmation.get("request_id", token), token))
            if consumed.rowcount != 1:
                _reject("preview_consumed", "确认预览已经使用")
        return result

    def reject_suggestion(self, workspace_id: str, suggestion_id: str, *, actor: str,
                          confirmation: dict) -> dict:
        row = self.store.conn.execute(
            "SELECT * FROM custom_field_suggestion WHERE shop_id=? AND id=?",
            (workspace_id, suggestion_id)).fetchone()
        if not row or row["state"] != "pending":
            _reject("suggestion_invalid", "待处理建议不存在")
        updated = now_iso()
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE custom_field_suggestion SET state='rejected', updated_at=? WHERE id=?",
                (updated, suggestion_id))
            self._audit(workspace_id, "suggestion.reject", None,
                        self._public_suggestion(row), {"state": "rejected"},
                        actor, confirmation, self.config_version(workspace_id) or 1, updated)
        return self._public_suggestion(self.store.conn.execute(
            "SELECT * FROM custom_field_suggestion WHERE id=?", (suggestion_id,)).fetchone())

    def missing_required(self, workspace_id: str, record_type: str,
                         attributes: dict, assignments) -> list:
        present = {item.get("field_id") for item in assignments or []
                   if isinstance(item, dict)}
        gaps = []
        for field in self.list_fields(workspace_id, include_disabled=False)["fields"]:
            if (field["required"] and field["field_id"] not in present
                    and scope_matches(field["scopes"], record_type, attributes)):
                gaps.append({"field_id": field["field_id"],
                             "display_name": field["display_name"],
                             "status": "missing"})
        return gaps

    def _typed_value(self, field, item) -> dict:
        if "value" not in item:
            _reject("invalid_value", "字段值不能缺失")
        value = item["value"]
        value_type = field["value_type"]
        quote = item.get("source_quote")
        stored = {"field_id": field["field_id"], "value_type": value_type,
                  "text_value": None, "number_value": None,
                  "option_id": None, "date_value": None,
                  "source_quote": quote}
        if quote is not None and (not isinstance(quote, str) or "\u0000" in quote):
            _reject("invalid_source", "字段来源引用不合法")
        if value_type == "text":
            stored["text_value"] = _text(value, minimum=1, maximum=200, label="文本值")
        elif value_type == "number":
            stored["number_value"] = canonical_number(value)
        elif value_type == "date":
            stored["date_value"] = canonical_date(value)
        elif value_type == "enum":
            option_id = value.get("option_id") if isinstance(value, dict) else value
            option = next((row for row in field.get("options", [])
                           if row["option_id"] == option_id), None)
            if not option or not option["enabled"]:
                _reject("invalid_option", "枚举选项不存在或已停用")
            stored["option_id"] = option_id
        return stored

    def _normalize_filters(self, filters):
        if filters is None:
            return []
        if not isinstance(filters, list):
            _reject("invalid_filter", "筛选条件必须是数组")
        return filters

    def _compile_filter(self, workspace_id, record_type, item):
        if not isinstance(item, dict) or set(item) - {"field_id", "op", "value"}:
            _reject("invalid_filter", "筛选条件格式不正确")
        field = self._require(workspace_id, item.get("field_id"))
        operation = item.get("op")
        value = item.get("value")
        if operation not in _FILTER_OPS[field["value_type"]]:
            _reject("invalid_filter", "筛选操作与字段类型不匹配")
        if operation == "between":
            if (not isinstance(value, list) or len(value) != 2
                    or any(not isinstance(part, str) for part in value)):
                _reject("invalid_filter", "区间筛选需要两个文本边界")
            if field["value_type"] == "number":
                try:
                    bounds = (Decimal(value[0]), Decimal(value[1]))
                except Exception:
                    _reject("invalid_filter", "数字区间边界不合法")
                if bounds[0] > bounds[1]:
                    _reject("invalid_filter", "区间下界不能大于上界")
            elif value[0] > value[1]:
                _reject("invalid_filter", "区间下界不能大于上界")
        elif operation == "in":
            if not isinstance(value, list) or not value or any(not isinstance(part, str) or not part for part in value):
                _reject("invalid_filter", "枚举筛选需要选项 ID 列表")
            known = {option["option_id"] for option in field.get("options", [])}
            if any(part not in known for part in value):
                _reject("invalid_filter", "枚举选项不存在或不属于本字段")
        elif operation in {"eq", "contains"}:
            if not isinstance(value, str):
                _reject("invalid_filter", "文本筛选值必须是文本")
        elif "value" in item:
            _reject("invalid_filter", "缺失和不适用筛选不接受比较值")

        def check(row):
            current = self._current_value(workspace_id, record_type, row["id"], field["field_id"])
            applicable = scope_matches(field["scopes"], record_type, self._attributes(record_type, row))
            if operation == "missing":
                return applicable and current is None
            if operation == "not_applicable":
                return not applicable
            if current is None:
                return False
            if operation == "eq":
                return current["text_value"] == value
            if operation == "contains":
                return value in current["text_value"]
            if operation == "in":
                return current["option_id"] in value
            actual = current["number_value"] if field["value_type"] == "number" else current["date_value"]
            if field["value_type"] == "number":
                return Decimal(value[0]) <= Decimal(actual) <= Decimal(value[1])
            return value[0] <= actual <= value[1]

        return check

    def _revenue_split(self, workspace_id, record_type, field, rows, start, end, metric):
        if record_type != "entry" or metric != "revenue":
            return None
        from .accounting import revenue_days
        revenue_rows = [row for row in rows if row["type"] == "revenue"]
        sales = self.store.list_sales(workspace_id, start, end)
        daily = revenue_days(revenue_rows, sales, self.store, workspace_id)
        total = (round2(sum(day["amount"] or 0 for day in daily))
                 if daily and all(day["amount"] is not None for day in daily) else None)
        authoritative = []
        for day in daily:
            comparisons = day.get("comparisons") or []
            if not comparisons or any(item.get("has_detail") for item in comparisons):
                continue
            matching = [row for row in revenue_rows if row["date"] == day["date"]]
            if len(matching) == 1:
                authoritative.append(matching[0])
        groups, unassigned = {}, 0
        for row in authoritative:
            applicable = scope_matches(field["scopes"], "entry", self._attributes("entry", row))
            value = self._current_value(workspace_id, "entry", row["id"], field["field_id"])
            if applicable and value is not None:
                label = self._group_label(field, value)
                groups[label] = groups.get(label, 0) + row["amount"]
            else:
                unassigned += row["amount"]
        tagged = round2(sum(sale["amount"] or 0 for sale in sales
                            if self._current_value(workspace_id, "sale", sale["id"], field["field_id"])))
        return {"status": "not_attributable", "total": total,
                "authoritative_groups": [
                    {"label": label, "amount": round2(amount)} for label, amount in groups.items()],
                "unassigned_authoritative_amount": round2(unassigned),
                "tagged_sale_amount": tagged,
                "message": "无法按此字段完整拆分营业额"}

    def _domain_rows(self, workspace_id, record_type, start, end):
        if record_type == "entry":
            return self.store.list_entries(workspace_id, start, end)
        if record_type == "sale":
            return self.store.list_sales(workspace_id, start, end)
        if record_type == "observation":
            return [row for row in self.store.observation_list(workspace_id, start, end)
                    if str(row.get("dedup_key", "")).startswith("record:")]
        thoughts = self.store.thought_list(workspace_id, start=start, end=end, page_size=100)
        rows = list(thoughts["items"])
        while thoughts.get("next_before"):
            thoughts = self.store.thought_list(workspace_id, start=start, end=end, page_size=100,
                                               before=thoughts["next_before"])
            rows.extend(thoughts["items"])
        return rows

    def _attributes(self, record_type, row):
        return {"entry_types": row.get("type"), "categories": row.get("category"),
                "product_categories": row.get("product_category"), "kinds": row.get("kind")}

    def _current_value(self, workspace_id, record_type, target_id, field_id):
        table = _VALUE_TABLES[record_type]
        return self.store.conn.execute(
            f"SELECT * FROM {table} WHERE shop_id=? AND target_id=? AND field_id=?",
            (workspace_id, target_id, field_id)).fetchone()

    def _query_item(self, workspace_id, record_type, row):
        values = self.values_for(workspace_id, record_type, row["id"])
        for value in values:
            value["display_value"] = self._display_value(workspace_id, value)
        date, created_at, _record_id = _sort_key(row)
        return {"id": row["id"], "date": date, "created_at": created_at,
                "custom_fields": values}

    def _display_value(self, workspace_id, value):
        if value["value_type"] == "enum":
            option = self.store.conn.execute(
                "SELECT display_name, enabled FROM custom_field_option WHERE shop_id=? AND id=?",
                (workspace_id, value["value"])).fetchone()
            suffix = "" if option and option["enabled"] else "（已停用）"
            return f"{option['display_name']}{suffix}" if option else value["value"]
        return value["value"]

    def _group_label(self, field, value):
        if field["value_type"] == "text":
            return value["text_value"]
        option = next((item for item in field.get("options", [])
                       if item["option_id"] == value["option_id"]), None)
        return option["display_name"] if option else value["option_id"]

    def _export_header(self, field):
        generic = {"日期", "金额", "分类", "记录ID"}
        name = field["display_name"]
        if name in generic:
            name = f"自定义：{name}"
        if not field["enabled"]:
            name += "（已停用）"
        return name

    def _public_value(self, row) -> dict:
        value = row["text_value"]
        if row["value_type"] == "number":
            value = row["number_value"]
        elif row["value_type"] == "enum":
            value = row["option_id"]
        elif row["value_type"] == "date":
            value = row["date_value"]
        return {"field_id": row["field_id"], "value_type": row["value_type"],
                "value": value, "source_quote": row["source_quote"],
                "source": _load_json(row["source_json"], {})}

    def _public_suggestion(self, row) -> dict:
        return {"suggestion_id": row["id"], "workspace_id": row["shop_id"],
                "suggestion_type": row["suggestion_type"], "revision": row["revision"],
                "state": row["state"], "payload": _load_json(row["payload_json"], {}),
                "evidence": _load_json(row["evidence_json"], {}),
                "custom_fields_version": row["config_version"]}

    def _field(self, workspace_id, field_id):
        row = self.store.conn.execute(
            "SELECT * FROM custom_field WHERE shop_id=? AND id=?",
            (workspace_id, field_id)).fetchone()
        return _public_field(row) if row else None

    def _require(self, workspace_id, field_id):
        field = self._field(workspace_id, field_id)
        if not field:
            _reject("unknown_field", "字段不存在或不属于本工作空间")
        return field

    def _options(self, workspace_id, field_id):
        rows = self.store.conn.execute(
            """SELECT * FROM custom_field_option
               WHERE shop_id=? AND field_id=? ORDER BY position, id""",
            (workspace_id, field_id))
        return [{"option_id": row["id"], "display_name": row["display_name"],
                 "enabled": bool(row["enabled"]), "position": row["position"]} for row in rows]

    def _option(self, workspace_id, field_id, option_id):
        row = next((item for item in self._options(workspace_id, field_id)
                    if item["option_id"] == option_id), None)
        if not row:
            _reject("invalid_option", "枚举选项不存在")
        return row

    def _insert_options(self, workspace_id, field_id, options, created):
        if len(options) > MAX_ENABLED_OPTIONS:
            _reject("limit_exceeded", "启用选项数量已达上限")
        public = []
        seen = set()
        for position, option in enumerate(options):
            name = option.get("display_name") if isinstance(option, dict) else option
            name = _text(name, minimum=1, maximum=40, label="选项名称", controls=True)
            identity = display_identity(name)
            if identity in seen:
                _reject("duplicate_option", "枚举选项名称重复")
            seen.add(identity)
            option_id = new_id("opt")
            self.store.conn.execute(
                """INSERT INTO custom_field_option(
                    id, shop_id, field_id, display_name, name_identity, enabled,
                    position, created_at, updated_at) VALUES(?,?,?,?,?,1,?,?,?)""",
                (option_id, workspace_id, field_id, name, identity,
                 position, created, created))
            public.append({"option_id": option_id, "display_name": name,
                           "enabled": True, "position": position})
        return public

    def _option_counts(self, workspace_id, field_id):
        row = self.store.conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(enabled), 0)
               FROM custom_field_option WHERE shop_id=? AND field_id=?""",
            (workspace_id, field_id)).fetchone()
        return {"total": row[0], "enabled": row[1]}

    def _assert_capacity(self, workspace_id, *, enabling: bool):
        row = self.store.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM custom_field WHERE shop_id=?",
            (workspace_id,)).fetchone()
        if row[0] >= MAX_FIELDS or (enabling and row[1] >= MAX_ENABLED_FIELDS):
            _reject("limit_exceeded", "字段数量已达上限，请复用或重新启用已有字段")

    def _assert_unique(self, workspace_id, key, display_name, ignore=None):
        identity = display_identity(display_name)
        row = self.store.conn.execute(
            """SELECT id FROM custom_field
               WHERE shop_id=? AND id!=COALESCE(?, '')
                 AND (key=? OR name_identity=?)""",
            (workspace_id, ignore or "", key or "", identity)).fetchone()
        if row:
            _reject("duplicate_field", "字段键或显示名已存在")

    def _assert_option_unique(self, workspace_id, field_id, display_name):
        row = self.store.conn.execute(
            """SELECT 1 FROM custom_field_option
               WHERE shop_id=? AND field_id=? AND name_identity=?""",
            (workspace_id, field_id, display_identity(display_name))).fetchone()
        if row:
            _reject("duplicate_option", "枚举选项名称已存在")

    def _bump(self, workspace_id, updated):
        self.store.conn.execute(
            """INSERT INTO custom_field_config(shop_id, version, updated_at)
               VALUES(?, 1, ?)
               ON CONFLICT(shop_id) DO UPDATE SET
                 version=version+1, updated_at=excluded.updated_at""",
            (workspace_id, updated))
        return self.config_version(workspace_id)

    def _audit(self, workspace_id, action, field_id, before, after,
               actor, confirmation, version, created):
        self.store.conn.execute(
            """INSERT INTO custom_field_audit(
                id, shop_id, action, field_id, before_json, after_json,
                actor, confirmation_json, config_version, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (new_id("cfa"), workspace_id, action, field_id,
             json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
             json.dumps(after, ensure_ascii=False, sort_keys=True),
             actor, json.dumps(confirmation or {}, ensure_ascii=False, sort_keys=True),
             version, created))

    def maintain_alias(self, workspace_id: str, field_id: str, source_text: str,
                       target, *, actor: str, confirmation: dict, enabled: bool = True) -> dict:
        """保存后续写入使用的值别名；不改写已经保存的历史值。"""
        field = self._require(workspace_id, field_id)
        source = _text(source_text, minimum=1, maximum=200, label="别名原文", controls=True)
        identity = source_identity(source)
        target_text, option_id = None, None
        if field["value_type"] == "enum":
            option_id = target.get("option_id") if isinstance(target, dict) else target
            if not isinstance(option_id, str):
                _reject("invalid_option", "枚举别名必须指向选项")
            self._option(workspace_id, field_id, option_id)
        else:
            typed = self._typed_value(field, {"field_id": field_id, "value": target})
            target_text = typed["text_value"] or typed["number_value"] or typed["date_value"]
        created = now_iso()
        existing = self.store.conn.execute(
            """SELECT * FROM custom_field_alias
               WHERE shop_id=? AND field_id=? AND source_identity=?""",
            (workspace_id, field_id, identity)).fetchone()
        with self.store.transaction():
            if existing:
                self.store.conn.execute(
                    """UPDATE custom_field_alias
                       SET source_text=?, target_text=?, option_id=?, enabled=?, updated_at=?
                       WHERE shop_id=? AND id=?""",
                    (source, target_text, option_id, int(enabled), created,
                     workspace_id, existing["id"]))
                alias_id = existing["id"]
            else:
                alias_id = new_id("cfaa")
                self.store.conn.execute(
                    """INSERT INTO custom_field_alias(
                        id, shop_id, field_id, source_text, source_identity, target_text,
                        option_id, enabled, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (alias_id, workspace_id, field_id, source, identity, target_text,
                     option_id, int(enabled), created, created))
            version = self._bump(workspace_id, created)
            self._audit(workspace_id, "alias.update", field_id,
                        dict(existing) if existing else None,
                        {"alias_id": alias_id, "source_text": source, "target_text": target_text,
                         "option_id": option_id, "enabled": bool(enabled)},
                        actor, confirmation, version, created)
        return {"custom_fields_version": version,
                "alias": {"alias_id": alias_id, "field_id": field_id, "source_text": source,
                          "target": option_id or target_text, "enabled": bool(enabled)}}

    def maintain_import_mapping(self, workspace_id: str, source_header: str, target: dict, *,
                                actor: str, confirmation: dict) -> dict:
        """记住来源列以后导入到哪个字段；不改写已导入的账。"""
        header = _text(source_header, minimum=1, maximum=80, label="来源列名", controls=True)
        identity = source_identity(header)
        if not isinstance(target, dict):
            _reject("invalid_mapping", "列映射目标格式不正确")
        kind = target.get("kind")
        generic, field_id = None, None
        if kind == "generic":
            generic = target.get("field")
            if generic not in {"date", "amount", "type", "category", "item", "qty", "unit", "note"}:
                _reject("invalid_mapping", "通用列不受支持")
        elif kind == "custom":
            field_id = target.get("field_id")
            field = self._require(workspace_id, field_id)
            if not field["enabled"]:
                _reject("field_disabled", "停用字段不能作为新的导入列")
        elif kind != "raw":
            _reject("invalid_mapping", "列映射只能指向通用字段、自定义字段或原始保留")
        created = now_iso()
        existing = self.store.conn.execute(
            "SELECT * FROM custom_import_mapping WHERE shop_id=? AND header_identity=?",
            (workspace_id, identity)).fetchone()
        with self.store.transaction():
            if existing:
                self.store.conn.execute(
                    """UPDATE custom_import_mapping
                       SET source_header=?, target_kind=?, generic_field=?, field_id=?,
                           enabled=1, updated_at=? WHERE shop_id=? AND id=?""",
                    (header, kind, generic, field_id, created, workspace_id, existing["id"]))
                mapping_id = existing["id"]
            else:
                mapping_id = new_id("cim")
                self.store.conn.execute(
                    """INSERT INTO custom_import_mapping(
                        id, shop_id, source_header, header_identity, target_kind,
                        generic_field, field_id, enabled, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,1,?,?)""",
                    (mapping_id, workspace_id, header, identity, kind, generic,
                     field_id, created, created))
            version = self._bump(workspace_id, created)
            self._audit(workspace_id, "mapping.update", field_id,
                        dict(existing) if existing else None,
                        {"mapping_id": mapping_id, "source_header": header, "kind": kind,
                         "generic_field": generic, "field_id": field_id},
                        actor, confirmation, version, created)
        return {"custom_fields_version": version, "mapping": {
            "mapping_id": mapping_id, "source_header": header, "kind": kind,
            "generic_field": generic, "field_id": field_id}}

    def import_mappings(self, workspace_id: str) -> dict:
        rows = self.store.conn.execute(
            """SELECT * FROM custom_import_mapping
               WHERE shop_id=? AND enabled=1 ORDER BY created_at, id""",
            (workspace_id,))
        return {"mappings": [{
            "mapping_id": row["id"], "source_header": row["source_header"],
            "kind": row["target_kind"], "generic_field": row["generic_field"],
            "field_id": row["field_id"]} for row in rows]}

    def resolve_alias(self, workspace_id: str, field_id: str, source_text: str):
        if not isinstance(source_text, str):
            return None
        row = self.store.conn.execute(
            """SELECT * FROM custom_field_alias
               WHERE shop_id=? AND field_id=? AND source_identity=? AND enabled=1""",
            (workspace_id, field_id, source_identity(source_text))).fetchone()
        if not row:
            return None
        return row["option_id"] or row["target_text"]

    def backfill_values(self, workspace_id: str, record_type: str, changes: list, *,
                        config_version: int, actor: str, confirmation: dict) -> dict:
        """只补或修订自定义属性；不改金额、正文、日期或原话。"""
        if record_type not in RECORD_TYPES:
            _reject("invalid_scope", "记录类型不受支持")
        if config_version != self.config_version(workspace_id):
            _reject("config_version_conflict", "字段配置已变更，请重新读取后提交")
        if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_REVISION_TARGETS:
            _reject("invalid_revision", "属性补填一次为 1 到 100 条")
        prepared = []
        seen = set()
        for item in changes:
            if not isinstance(item, dict) or set(item) - {"target_id", "field_id", "op", "value"}:
                _reject("invalid_revision", "属性补填格式不正确")
            target_id, field_id, operation = item.get("target_id"), item.get("field_id"), item.get("op")
            if not all(isinstance(value, str) and value for value in (target_id, field_id)):
                _reject("invalid_revision", "目标记录和字段必须明确")
            if (target_id, field_id) in seen:
                _reject("duplicate_field", "同一目标字段不能在一批里改两次")
            seen.add((target_id, field_id))
            if operation not in {"fill", "replace", "clear"}:
                _reject("invalid_revision", "属性补填只支持 fill、replace 或 clear")
            if not self._target_exists(workspace_id, record_type, target_id):
                _reject("unknown_target", "目标记录不存在或不属于本工作空间")
            field = self._require(workspace_id, field_id)
            if not field["enabled"]:
                _reject("field_disabled", "停用字段不能补填")
            attributes = self._target_attributes(workspace_id, record_type, target_id)
            if not scope_matches(field["scopes"], record_type, attributes):
                _reject("scope_mismatch", "字段不适用于这条记录")
            current = self._current_value(workspace_id, record_type, target_id, field_id)
            if operation == "fill" and current is not None:
                continue
            if operation == "clear":
                prepared.append((target_id, field, None))
                continue
            if "value" not in item:
                _reject("invalid_value", "补填值不能缺失")
            resolved = self.resolve_alias(workspace_id, field_id, item["value"])
            value = resolved if resolved is not None else item["value"]
            prepared.append((target_id, field, self._typed_value(field, {**item, "value": value})))
        created = now_iso()
        table = _VALUE_TABLES[record_type]
        with self.store.transaction():
            if config_version != self.config_version(workspace_id):
                _reject("config_version_conflict", "字段配置已变更，请重新读取后提交")
            for target_id, field, stored in prepared:
                self.store.conn.execute(
                    f"DELETE FROM {table} WHERE shop_id=? AND target_id=? AND field_id=?",
                    (workspace_id, target_id, field["field_id"]))
                self.store.conn.execute(
                    """DELETE FROM custom_field_gap
                       WHERE shop_id=? AND record_type=? AND target_id=? AND field_id=?""",
                    (workspace_id, record_type, target_id, field["field_id"]))
                if stored is None:
                    continue
                self.store.conn.execute(
                    f"""INSERT INTO {table}(
                        shop_id, target_id, field_id, value_type, text_value, number_value,
                        option_id, date_value, source_quote, source_json, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (workspace_id, target_id, stored["field_id"], stored["value_type"],
                     stored["text_value"], stored["number_value"], stored["option_id"],
                     stored["date_value"], stored["source_quote"],
                     json.dumps({"mode": "backfill", "actor": actor}, ensure_ascii=False, sort_keys=True),
                     created, created))
            self._audit(workspace_id, "value.backfill", None, None,
                        {"record_type": record_type, "count": len(prepared)},
                        actor, confirmation, config_version, created)
        return {"record_type": record_type, "changed": len(prepared),
                "custom_fields_version": config_version}

    def _target_exists(self, workspace_id, record_type, target_id):
        return self._target_row(workspace_id, record_type, target_id) is not None

    def _target_attributes(self, workspace_id, record_type, target_id):
        row = self._target_row(workspace_id, record_type, target_id)
        if not row:
            return {}
        return self._attributes(record_type, {key: row[key] for key in row.keys()})

    def _target_row(self, workspace_id, record_type, target_id):
        tables = {"entry": "entry", "sale": "sale", "observation": "observation", "thought": "thoughts"}
        return self.store.conn.execute(
            f"SELECT * FROM {tables[record_type]} WHERE shop_id=? AND id=?",
            (workspace_id, target_id)).fetchone()
