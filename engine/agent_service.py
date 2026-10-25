"""受控的 Agent 业务门面。

REST、iLink 和未来 MCP 都通过这里访问业务，不直接操作 SQLite。
Agent 先整理口语记录，门面校验结构化事实、工作空间授权和结果；旧原话
模式、修正与导入仍复用现有 engine 模块。所有写入共用 Store 的账务投影。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .agent_normalization import persist_structured, validate_structured
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
    """已由传输层认证的调用主体。"""

    id: str
    owner_id: str


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
            "list_reviews": {"method": "GET", "path": "/v1/reviews"},
            "apply_review": {"method": "POST", "path": "/v1/reviews", "confirmation": "required"},
            "preview_import": {"method": "POST", "path": "/v1/imports/preview", "confirmation": "required"},
            "commit_import": {"method": "POST", "path": "/v1/imports/commit", "confirmation": "required"},
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
                     peer=None, structured=None) -> dict:
        workspace = self.workspace(principal, workspace_id)
        request_id = self._request_id(request_id)
        text = self._text(text)
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
        page_size = filters.pop("page_size", 30)
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            raise AgentError("invalid_page_size", "page_size 必须是数字")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise AgentError("invalid_page_size", "page_size 必须在 1 到 100 之间")
        filters["page_size"] = page_size
        return self.store.record_timeline(workspace_id, **filters)

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

    def _plan_csv(self, csv: str) -> tuple[dict, str]:
        csv = self._csv(csv)
        digest = hashlib.sha256(csv.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "import.csv"
            path.write_text(csv, encoding="utf-8")
            plan = plan_import(str(path))
        return plan, digest

    def preview_import(self, principal: Principal, workspace_id: str, csv: str) -> dict:
        self.workspace(principal, workspace_id)
        plan, digest = self._plan_csv(csv)
        token = "preview-" + uuid.uuid4().hex
        # Remember what the user actually previewed. A batch appearing later
        # without a complete response may be an orphan from a legacy request.
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

    def commit_import(self, principal: Principal, workspace_id: str, csv: str,
                      request_id: str, *, confirmed: bool = False, filename: str = "agent.csv",
                      preview_token: str | None = None) -> dict:
        self.workspace(principal, workspace_id)
        if not isinstance(filename, str) or not 1 <= len(filename) <= 200:
            raise AgentError("invalid_filename", "filename 必须是 1 到 200 个字符")
        if confirmed is not True:
            raise AgentError("confirmation_required", "导入提交前必须明确 confirmed=true", status=409)
        self._request_id(request_id)
        csv = self._csv(csv)
        digest = hashlib.sha256(csv.encode("utf-8")).hexdigest()
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
            if not preview or preview["sha256"] != digest:
                raise AgentError("preview_mismatch", "CSV 与预览内容不一致，请重新预览", status=409)
            if preview.get("consumed_request_id") is not None:
                raise AgentError("preview_consumed", "预览已用于另一提交，请重新预览", status=409)
            try:
                created = dt.datetime.fromisoformat(preview["created_at"])
                age = (dt.datetime.now(created.tzinfo) - created).total_seconds()
            except (KeyError, TypeError, ValueError, OverflowError):
                age = -1
            if not 0 <= age < PREVIEW_TTL_SECONDS:
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
