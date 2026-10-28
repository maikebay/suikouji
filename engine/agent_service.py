"""受控的 Agent 业务门面。

REST、iLink 和未来 MCP 都通过这里访问业务，不直接操作 SQLite。
Agent 先整理口语记录，门面校验结构化事实、工作空间授权和结果；旧原话
模式、修正与导入仍复用现有 engine 模块。所有写入共用 Store 的账务投影。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import uuid
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .agent_normalization import persist_structured, validate_structured
from .custom_fields import CustomFieldError, CustomFieldStore
from .capabilities import product_capabilities
from .importer import commit_import, plan_import
from .model import month_range
from .observe import ingest
from .review import apply_review, review_items


MAX_TEXT = 2000
MAX_REQUEST_ID = 100
MAX_PAGE_SIZE = 100
PREVIEW_TTL_SECONDS = 1800


class AgentError(ValueError):
    """可安全返回给外部 Agent 的业务错误。"""

    def __init__(self, code: str, message: str, *, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


@dataclass(frozen=True)
class Principal:
    """已由传输层认证的调用主体。默认是外部 Agent，不能批准配置。"""

    id: str
    owner_id: str
    role: str = "agent"


class AgentService:
    """统一的、按 principal 限定工作空间的业务入口。"""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def capabilities() -> dict:
        contract = product_capabilities()
        contract["contract_version"] = "agent-1"
        contract["operations"] = {
            "capture_text": {"method": "POST", "path": "/v1/captures", "confirmation": "ambiguous"},
            "get_capture": {"method": "GET", "path": "/v1/captures/{capture_id}"},
            "list_timeline": {"method": "GET", "path": "/v1/timeline"},
            "get_summary": {"method": "GET", "path": "/v1/summary"},
            "list_entries": {"method": "GET", "path": "/v1/entries"},
            "list_sales": {"method": "GET", "path": "/v1/sales"},
            "list_observations": {"method": "GET", "path": "/v1/observations"},
            "list_thoughts": {"method": "GET", "path": "/v1/thoughts"},
            "list_reviews": {"method": "GET", "path": "/v1/reviews"},
            "apply_review": {"method": "POST", "path": "/v1/reviews", "confirmation": "required"},
            "preview_import": {"method": "POST", "path": "/v1/imports/preview", "confirmation": "required"},
            "preview_xlsx": {"method": "POST", "path": "/v1/imports/xlsx-preview", "confirmation": "required"},
            "commit_xlsx": {"method": "POST", "path": "/v1/imports/xlsx-commit", "confirmation": "required"},
            "commit_import": {"method": "POST", "path": "/v1/imports/commit", "confirmation": "required"},
            "list_custom_fields": {"method": "GET", "path": "/v1/custom-fields"},
            "maintain_custom_fields": {
                "method": "POST", "path": "/v1/custom-field-maintenance",
                "confirmation": "none", "actor": "authorized_agent",
            },
            "query_custom_field_records": {"method": "POST", "path": "/v1/custom-field-records/query"},
            "group_custom_field_records": {"method": "POST", "path": "/v1/custom-field-records/group"},
            "export_custom_field_records": {"method": "POST", "path": "/v1/custom-field-records/export"},
            "backfill_custom_field_values": {
                "method": "POST", "path": "/v1/custom-field-values/backfill",
                "confirmation": "none", "actor": "authorized_agent",
            },
        }
        return contract

    @staticmethod
    def _request_id(value, label="request_id") -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_REQUEST_ID:
            raise AgentError("invalid_request_id", f"{label} 必须是 1 到 {MAX_REQUEST_ID} 个字符")
        return value

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
            raise AgentError("invalid_text", f"原话必须是 1 到 {MAX_TEXT} 个字符")
        return value

    @staticmethod
    def _check_unicode(value, code):
        # JSON escapes can represent unpaired surrogates; neither UTF-8 nor
        # SQLite can store them. Inspect only, preserving original hash bytes.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise AgentError(code, "文本含有无效的 Unicode 字符") from exc
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)

    @staticmethod
    def _cursor_key(value, *, date_only=False):
        if (not isinstance(value, (list, tuple)) or len(value) != 3
                or any(not isinstance(part, str) or not part or "|" in part for part in value)):
            raise ValueError("invalid cursor tuple")
        for part in value:
            part.encode("utf-8")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value[0][:10]):
            raise ValueError("invalid cursor date")
        dt.date.fromisoformat(value[0][:10])
        if date_only and len(value[0]) != 10:
            raise ValueError("invalid cursor date")
        if len(value[0]) != 10:
            dt.datetime.fromisoformat(value[0].replace("Z", "+00:00"))
        dt.datetime.fromisoformat(value[1].replace("Z", "+00:00"))
        return tuple(value)

    @staticmethod
    def _fingerprint(payload):
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                        sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _business_identity(principal, request_id):
        encoded = json.dumps([principal.id, request_id], ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _idempotent(self, principal: Principal, workspace_id: str, operation: str,
                    request_id: str, payload: dict):
        fingerprint = self._fingerprint(payload)
        existing = self.store.agent_request_get(principal.id, workspace_id, operation, request_id)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise AgentError("idempotency_conflict", "同一 request_id 不能提交不同内容", status=409)
            if existing.get("response") is None:
                # A process may have exited after the claim or business commit.
                # The store lease permits recovery only for the same payload.
                lease_token = self.store.agent_request_claim(
                    principal.id, workspace_id, operation, request_id, fingerprint
                )
                if lease_token:
                    return None, (principal.id, workspace_id, operation, request_id, lease_token), True
                raise AgentError("request_in_progress", "同一 request_id 正在处理中", status=409)
            return existing["response"], None, False
        lease_token = self.store.agent_request_claim(
            principal.id, workspace_id, operation, request_id, fingerprint
        )
        if not lease_token:
            existing = self.store.agent_request_get(principal.id, workspace_id, operation, request_id)
            if existing and existing["fingerprint"] != fingerprint:
                raise AgentError("idempotency_conflict", "同一 request_id 不能提交不同内容", status=409)
            if existing and existing.get("response") is not None:
                return existing["response"], None, False
            raise AgentError("request_in_progress", "同一 request_id 正在处理中", status=409)
        return None, (principal.id, workspace_id, operation, request_id, lease_token), False

    def _finish(self, claim, response):
        if claim and not self.store.agent_request_finish(*claim, response):
            # A fenced claim may have been replaced by another worker. Do not
            # commit business rows without a durable response for this request.
            raise RuntimeError("idempotency claim lost before response commit")

    def _fields(self) -> CustomFieldStore:
        return CustomFieldStore(self.store)

    def _manager(self, principal: Principal):
        if principal.role != "manager":
            raise AgentError("confirmation_forbidden", "外部 Agent 不能批准字段配置", status=403)

    def _field_error(self, exc: CustomFieldError):
        status = 409 if exc.code in {"config_version_conflict", "preview_stale", "preview_consumed",
                                      "cursor_stale"} else 400
        raise AgentError(exc.code, str(exc), status=status) from exc

    def query_custom_field_records(self, principal: Principal, workspace_id: str, query: dict) -> dict:
        self.workspace(principal, workspace_id)
        try:
            return self._fields().query_records(
                workspace_id, query.get("record_type"), start=query.get("start", "0000"),
                end=query.get("end", "9999"), filters=query.get("filters"),
                page_size=query.get("page_size", 30), before=query.get("before"))
        except CustomFieldError as exc:
            self._field_error(exc)

    def group_custom_field_records(self, principal: Principal, workspace_id: str, query: dict) -> dict:
        self.workspace(principal, workspace_id)
        try:
            return self._fields().group_records(
                workspace_id, query.get("record_type"), query.get("field_id"),
                start=query.get("start", "0000"), end=query.get("end", "9999"),
                metric=query.get("metric", "count"))
        except CustomFieldError as exc:
            self._field_error(exc)

    def export_custom_field_records(self, principal: Principal, workspace_id: str, query: dict) -> dict:
        self.workspace(principal, workspace_id)
        try:
            return self._fields().export_records(
                workspace_id, query.get("record_type"), start=query.get("start", "0000"),
                end=query.get("end", "9999"))
        except CustomFieldError as exc:
            self._field_error(exc)

    def list_custom_fields(self, principal: Principal, workspace_id: str) -> dict:
        self.workspace(principal, workspace_id)
        return self._fields().list_fields(workspace_id)

    def maintain_custom_fields(self, principal: Principal, workspace_id: str,
                               request_id: str, action: str, change: dict) -> dict:
        """已授权 Agent 直接维护字段定义；不要求用户预览或再次确认。"""
        self.workspace(principal, workspace_id)
        request_id = self._request_id(request_id)
        if action not in {"create", "update", "add_option", "set_option_enabled",
                          "alias", "import_mapping"}:
            raise AgentError("invalid_change", "字段维护操作不受支持")
        if not isinstance(change, dict):
            raise AgentError("invalid_change", "字段维护内容必须是对象")
        payload = {"action": action, "change": change}
        confirmation = {"channel": "agent", "actor": principal.id, "request_id": request_id}
        with self.store.transaction():
            cached, claim, _recovered = self._idempotent(
                principal, workspace_id, "custom_field_maintenance", request_id, payload)
            if cached is not None:
                return {**cached, "duplicate": True}
            try:
                result = self._apply_field_maintenance(
                    workspace_id, action, change, actor=principal.id, confirmation=confirmation)
            except CustomFieldError as exc:
                self._field_error(exc)
            self._finish(claim, result)
        return result

    def _apply_field_maintenance(self, workspace_id, action, change, *, actor, confirmation):
        fields = self._fields()
        if action == "create":
            return fields.create_field(workspace_id, change.get("spec"), actor=actor,
                                       confirmation=confirmation)
        field_id = change.get("field_id")
        if action == "update":
            return fields.update_field(workspace_id, field_id, change.get("changes"),
                                       actor=actor, confirmation=confirmation)
        if action == "add_option":
            return fields.add_option(workspace_id, field_id, change.get("display_name"),
                                     actor=actor, confirmation=confirmation)
        if action == "alias":
            return fields.maintain_alias(
                workspace_id, field_id, change.get("source_text"), change.get("target"),
                actor=actor, confirmation=confirmation,
                enabled=change.get("enabled", True))
        if action == "import_mapping":
            return fields.maintain_import_mapping(
                workspace_id, change.get("source_header"), change.get("target"),
                actor=actor, confirmation=confirmation)
        return fields.set_option_enabled(
            workspace_id, field_id, change.get("option_id"), change.get("enabled"),
            actor=actor, confirmation=confirmation)

    def backfill_custom_field_values(self, principal: Principal, workspace_id: str,
                                     request_id: str, revision: dict) -> dict:
        """已授权 Agent 直接补填历史属性；不改财务金额。"""
        self.workspace(principal, workspace_id)
        request_id = self._request_id(request_id)
        if not isinstance(revision, dict):
            raise AgentError("invalid_revision", "属性补填内容必须是对象")
        payload = {"revision": revision}
        confirmation = {"channel": "agent", "actor": principal.id, "request_id": request_id}
        with self.store.transaction():
            cached, claim, _recovered = self._idempotent(
                principal, workspace_id, "custom_field_backfill", request_id, payload)
            if cached is not None:
                return {**cached, "duplicate": True}
            try:
                result = self._fields().backfill_values(
                    workspace_id, revision.get("record_type"), revision.get("changes"),
                    config_version=revision.get("custom_fields_version"),
                    actor=principal.id, confirmation=confirmation)
            except CustomFieldError as exc:
                self._field_error(exc)
            self._finish(claim, result)
        return result

    def submit_custom_field_suggestion(self, principal: Principal, workspace_id: str,
                                       request_id: str, suggestion: dict) -> dict:
        self.workspace(principal, workspace_id)
        request_id = self._request_id(request_id)
        payload = {"suggestion": suggestion}
        with self.store.transaction():
            cached, claim, _recovered = self._idempotent(
                principal, workspace_id, "custom_field_suggestion", request_id, payload)
            if cached is not None:
                return {**cached, "duplicate": True}
            try:
                result = {"suggestion": self._fields().submit_suggestion(
                    workspace_id, suggestion, actor=principal.id)}
            except CustomFieldError as exc:
                self._field_error(exc)
            self._finish(claim, result)
        return result

    def preview_custom_field_change(self, principal: Principal, workspace_id: str,
                                    change: dict) -> dict:
        self.workspace(principal, workspace_id)
        self._manager(principal)
        try:
            return self._fields().preview_change(workspace_id, principal.id, change)
        except CustomFieldError as exc:
            self._field_error(exc)

    def commit_custom_field_change(self, principal: Principal, workspace_id: str,
                                   request_id: str, preview_token: str) -> dict:
        self.workspace(principal, workspace_id)
        self._manager(principal)
        request_id = self._request_id(request_id)
        payload = {"preview_token": preview_token}
        with self.store.transaction():
            cached, claim, _recovered = self._idempotent(
                principal, workspace_id, "custom_field_change", request_id, payload)
            if cached is not None:
                return {**cached, "duplicate": True}
            try:
                result = self._fields().commit_change(
                    workspace_id, principal.id, preview_token, actor=principal.id,
                    confirmation={"request_id": request_id, "principal_id": principal.id,
                                  "role": principal.role})
            except CustomFieldError as exc:
                self._field_error(exc)
            self._finish(claim, result)
        return result

    def workspace(self, principal: Principal, workspace_id: str) -> dict:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise AgentError("workspace_required", "必须指定工作空间")
        workspace = self.store.workspace_get(workspace_id, principal.owner_id)
        if not workspace:
            raise AgentError("workspace_forbidden", "工作空间不存在或不属于当前调用主体", status=403)
        return workspace

    def list_workspaces(self, principal: Principal) -> list:
        return self.store.workspace_list(principal.owner_id)

    def capture_text(self, principal: Principal, workspace_id: str, text: str,
                     request_id: str, *, source: str = "agent", received_at=None,
                     context=None, intent=None, profile_id=None, dedup_ref=None,
                     peer=None, structured=None, custom_fields_version=None) -> dict:
        workspace = self.workspace(principal, workspace_id)
        request_id = self._request_id(request_id)
        text = self._text(text)
        self._check_unicode(request_id, "invalid_request_id")
        self._check_unicode(text, "invalid_text")
        if structured is not None:
            self._check_unicode(structured, "invalid_structured")
        self._check_unicode([context, intent, profile_id, dedup_ref, peer], "invalid_text")
        if source not in ("agent", "chat", "ilink", "web", "cli"):
            raise AgentError("invalid_source", "来源不在允许范围内")
        if received_at is not None:
            if (not isinstance(received_at, str) or not 10 <= len(received_at) <= 80
                    or received_at[4] != "-" or received_at[7] != "-"):
                raise AgentError("invalid_received_at", "received_at 格式不正确")
            try:
                dt.date.fromisoformat(received_at[:10])
                dt.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AgentError("invalid_received_at", "received_at 格式不正确") from exc
        for label, value in (("context", context), ("intent", intent),
                             ("dedup_ref", dedup_ref), ("peer", peer)):
            if value is not None and not isinstance(value, str):
                raise AgentError(f"invalid_{label}", f"{label} 格式不正确")
        explicit_dedup = bool(dedup_ref)
        legacy_dedup = f"agent:{principal.id}:{request_id}"
        dedup_ref = dedup_ref or "agent-v2:" + self._business_identity(principal, request_id)
        payload = {"text": text, "source": source, "received_at": received_at,
                   "context": context, "intent": intent, "profile_id": profile_id,
                   "peer": peer, "dedup_ref": dedup_ref}
        if structured is not None:
            # Preserve the legacy fingerprint byte-for-byte when no structured
            # data was supplied. Modes and normalized facts share one request ID.
            payload["structured"] = structured
        if custom_fields_version is not None:
            if type(custom_fields_version) is not int or custom_fields_version < 0:
                raise AgentError("invalid_custom_fields_version", "字段配置版本必须是非负整数")
            payload["custom_fields_version"] = custom_fields_version
        # Claim, business projections, and the full response commit together.
        # Store's nested savepoints never publish an intermediate result.
        with self.store.transaction():
            # Preserve pre-v2 completed responses and claim-only recovery. Never
            # infer ownership of an ambiguous legacy key from the record alone.
            old_request = self.store.agent_request_get(principal.id, workspace_id, "capture", request_id)
            legacy_payload = {**payload, "dedup_ref": legacy_dedup}
            if (not explicit_dedup and old_request
                    and old_request["fingerprint"] == self._fingerprint(legacy_payload)):
                dedup_ref, payload = legacy_dedup, legacy_payload
            cached, claim, recovered = self._idempotent(
                principal, workspace_id, "capture", request_id, payload,
            )
            if cached is not None:
                return {**cached, "duplicate": True}
            if profile_id is not None and profile_id != workspace["profile_id"]:
                raise AgentError("profile_mismatch", "profile_id 与工作空间不一致")
            normalized = None
            if structured is not None:
                if workspace["profile_id"] != "restaurant":
                    raise AgentError("profile_unsupported", "当前结构化记账只支持 restaurant 工作空间")
                try:
                    normalized = validate_structured(text, structured)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise AgentError("invalid_structured", str(exc)) from exc
            existing_record = self.store.record_find(workspace_id, dedup_ref)
            if existing_record and (
                existing_record.get("raw_text") != text
                or existing_record.get("source") != source
                or existing_record.get("peer") != peer
            ):
                raise AgentError("idempotency_conflict", "同一 dedup_ref 不能提交不同原话", status=409)
            legacy_record = (self.store.record_find(workspace_id, legacy_dedup)
                             if not explicit_dedup and dedup_ref != legacy_dedup else None)
            if (legacy_record or (existing_record
                    and existing_record["parse_status"] not in {"pending", "parsing"})):
                raise AgentError("recovery_needs_review", "旧请求已有业务结果但缺少完整响应，请人工核对", status=409)
            if normalized is not None:
                if existing_record:
                    # A structured request is always atomic. A leftover pending
                    # raw record cannot establish which normalized facts were
                    # accepted, even if its ledger_refs happen to be empty.
                    code = ("request_in_progress" if existing_record["parse_status"] == "parsing"
                            else "recovery_needs_review")
                    raise AgentError(code, "已有原话但缺少结构化完整响应，请核对后再处理", status=409)
                receipt = persist_structured(
                    self.store, workspace_id, text, normalized, source=source, peer=peer,
                    dedup_ref=dedup_ref, received_at=received_at, context=context,
                    intent=intent, profile_id=workspace["profile_id"],
                    custom_fields_version=custom_fields_version,
                )
            else:
                try:
                    receipt = ingest(
                        self.store, workspace_id, text, source=source, peer=peer,
                        dedup_ref=dedup_ref, received_at=received_at, context=context,
                        intent=intent, profile_id=profile_id,
                    )
                except (ValueError, TypeError, OverflowError) as exc:
                    # Only a rejected input is a terminal business result. Response
                    # persistence failures must escape and roll back the whole request.
                    record = self.store.record_find(workspace_id, dedup_ref)
                    if not record or record["parse_status"] != "rejected":
                        raise
                    receipt = {"record": record, "duplicate": False, "status": "rejected",
                               "parsed": None, "warnings": [str(exc)]}
            if receipt.get("status") in {"pending", "parsing"}:
                raise AgentError("request_in_progress", "原话仍在处理中，请稍后重试", status=409)
            detail = self.store.record_detail(workspace_id, receipt["record"]["id"])
            result = {
                "duplicate": bool(receipt.get("duplicate")),
                "status": receipt.get("status"),
                "capture": detail["record"] if detail else receipt["record"],
                "parsed": receipt.get("parsed"),
                "entries": detail["entries"] if detail else [],
                "sales": detail["sales"] if detail else [],
                "observations": detail["observations"] if detail else [],
                "thoughts": detail["thoughts"] if detail else [],
                "warnings": receipt.get("warnings", detail["warnings"] if detail else []),
                "provenance": {
                    "source": source,
                    "principal_id": principal.id,
                    "request_id": request_id,
                    "dedup_ref": dedup_ref,
                    "workspace_id": workspace_id,
                },
            }
            if normalized is not None:
                result["normalization"] = receipt["normalization"]
                if receipt.get("custom_fields", {}).get("written") or receipt.get("custom_fields", {}).get("missing_required"):
                    result["custom_fields"] = receipt["custom_fields"]
            self._finish(claim, result)
            return result

    def capture_detail(self, principal: Principal, workspace_id: str, capture_id: str) -> dict:
        self.workspace(principal, workspace_id)
        detail = self.store.record_detail(workspace_id, capture_id)
        if not detail:
            raise AgentError("capture_not_found", "原话不存在", status=404)
        return detail

    @staticmethod
    def _bounds(start, end):
        start = start or "0000"
        end = end or "9999"
        if not isinstance(start, str) or not isinstance(end, str) or start > end:
            raise AgentError("invalid_range", "日期范围不正确")
        return start, end

    def timeline(self, principal: Principal, workspace_id: str, **filters) -> dict:
        self.workspace(principal, workspace_id)
        if "types" in filters and filters["types"] != "thought":
            raise AgentError("invalid_types", "types 只支持 thought")
        page_size = filters.pop("page_size", 30)
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            raise AgentError("invalid_page_size", "page_size 必须是数字")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise AgentError("invalid_page_size", "page_size 必须在 1 到 100 之间")
        filters["page_size"] = page_size
        if filters.get("before") is not None:
            try:
                filters["before"] = self._cursor_key(filters["before"])
            except (ValueError, TypeError, UnicodeError) as exc:
                raise AgentError("invalid_cursor", "before 游标格式不正确") from exc
        return self.store.record_timeline(workspace_id, **filters)

    def thoughts(self, principal: Principal, workspace_id: str, *, start=None, end=None,
                 tags=None, q="", page_size=30, before=None) -> dict:
        self.workspace(principal, workspace_id)
        from .thoughts import normalize_tags
        for value in (start, end):
            if value is not None:
                try:
                    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                        raise ValueError()
                    dt.date.fromisoformat(value)
                except ValueError as exc:
                    raise AgentError("invalid_range", "日期必须是有效的 YYYY-MM-DD") from exc
        if start is not None and end is not None and start > end:
            raise AgentError("invalid_range", "开始日期不能晚于结束日期")
        if not isinstance(q, str) or len(q) > 200:
            raise AgentError("invalid_query", "关键词最多 200 个字符")
        self._check_unicode(q, "invalid_query")
        if type(page_size) is not int and not (isinstance(page_size, str) and re.fullmatch(r"[0-9]+", page_size)):
            raise AgentError("invalid_page_size", "page_size 必须是 1 到 100 的整数")
        if len(str(page_size)) > 3 or not 1 <= int(page_size) <= MAX_PAGE_SIZE:
            raise AgentError("invalid_page_size", "page_size 必须是 1 到 100 的整数")
        try:
            tags = normalize_tags([] if tags is None else tags)
        except ValueError as exc:
            raise AgentError("invalid_tags", str(exc)) from exc
        # Bind opaque continuation tokens to the authorized workspace and semantic
        # filters. Pagination size may change without changing the result set.
        binding = self._fingerprint({"workspace": workspace_id, "start": start, "end": end,
                                     "tags": sorted(tags), "q": q.casefold()})
        key = None
        if before is not None:
            try:
                if (not isinstance(before, str) or not 1 <= len(before) <= 2048
                        or not re.fullmatch(r"[A-Za-z0-9_-]+", before)):
                    raise ValueError()
                cursor = json.loads(base64.b64decode(before + "=" * (-len(before) % 4),
                                                    altchars=b"-_", validate=True))
                if (not isinstance(cursor, dict) or set(cursor) != {"v", "filter", "key"}
                        or type(cursor["v"]) is not int or cursor["v"] != 1
                        or cursor["filter"] != binding or not isinstance(cursor["key"], list)
                        or len(cursor["key"]) != 3
                        or any(not isinstance(part, str) or not part for part in cursor["key"])):
                    raise ValueError()
                key = self._cursor_key(cursor["key"], date_only=True)
            except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
                raise AgentError("invalid_cursor", "随想游标无效或与工作空间、筛选条件不匹配") from exc
        result = self.store.thought_list(workspace_id, start=start, end=end, tags=tags,
                                         q=q, page_size=int(page_size), before=key)
        next_key = result["next_before"]
        token = None
        if next_key is not None:
            encoded = json.dumps({"v": 1, "filter": binding, "key": list(next_key)},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            token = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
        return {**result, "next_before": token}

    def summary(self, principal: Principal, workspace_id: str, month=None) -> dict:
        self.workspace(principal, workspace_id)
        if month:
            try:
                start, end = month_range(month)
            except (TypeError, ValueError):
                raise AgentError("invalid_month", "月份格式为 YYYY-MM")
        else:
            start, end = "0000", "9999"
        return self.store.summary(workspace_id, start, end)

    def entries(self, principal: Principal, workspace_id: str, start=None, end=None, type_=None) -> list:
        self.workspace(principal, workspace_id)
        start, end = self._bounds(start, end)
        return self.store.list_entries(workspace_id, start, end, type_)

    def sales(self, principal: Principal, workspace_id: str, start=None, end=None) -> list:
        self.workspace(principal, workspace_id)
        start, end = self._bounds(start, end)
        return self.store.list_sales(workspace_id, start, end)

    def observations(self, principal: Principal, workspace_id: str, start=None, end=None,
                     kind=None, subject=None, measure=None) -> list:
        self.workspace(principal, workspace_id)
        start, end = self._bounds(start, end)
        return self.store.observation_list(workspace_id, start, end, kind, subject, measure)

    def reviews(self, principal: Principal, workspace_id: str) -> list:
        self.workspace(principal, workspace_id)
        return review_items(self.store, workspace_id)

    def review(self, principal: Principal, workspace_id: str, request_id: str,
               target_id: str, fields: dict, reason: str) -> dict:
        self.workspace(principal, workspace_id)
        if not isinstance(target_id, str) or ":" not in target_id:
            raise AgentError("invalid_target", "修正目标格式不正确")
        kind, row_id = target_id.split(":", 1)
        request_id = self._request_id(request_id)
        with self.store.transaction():
            cached, claim, recovered = self._idempotent(
                principal, workspace_id, "review", request_id,
                {"target_id": target_id, "fields": fields, "reason": reason},
            )
            if cached is not None:
                return {**cached, "duplicate": True}
            row = self.store.get_entry(row_id) if kind == "entry" else self.store.get_sale(row_id) if kind == "sale" else None
            if not row or row.get("shop_id") != workspace_id:
                raise AgentError("target_forbidden", "修正目标不存在或不属于当前工作空间", status=403)
            try:
                review_request_id = "agent-v2-" + self._business_identity(principal, request_id)
                legacy_id = "agent-" + hashlib.sha256(
                    f"{principal.id}:{request_id}".encode("utf-8")
                ).hexdigest()
                if (self.store.record_find(workspace_id, f"review:{review_request_id}")
                        or self.store.record_find(workspace_id, f"review:{legacy_id}")):
                    raise AgentError("recovery_needs_review", "旧修正已生效但缺少完整响应，请人工核对", status=409)
                result = apply_review(self.store, workspace_id, review_request_id,
                                      target_id, fields, reason)
                self._finish(claim, result)
                return result
            except AgentError:
                raise
            except ValueError as exc:
                raise AgentError("invalid_review", str(exc)) from exc

    @staticmethod
    def _csv(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AgentError("invalid_csv", "CSV 内容不能为空")
        if len(value.encode("utf-8")) > 2_000_000:
            raise AgentError("csv_too_large", "CSV 内容不能超过 2 MB")
        return value

    def _plan_csv(self, csv: str, workspace_id: str | None = None) -> tuple[dict, str]:
        csv = self._csv(csv)
        digest = hashlib.sha256(csv.encode("utf-8")).hexdigest()
        mappings = None
        if workspace_id is not None:
            mappings = self._fields().import_mappings(workspace_id)["mappings"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "import.csv"
            path.write_text(csv, encoding="utf-8")
            plan = plan_import(str(path), column_mappings=mappings or None)
        return plan, digest

    def _save_import_preview(self, principal, workspace_id, plan, digest):
        token = "preview-" + uuid.uuid4().hex
        with self.store.transaction():
            existing_batch = self.store.batch_find_by_hash(workspace_id, digest)
            saved_plan = {**plan, "_agent_existing_batch_id":
                          existing_batch["id"] if existing_batch else None}
            self.store.agent_import_preview_put(token, principal.id, workspace_id, digest, saved_plan)
        return {"preview": plan, "sha256": digest, "preview_token": token,
                "workspace_id": workspace_id, "confirmation_required": True,
                "expires_in_seconds": PREVIEW_TTL_SECONDS,
                "duplicate_file": existing_batch is not None,
                "existing_batch": ({key: existing_batch[key] for key in ("id", "filename", "rows")}
                                   if existing_batch else None)}

    def preview_import(self, principal: Principal, workspace_id: str, csv: str) -> dict:
        self.workspace(principal, workspace_id)
        plan, digest = self._plan_csv(csv, workspace_id)
        return self._save_import_preview(principal, workspace_id, plan, digest)

    def _source_dir(self) -> Path:
        location = getattr(self.store, "path", None)
        if not location or location == ":memory:":
            raise AgentError("source_unavailable", "当前账本不能保留上传原件", status=409)
        directory = Path(location).parent / "import-sources"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _stage_source(self, content: bytes, digest: str) -> Path:
        """先把校验过的原件写成普通文件，再解析。文件和账本不是同一个事务。"""
        destination = self._source_dir() / digest
        if destination.is_symlink() or destination.parent.is_symlink():
            raise AgentError("invalid_source", "上传原件位置不正确", status=409)
        if not destination.exists():
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        read_back = destination.read_bytes()
        if hashlib.sha256(read_back).hexdigest() != digest:
            raise AgentError("invalid_source", "上传原件校验失败", status=409)
        return destination

    def preview_xlsx(self, principal: Principal, workspace_id: str, content: bytes,
                     *, filename: str, sheet_index: int = 0, header_row: int = 0,
                     direction=None, excluded_rows=None) -> dict:
        """预览已通过上传限额的 XLSX。提交仍要确认，且绑定原件摘要和工作表。"""
        self.workspace(principal, workspace_id)
        if not isinstance(content, bytes) or not content:
            raise AgentError("invalid_file", "XLSX 内容不能为空")
        if len(content) > 10 * 1024 * 1024:
            raise AgentError("file_too_large", "XLSX 不能超过 10MiB", status=413)
        if not isinstance(filename, str) or not filename.lower().endswith(".xlsx"):
            raise AgentError("invalid_filename", "只接受 .xlsx")
        if isinstance(sheet_index, bool) or not isinstance(sheet_index, int) or sheet_index < 0:
            raise AgentError("invalid_sheet", "工作表序号不正确")
        if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 0:
            raise AgentError("invalid_header", "表头行不正确")
        digest = hashlib.sha256(content).hexdigest()
        path = self._stage_source(content, digest)
        mappings = self._fields().import_mappings(workspace_id)["mappings"]
        version = self._fields().config_version(workspace_id)
        try:
            plan = plan_import(str(path), header_row=header_row,
                               column_mappings=mappings or None, sheet_index=sheet_index,
                               direction=direction, excluded_rows=excluded_rows)
        except ValueError as exc:
            raise AgentError("invalid_xlsx", str(exc)) from exc
        if plan.get("sheet") is None:
            raise AgentError("invalid_xlsx", "XLSX 缺少稳定工作表编号")
        batch_identity = digest + ":sheet:" + str(plan["sheet"])
        plan.update({"source_mode": "uploaded_file", "file_sha256": digest,
                     "batch_identity": batch_identity, "custom_fields_version": version})
        saved = self._save_import_preview(principal, workspace_id, plan, batch_identity)
        saved.update({"source_mode": "uploaded_file", "file_sha256": digest,
                      "batch_identity": batch_identity, "sha256": batch_identity})
        return saved

    def commit_xlsx(self, principal: Principal, workspace_id: str, request_id: str, *,
                    confirmed: bool = False, filename: str = "import.xlsx",
                    preview_token: str | None = None) -> dict:
        """只提交已经预览过的 XLSX。不接收文件字节，也不重读表格。"""
        self.workspace(principal, workspace_id)
        if not isinstance(filename, str) or not 1 <= len(filename) <= 200:
            raise AgentError("invalid_filename", "filename 必须是 1 到 200 个字符")
        if confirmed is not True:
            raise AgentError("confirmation_required", "导入提交前必须明确 confirmed=true", status=409)
        self._request_id(request_id)
        if not isinstance(preview_token, str) or not preview_token:
            raise AgentError("preview_required", "必须先提交同一份 XLSX 的预览", status=409)
        preview = self.store.agent_import_preview_get(preview_token, principal.id, workspace_id)
        plan = preview["plan"] if preview else {}
        digest = plan.get("file_sha256", "")
        with self.store.transaction():
            cached, claim, recovered = self._idempotent(
                principal, workspace_id, "xlsx_import_commit", request_id,
                {"file_sha256": digest, "sheet": plan.get("sheet"), "filename": filename,
                 "confirmed": confirmed, "preview_token": preview_token},
            )
            if cached is not None:
                return {**cached, "duplicate": True}
            self._checked_xlsx_preview(preview, principal, workspace_id)
            if not self.store.agent_import_preview_consume(
                preview_token, principal.id, workspace_id, request_id
            ):
                raise AgentError("preview_consumed", "预览已被使用，请重新预览", status=409)
            try:
                result = commit_import(self.store, workspace_id, plan, filename, digest)
            except ValueError as exc:
                raise AgentError("import_conflict", str(exc), status=409) from exc
            response = {"result": result, "sha256": plan["batch_identity"],
                        "file_sha256": digest, "workspace_id": workspace_id,
                        "request_id": request_id}
            self._finish(claim, response)
            return response

    def _checked_xlsx_preview(self, preview, principal, workspace_id):
        if not preview or preview["plan"].get("source_mode") != "uploaded_file":
            raise AgentError("preview_mismatch", "预览不是这份 XLSX", status=409)
        plan = preview["plan"]
        digest = plan.get("file_sha256", "")
        if preview["sha256"] != plan.get("batch_identity") or ":sheet:" not in preview["sha256"]:
            raise AgentError("preview_mismatch", "预览没有绑定工作表", status=409)
        path = self._source_dir() / digest
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise AgentError("source_missing", "上传原件已缺失，请重新上传", status=409)
        if plan.get("custom_fields_version") != self._fields().config_version(workspace_id):
            raise AgentError("config_version_conflict", "字段配置在预览后发生了变化", status=409)
        if preview.get("consumed_request_id") is not None:
            raise AgentError("preview_consumed", "预览已用于另一提交，请重新预览", status=409)
        age = self._preview_age(preview)
        if not 0 <= age < PREVIEW_TTL_SECONDS:
            raise AgentError("preview_expired", "预览已过期，请重新预览并确认", status=409)
        if plan.get("direction_required") and plan.get("direction") not in ("purchase", "sale"):
            raise AgentError("direction_required", "请先确认这是采购还是销售", status=409)
        existing_batch = self.store.batch_find_by_hash(workspace_id, plan["batch_identity"])
        if existing_batch and plan.get("_agent_existing_batch_id") != existing_batch["id"]:
            raise AgentError("recovery_needs_review", "预览后出现了同文件批次但缺少本请求完整响应，请核对后重新预览", status=409)
        if plan["skipped"]:
            raise AgentError("import_needs_review", "存在无法识别的行，请先修正或重新预览可导入行",
                             details={"skipped": plan["skipped"]}, status=409)

    @staticmethod
    def _preview_age(preview) -> float:
        try:
            created = dt.datetime.fromisoformat(preview["created_at"])
            return (dt.datetime.now(created.tzinfo) - created).total_seconds()
        except (KeyError, TypeError, ValueError, OverflowError):
            return -1

    def commit_import(self, principal: Principal, workspace_id: str, csv: str | None,
                      request_id: str, *, confirmed: bool = False, filename: str = "agent.csv",
                      preview_token: str | None = None, source_sha256: str | None = None) -> dict:
        self.workspace(principal, workspace_id)
        if not isinstance(filename, str) or not 1 <= len(filename) <= 200:
            raise AgentError("invalid_filename", "filename 必须是 1 到 200 个字符")
        if confirmed is not True:
            raise AgentError("confirmation_required", "导入提交前必须明确 confirmed=true", status=409)
        self._request_id(request_id)
        csv = self._csv(csv)
        digest = hashlib.sha256(csv.encode("utf-8")).hexdigest()
        if source_sha256 is not None and source_sha256 != digest:
            raise AgentError("invalid_source", "CSV 来源摘要必须等于 CSV 正文", status=409)
        if not isinstance(preview_token, str) or not preview_token:
            raise AgentError("preview_required", "必须先提交同一份 CSV 的预览", status=409)
        with self.store.transaction():
            cached, claim, recovered = self._idempotent(
                principal, workspace_id, "import_commit", request_id,
                {"sha256": digest, "filename": filename, "confirmed": confirmed,
                 "preview_token": preview_token},
            )
            if cached is not None:
                return {**cached, "duplicate": True}
            preview = self.store.agent_import_preview_get(preview_token, principal.id, workspace_id)
            if preview and (preview["plan"].get("source_mode") == "uploaded_file"
                            or ":sheet:" in str(preview.get("sha256", ""))):
                raise AgentError("preview_mismatch", "XLSX 预览不能用 CSV 提交", status=409)
            if not preview or preview["sha256"] != digest:
                raise AgentError("preview_mismatch", "CSV 与预览内容不一致，请重新预览", status=409)
            if preview.get("consumed_request_id") is not None:
                raise AgentError("preview_consumed", "预览已用于另一提交，请重新预览", status=409)
            if not 0 <= self._preview_age(preview) < PREVIEW_TTL_SECONDS:
                raise AgentError("preview_expired", "预览已过期，请重新预览并确认", status=409)
            plan = preview["plan"]
            existing_batch = self.store.batch_find_by_hash(workspace_id, digest)
            if (existing_batch
                    and plan.get("_agent_existing_batch_id") != existing_batch["id"]):
                raise AgentError("recovery_needs_review", "预览后出现了同文件批次但缺少本请求完整响应，请核对后重新预览", status=409)
            if plan["skipped"]:
                raise AgentError("import_needs_review", "存在无法识别的行，请先修正或确认预览", details={"skipped": plan["skipped"]}, status=409)
            if not self.store.agent_import_preview_consume(
                preview_token, principal.id, workspace_id, request_id
            ):
                raise AgentError("preview_consumed", "预览已被使用，请重新预览", status=409)
            try:
                result = commit_import(self.store, workspace_id, plan, filename, digest)
                response = {"result": result, "sha256": digest, "workspace_id": workspace_id,
                            "request_id": request_id}
                self._finish(claim, response)
                return response
            except ValueError as exc:
                raise AgentError("import_conflict", str(exc), status=409) from exc
