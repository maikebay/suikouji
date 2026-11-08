"""工作空间名称 → 分类规则。服务端是唯一权威，不扫描历史，也不调用模型。

规则按规范化后的账目名称精确匹配（Unicode NFC，再去掉首尾空白，区分大小写）。
已确认且启用的规则才参与入账；候选只留档，不套用，也不拦截入账。
改规则不回写已经记下的账。
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata

from .agent_normalization import CATEGORY_TYPE, EXPENSE_CATEGORIES
from .custom_fields import CustomFieldError, CustomFieldStore, display_identity, scope_matches
from .model import new_id, now_iso


BILL_FIELD_KEY = "cf_bill_category"
RULE_STATES = ("confirmed", "candidate")
# 账单选项编号和账目类型/分类的合法组合。不是按名称猜测，也不是一对一的历史统计。
LEGAL_BILL_PAIRS = {
    "opt_bill_revenue": ("revenue", "营业额"),
    "opt_bill_ingredients": ("purchase", "食材成本"),
    "opt_bill_rent": ("expense", "房租"),
    "opt_bill_wages": ("expense", "人工"),
    "opt_bill_utilities": ("expense", "水电"),
    "opt_bill_commission": ("expense", "平台佣金"),
    "opt_bill_packaging": ("expense", "包装耗材"),
    "opt_bill_promotion": ("expense", "推广"),
    "opt_bill_repair": ("expense", "维修"),
    "opt_bill_tax": ("expense", "税费"),
    "opt_bill_other": ("expense", "其他支出"),
}
PRIORITY = (
    "explicit_values_on_this_entry",
    "one_off_correction_of_this_entry",
    "enabled_confirmed_rule",
    "ask",
)
NOT_IN_MVP = ("history", "ai")
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f]")
_AUDIT_LIMIT = 100


class NameRuleError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def normalize_item(value) -> str:
    """名称规则的匹配键：NFC，去掉首尾空白，不折叠大小写。"""
    if not isinstance(value, str):
        raise NameRuleError("invalid_item", "名称必须是文本")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise NameRuleError("invalid_item", "名称不是有效 UTF-8") from exc
    if _CONTROL_RE.search(value):
        raise NameRuleError("invalid_item", "名称含有禁止字符")
    text = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(text) <= 100:
        raise NameRuleError("invalid_item", "名称长度必须为 1–100 个字符")
    return text


def legal_pairs() -> list:
    return [
        {"bill_category": option_id, "type": entry_type, "category": category}
        for option_id, (entry_type, category) in LEGAL_BILL_PAIRS.items()
    ]


def assert_ledger_pair(entry_type, category) -> None:
    expected = CATEGORY_TYPE.get(category)
    if entry_type not in {"revenue", "purchase", "expense"} or expected != entry_type:
        raise NameRuleError(
            "illegal_classification",
            "账目类型和分类不是合法组合",
            details={"type": entry_type, "category": category},
        )


def _pair_error(entry_type, category, bill_category):
    expected = LEGAL_BILL_PAIRS.get(bill_category)
    details = {
        "type": entry_type,
        "category": category,
        "bill_category": bill_category,
        "legal_pairs": legal_pairs(),
    }
    if expected:
        details["expected"] = {"type": expected[0], "category": expected[1]}
    raise NameRuleError(
        "illegal_classification",
        "账目分类和账单分类不是允许的组合",
        details=details,
    )


class NameRuleStore:
    """读规则必须发生在调用方的写入事务里。这里不保留进程内缓存。"""

    def __init__(self, store):
        self.store = store

    def rules_version(self, workspace_id: str) -> int:
        row = self.store.conn.execute(
            "SELECT version FROM name_rule_config WHERE shop_id=?",
            (workspace_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def bill_field(self, workspace_id: str):
        for field in CustomFieldStore(self.store).list_fields(workspace_id)["fields"]:
            if field["key"] == BILL_FIELD_KEY:
                return field
        return None

    def list_rules(self, workspace_id: str) -> dict:
        field = self.bill_field(workspace_id)
        installed = {}
        if field:
            installed = {option["option_id"]: option for option in field.get("options", [])}
        pairs = []
        for item in legal_pairs():
            option = installed.get(item["bill_category"])
            pairs.append({
                **item,
                "installed": option is not None,
                "enabled": bool(option and option["enabled"]),
                "display_name": option["display_name"] if option else None,
            })
        rules = [self._public(row) for row in self.store.conn.execute(
            """SELECT * FROM name_rule WHERE shop_id=?
               ORDER BY item_identity, state, id""",
            (workspace_id,))]
        audits = [self._public_audit(row) for row in self.store.conn.execute(
            """SELECT * FROM name_rule_audit WHERE shop_id=?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (workspace_id, _AUDIT_LIMIT))]
        return {
            "rules_version": self.rules_version(workspace_id),
            "custom_fields_version": CustomFieldStore(self.store).config_version(workspace_id),
            "bill_field_key": BILL_FIELD_KEY,
            "bill_field_id": field["field_id"] if field else None,
            "priority": list(PRIORITY),
            "not_in_mvp": list(NOT_IN_MVP),
            "match": {
                "item": "NFC，去掉首尾空白，区分大小写，精确相等",
                "confirmed_enabled": "套用到省略了类型、分类或账单分类的新账",
                "candidate": "只留档，不套用，不拦截入账",
                "disabled": "不套用；停用而不是删除",
                "history_rewrite": False,
            },
            "legal_pairs": pairs,
            "rules": rules,
            "audits": audits,
        }

    def apply(self, workspace_id: str, spec: dict, *, actor: str, request_id: str) -> dict:
        """创建或更新一条规则。调用方负责幂等；本方法不读请求缓存。"""
        scope = spec.get("scope")
        if scope not in RULE_STATES:
            raise NameRuleError("invalid_scope", "规则范围只能是 confirmed 或 candidate")
        if spec.get("rules_version") is not None:
            self._expect_version(workspace_id, spec.get("rules_version"))
        item = normalize_item(spec.get("item"))
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise NameRuleError("invalid_enabled", "enabled 必须是布尔值")
        raw_text = spec.get("raw_text")
        if raw_text is not None:
            raw_text = _raw_text(raw_text)
        entry_type = spec.get("type")
        category = spec.get("category")
        bill = spec.get("bill_category")
        existing_for_disable = self._row(workspace_id, item, scope)
        disabling = (scope == "confirmed" and enabled is False
                     and entry_type is None and category is None and bill is None)
        if disabling:
            if not existing_for_disable:
                raise NameRuleError("rule_not_found", "没有可停用的已确认规则",
                                    details={"item": item})
            entry_type = existing_for_disable["entry_type"]
            category = existing_for_disable["category"]
            bill = existing_for_disable["bill_category"]
        elif scope == "confirmed":
            self._require_complete(entry_type, category, bill)
            self._assert_bill(workspace_id, entry_type, category, bill)
        else:
            self._validate_partial(workspace_id, entry_type, category, bill)
        existing = self._row(workspace_id, item, scope)
        if existing and _same_rule(existing, entry_type, category, bill, enabled):
            return {
                "unchanged": True,
                "duplicate": False,
                "scope": scope,
                "rules_version": self.rules_version(workspace_id),
                "rule": self._public(existing),
                "audit": None,
            }
        created = now_iso()
        before = self._public(existing) if existing else None
        with self.store.transaction():
            if spec.get("rules_version") is not None:
                self._expect_version(workspace_id, spec.get("rules_version"))
            if existing:
                self.store.conn.execute(
                    """UPDATE name_rule
                       SET item_text=?, entry_type=?, category=?, bill_category=?,
                           enabled=?, revision=revision+1, updated_at=?
                       WHERE shop_id=? AND id=?""",
                    (item, entry_type, category, bill, int(enabled), created,
                     workspace_id, existing["id"]))
                rule_id = existing["id"]
                action = "rule.disable" if before["enabled"] and not enabled else (
                    "rule.enable" if not before["enabled"] and enabled else f"rule.{scope}")
            else:
                rule_id = new_id("nr")
                self.store.conn.execute(
                    """INSERT INTO name_rule(
                        id, shop_id, item_text, item_identity, entry_type, category,
                        bill_category, state, enabled, revision, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (rule_id, workspace_id, item, item, entry_type, category, bill,
                     scope, int(enabled), created, created))
                action = "rule.disable" if scope == "confirmed" and not enabled else f"rule.{scope}"
            version = self._bump(workspace_id, created)
            after_row = self._by_id(workspace_id, rule_id)
            after = self._public(after_row)
            audit = self._audit(
                workspace_id, rule_id, action, actor, request_id, raw_text,
                before, after, version, created)
        return {
            "unchanged": False,
            "duplicate": False,
            "scope": scope,
            "rules_version": version,
            "rule": after,
            "audit": audit,
        }

    def resolve_entries(self, workspace_id: str, entries: list, rules_version=None) -> tuple:
        """在当前事务里读取已提交的规则。客户端传来的版本不一致就拒绝，不写入。"""
        current = self.rules_version(workspace_id)
        if rules_version is not None and rules_version != current:
            raise NameRuleError(
                "rules_version_conflict",
                "名称规则已变更，请重新读取后再提交",
                status=409,
                details={"rules_version": current},
            )
        field = self.bill_field(workspace_id)
        resolved, decisions = [], []
        for index, row in enumerate(entries):
            new_row, decision = self._resolve_entry(workspace_id, row, field, current)
            decision["index"] = index
            resolved.append(new_row)
            decisions.append(decision)
        return resolved, {"rules_version": current, "entries": decisions}

    def entry_bill(self, workspace_id: str, entry_id: str):
        field = self.bill_field(workspace_id)
        if not field:
            return None
        row = self.store.conn.execute(
            """SELECT option_id FROM custom_entry_value
               WHERE shop_id=? AND target_id=? AND field_id=?""",
            (workspace_id, entry_id, field["field_id"])).fetchone()
        return row["option_id"] if row else None

    def write_entry_bill(self, workspace_id: str, entry_id: str, option_id: str, *,
                         entry_type: str, category: str, source: dict) -> None:
        """只改这一条账的账单分类，不改规则，不改其他账。"""
        self._assert_bill(workspace_id, entry_type, category, option_id)
        field = self.bill_field(workspace_id)
        created = now_iso()
        self.store.conn.execute(
            "DELETE FROM custom_entry_value WHERE shop_id=? AND target_id=? AND field_id=?",
            (workspace_id, entry_id, field["field_id"]))
        self.store.conn.execute(
            """INSERT INTO custom_entry_value(
                shop_id, target_id, field_id, value_type, text_value, number_value,
                option_id, date_value, source_quote, source_json, created_at, updated_at)
               VALUES(?,?,?,'enum',NULL,NULL,?,NULL,NULL,?,?,?)""",
            (workspace_id, entry_id, field["field_id"], option_id,
             json.dumps(source, ensure_ascii=False, sort_keys=True), created, created))

    def _resolve_entry(self, workspace_id, row, field, rules_version):
        row = copy.deepcopy(row)
        if "item" in row:
            row["item"] = normalize_item(row["item"])
        # These flags describe the request, and stay unchanged while fields are filled.
        omitted_type = "type" not in row
        omitted_category = "category" not in row
        explicit_bill, bill_explicit = self._explicit_bill(row, field)
        identity = row.get("item")
        rule = self._confirmed(workspace_id, identity) if identity else None

        entry_type = row.get("type")
        category = row.get("category")
        if omitted_type and not omitted_category:
            entry_type = CATEGORY_TYPE.get(category)
            if entry_type is None:
                raise NameRuleError(
                    "illegal_classification", "账目分类不在允许范围内",
                    details={"category": category})
        if not omitted_type and not omitted_category:
            assert_ledger_pair(entry_type, category)
        elif omitted_type and omitted_category and bill_explicit:
            pair = LEGAL_BILL_PAIRS.get(explicit_bill)
            if not pair:
                self._reject_bill_token(field, explicit_bill)
            entry_type, category = pair
        elif omitted_type and omitted_category and rule:
            entry_type = rule["entry_type"]
            category = rule["category"]
        elif omitted_category and not omitted_type and rule and rule["entry_type"] == entry_type:
            category = rule["category"]

        bill = explicit_bill if bill_explicit else None
        rule_matches_ledger = bool(
            rule and entry_type == rule["entry_type"] and category == rule["category"])
        if not bill_explicit and rule_matches_ledger:
            bill = rule["bill_category"]

        if entry_type is None or category is None:
            self._ask(identity, rules_version, self._candidates(workspace_id, identity) if identity else [])
        assert_ledger_pair(entry_type, category)
        if bill is not None:
            self._assert_bill(workspace_id, entry_type, category, bill, field=field)
            self._attach_bill(row, field, bill)
        else:
            row.pop("bill_category", None)
        row["type"] = entry_type
        row["category"] = category
        final = {"type": entry_type, "category": category, "bill_category": bill}
        rule_final = None
        if rule:
            rule_final = {
                "type": rule["entry_type"],
                "category": rule["category"],
                "bill_category": rule["bill_category"],
            }
        used_rule = bool(rule_final) and final == rule_final and (
            omitted_type or omitted_category or not bill_explicit)
        source = "confirmed_rule" if used_rule else "explicit"
        return row, {
            "item": identity,
            "source": source,
            "rule_id": rule["id"] if rule else None,
            "rules_version": rules_version,
            "type": entry_type,
            "category": category,
            "bill_category": bill,
            "overrode_rule": bool(rule) and source == "explicit" and final != rule_final,
        }

    def _ask(self, identity, rules_version, candidates):
        label = identity or "（未写名称）"
        raise NameRuleError(
            "classification_required",
            f"名称「{label}」没有可套用的已确认规则，请向用户确认账目类型、分类和账单分类",
            details={
                "item": identity,
                "rules_version": rules_version,
                "candidates": candidates,
                "ask": "请确认这笔账的类型、账目分类和账单分类。不要猜测，也不要根据历史笔数推断。",
                "read": "GET /v1/name-rules",
            },
        )

    def _explicit_bill(self, row, field):
        top = row.get("bill_category") if "bill_category" in row else None
        found = None
        present = False
        if field:
            for item in row.get("custom_fields") or []:
                if item.get("field_id") != field["field_id"]:
                    continue
                present = True
                value = item.get("value")
                if isinstance(value, dict):
                    if "display_name" in value and "option_id" not in value:
                        raise NameRuleError(
                            "invalid_option",
                            "账单分类必须使用选项编号，不能使用显示名",
                            details={"display_name": value.get("display_name")},
                        )
                    value = value.get("option_id")
                found = value
        if top is not None and found is not None and top != found:
            raise NameRuleError(
                "classification_conflict",
                "同一条账的账单分类出现了两个不同的值",
                details={"bill_category": top, "custom_field": found},
            )
        if "bill_category" in row:
            return top, True
        if present:
            return found, True
        return None, False

    def _attach_bill(self, row, field, option_id):
        if not field:
            raise NameRuleError(
                "bill_field_missing",
                "工作空间还没有账单分类字段 cf_bill_category",
                details={"bill_field_key": BILL_FIELD_KEY},
            )
        fields = [item for item in (row.get("custom_fields") or [])
                  if item.get("field_id") != field["field_id"]]
        fields.append({"field_id": field["field_id"], "value": option_id})
        row["custom_fields"] = fields
        row["bill_category"] = option_id

    def _assert_bill(self, workspace_id, entry_type, category, bill, field=None):
        assert_ledger_pair(entry_type, category)
        field = self.bill_field(workspace_id) if field is None else field
        option = self._require_option(field, bill)
        expected = LEGAL_BILL_PAIRS.get(option["option_id"])
        if expected != (entry_type, category):
            _pair_error(entry_type, category, option["option_id"])
        if not scope_matches(field["scopes"], "entry",
                             {"entry_types": entry_type, "categories": category}):
            raise NameRuleError("scope_mismatch", "账单分类字段不适用于这个账目类型或分类")

    def _require_option(self, field, bill):
        if not isinstance(bill, str) or not bill:
            raise NameRuleError("invalid_option", "账单分类必须是选项编号")
        if field is None:
            raise NameRuleError(
                "bill_field_missing",
                "工作空间还没有账单分类字段 cf_bill_category",
                details={"bill_field_key": BILL_FIELD_KEY, "bill_category": bill},
            )
        if not field["enabled"]:
            raise NameRuleError("field_disabled", "账单分类字段已停用")
        options = field.get("options") or []
        match = next((item for item in options if item["option_id"] == bill), None)
        if match:
            if not match["enabled"]:
                raise NameRuleError("invalid_option", "账单分类选项已停用",
                                    details={"option_id": bill})
            return match
        self._reject_bill_token(field, bill)

    def _reject_bill_token(self, field, bill):
        options = (field or {}).get("options") or []
        by_name = next((item for item in options
                        if item["display_name"] == bill
                        or display_identity(item["display_name"]) == _safe_identity(bill)), None)
        if by_name:
            raise NameRuleError(
                "invalid_option",
                f"账单分类必须使用选项编号 {by_name['option_id']}，不能使用显示名",
                details={"option_id": by_name["option_id"], "display_name": by_name["display_name"]},
            )
        raise NameRuleError(
            "invalid_option",
            "账单分类选项不存在或已停用",
            details={"bill_category": bill, "legal_pairs": legal_pairs()},
        )

    def _require_complete(self, entry_type, category, bill):
        if not entry_type or not category or not bill:
            raise NameRuleError(
                "classification_required",
                "确认长期规则时必须同时给出类型、账目分类和账单分类",
            )

    def _validate_partial(self, workspace_id, entry_type, category, bill):
        if entry_type is None and category is None and bill is None:
            return
        if category is not None and entry_type is None:
            entry_type = CATEGORY_TYPE.get(category)
            if entry_type is None:
                raise NameRuleError("illegal_classification", "账目分类不在允许范围内")
        if entry_type is not None and category is not None:
            assert_ledger_pair(entry_type, category)
        elif entry_type is not None or category is not None:
            if entry_type is not None and entry_type not in {"revenue", "purchase", "expense"}:
                raise NameRuleError("illegal_classification", "账目类型不在允许范围内")
            if category is not None and category not in CATEGORY_TYPE:
                raise NameRuleError("illegal_classification", "账目分类不在允许范围内")
        if bill is not None:
            if entry_type is None or category is None:
                pair = LEGAL_BILL_PAIRS.get(bill) if isinstance(bill, str) else None
                if pair and entry_type is None and category is None:
                    entry_type, category = pair
                elif entry_type is None or category is None:
                    self._require_option(self.bill_field(workspace_id), bill)
                    return
            self._assert_bill(workspace_id, entry_type, category, bill)

    def _expect_version(self, workspace_id, rules_version):
        if type(rules_version) is not int or rules_version < 0:
            raise NameRuleError("invalid_rules_version", "名称规则版本必须是非负整数")
        current = self.rules_version(workspace_id)
        if rules_version != current:
            raise NameRuleError(
                "rules_version_conflict",
                "名称规则已变更，请重新读取后再提交",
                status=409,
                details={"rules_version": current},
            )

    def _confirmed(self, workspace_id, item):
        return self.store.conn.execute(
            """SELECT * FROM name_rule
               WHERE shop_id=? AND item_identity=? AND state='confirmed' AND enabled=1""",
            (workspace_id, item)).fetchone()

    def _candidates(self, workspace_id, item):
        return [self._public(row) for row in self.store.conn.execute(
            """SELECT * FROM name_rule
               WHERE shop_id=? AND item_identity=? AND state='candidate' AND enabled=1
               ORDER BY updated_at, id""",
            (workspace_id, item))]

    def _row(self, workspace_id, item, state):
        return self.store.conn.execute(
            "SELECT * FROM name_rule WHERE shop_id=? AND item_identity=? AND state=?",
            (workspace_id, item, state)).fetchone()

    def _by_id(self, workspace_id, rule_id):
        return self.store.conn.execute(
            "SELECT * FROM name_rule WHERE shop_id=? AND id=?",
            (workspace_id, rule_id)).fetchone()

    def _bump(self, workspace_id, updated) -> int:
        self.store.conn.execute(
            """INSERT INTO name_rule_config(shop_id, version, updated_at)
               VALUES(?, 1, ?)
               ON CONFLICT(shop_id) DO UPDATE SET
                 version=version+1, updated_at=excluded.updated_at""",
            (workspace_id, updated))
        return self.rules_version(workspace_id)

    def _audit(self, workspace_id, rule_id, action, actor, request_id, raw_text,
               before, after, version, created):
        audit_id = new_id("nra")
        self.store.conn.execute(
            """INSERT INTO name_rule_audit(
                id, shop_id, rule_id, action, actor, request_id, raw_text,
                before_json, after_json, rules_version, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (audit_id, workspace_id, rule_id, action, actor, request_id, raw_text,
             json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
             json.dumps(after, ensure_ascii=False, sort_keys=True),
             version, created))
        return {
            "audit_id": audit_id, "rule_id": rule_id, "action": action, "actor": actor,
            "request_id": request_id, "raw_text": raw_text, "before": before, "after": after,
            "rules_version": version, "created_at": created,
        }

    def _public(self, row) -> dict:
        return {
            "rule_id": row["id"],
            "workspace_id": row["shop_id"],
            "item": row["item_text"],
            "type": row["entry_type"],
            "category": row["category"],
            "bill_category": row["bill_category"],
            "state": row["state"],
            "enabled": bool(row["enabled"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _public_audit(self, row) -> dict:
        return {
            "audit_id": row["id"],
            "rule_id": row["rule_id"],
            "action": row["action"],
            "actor": row["actor"],
            "request_id": row["request_id"],
            "raw_text": row["raw_text"],
            "before": _load(row["before_json"]),
            "after": _load(row["after_json"]),
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }


def _load(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _raw_text(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise NameRuleError("invalid_text", "规则来源原文必须是 1 到 2000 个字符")
    if _CONTROL_RE.search(value):
        raise NameRuleError("invalid_text", "规则来源原文含有禁止字符")
    return value.strip()


def _same_rule(row, entry_type, category, bill, enabled) -> bool:
    return (row["entry_type"] == entry_type and row["category"] == category
            and row["bill_category"] == bill and bool(row["enabled"]) is enabled)


def _safe_identity(value):
    if not isinstance(value, str):
        return None
    try:
        return display_identity(value)
    except CustomFieldError:
        return None


# Expense categories are part of the legal ledger set; referenced so a future
# edit that drops one from agent_normalization fails review of this module.
assert set(LEGAL_BILL_PAIRS["opt_bill_wages"]) <= {"expense", "人工"}
assert "人工" in EXPENSE_CATEGORIES
