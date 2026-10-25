"""本地试记页：python3 -m engine.web。只监听回环地址，使用独立测试库。"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import hashlib
import tempfile
import re
import math
import sqlite3
import uuid
from urllib.parse import quote

from .agent_service import AgentError, AgentService, Principal
from .cost import set_recipe, cost_table
from .importer import plan_import, commit_import

from .model import today_iso
from .observe import ingest
from .capabilities import product_capabilities
from .review import apply_review, corrections_map, review_items
from .store import SCHEMA_VERSION, SqliteStore, database_schema_ready

ROOT = Path(__file__).resolve().parent.parent
SHOP = "web-demo"
MAX_BODY = 32768


def state(store, month=None):
    from .model import month_range
    start, end = month_range(month) if month else ('0000', '9999')
    return {
        "today": today_iso(),
        "entries": store.list_entries(SHOP, "0000", "9999"),
        "observations": store.observation_list(SHOP, "0000", "9999"),
        "records": [dict(row) for row in store.conn.execute(
            "SELECT * FROM record WHERE shop_id=? ORDER BY created_at DESC, rowid DESC LIMIT 100",
            (SHOP,))],
        'summary': store.summary(SHOP, start, end),
        'sales': store.list_sales(SHOP, start, end),
        'costs': cost_table(store, SHOP, as_of=min(end, today_iso())),
        'review': review_items(store, SHOP),
        # 修正历史按行给全，不受「原话只显示最近 100 条」的截断影响。
        'corrections': corrections_map(store, SHOP),
        'month': month,
    }


class DemoHandler(BaseHTTPRequestHandler):
    def _request_id(self):
        # Trace IDs are server-owned; a caller may not forge log correlation.
        return uuid.uuid4().hex

    def _v1_error(self, exc, request_id):
        if isinstance(exc, AgentError):
            status, code, message, details = exc.status, exc.code, exc.message, exc.details
        else:
            logging.exception("agent request failed request_id=%s", request_id)
            status, code, message, details = 500, "internal_error", "请求未完成", {}
        extra = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        body = {"error": {"code": code, "message": message, "details": details,
                           "request_id": request_id}, "request_id": request_id}
        return self._send(status, body, headers=extra)

    def _principal(self):
        """Authenticate v1 with a configured bearer token; never trust query owner."""
        expected = getattr(self.server, "api_key", "")
        if not expected:
            raise AgentError("not_configured", "v1 API 尚未配置认证密钥", status=503)
        header = self.headers.get("Authorization", "")
        scheme, separator, token = header.partition(" ")
        if (scheme.lower() != "bearer" or not separator or not token.isascii()
                or not expected.isascii() or not hmac.compare_digest(token, expected)):
            raise AgentError("unauthorized", "需要有效的 Bearer 认证", status=401)
        principal_id = getattr(self.server, "principal_id", "agent")
        owner_id = getattr(self.server, "owner_id", "local")
        return Principal(str(principal_id), str(owner_id))

    def _v1_store(self):
        return SqliteStore(str(self.server.db_path))

    def _ready_probe(self):
        path = Path(self.server.db_path).resolve()
        if not path.is_file():
            return False, "database_missing"
        uri = "file:" + quote(str(path)) + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                try:
                    version = int(version[0]) if version else None
                except (TypeError, ValueError):
                    version = None
                if integrity != "ok" or version != SCHEMA_VERSION or not database_schema_ready(conn):
                    return False, "database_not_ready"
                workspace_id = getattr(self.server, "workspace_id", "")
                if workspace_id:
                    owner_id = getattr(self.server, "owner_id", "local")
                    row = conn.execute(
                        "SELECT 1 FROM workspace_meta WHERE shop_id=? AND owner_id=?",
                        (workspace_id, owner_id),
                    ).fetchone()
                    if not row:
                        return False, "workspace_missing"
                return True, "ready"
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return False, "database_unavailable"

    def _v1_json(self):
        if self.headers.get_content_type() != "application/json":
            raise AgentError("unsupported_media_type", "需要 JSON 格式", status=415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AgentError("invalid_content_length", "请求长度不正确") from exc
        if not 0 < size <= MAX_BODY:
            raise AgentError("body_too_large", "请求体超过限制", status=413)
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AgentError("invalid_json", "无法读取 JSON 请求") from exc
        if not isinstance(payload, dict):
            raise AgentError("invalid_json", "JSON 请求必须是对象")
        return payload

    def _v1_query(self):
        return {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}

    def _check_workspace(self, workspace):
        configured = getattr(self.server, "workspace_id", "")
        if configured and workspace != configured:
            raise AgentError("workspace_forbidden", "工作空间不存在或不属于当前调用主体", status=403)
        return workspace

    def _v1_get(self, path, service, principal):
        query = self._v1_query()
        workspace = query.get("workspace_id")
        if path == "/v1/capabilities":
            return self._send(200, service.capabilities())
        if path == "/v1/workspaces":
            workspaces = service.list_workspaces(principal)
            configured = getattr(self.server, "workspace_id", "")
            if configured:
                workspaces = [item for item in workspaces if item.get("id") == configured]
            return self._send(200, {"workspaces": workspaces})
        if not workspace:
            raise AgentError("workspace_required", "必须指定 workspace_id")
        workspace = self._check_workspace(workspace)
        if path == "/v1/timeline":
            fields = {key: query[key] for key in ("q", "start", "end", "parse_status", "source") if key in query}
            if "page_size" in query:
                fields["page_size"] = query["page_size"]
            if "before" in query:
                try:
                    fields["before"] = tuple(query["before"].split("|", 2))
                except Exception as exc:
                    raise AgentError("invalid_cursor", "before 游标格式不正确") from exc
            return self._send(200, service.timeline(principal, workspace, **fields))
        if path == "/v1/summary":
            return self._send(200, {"summary": service.summary(principal, workspace, query.get("month"))})
        if path == "/v1/entries":
            return self._send(200, {"entries": service.entries(principal, workspace, query.get("start"), query.get("end"), query.get("type"))})
        if path == "/v1/sales":
            return self._send(200, {"sales": service.sales(principal, workspace, query.get("start"), query.get("end"))})
        if path == "/v1/observations":
            return self._send(200, {"observations": service.observations(principal, workspace, query.get("start"), query.get("end"), query.get("kind"), query.get("subject"), query.get("measure"))})
        if path == "/v1/reviews":
            return self._send(200, {"reviews": service.reviews(principal, workspace)})
        if path.startswith("/v1/captures/"):
            capture_id = path.rsplit("/", 1)[-1]
            return self._send(200, service.capture_detail(principal, workspace, capture_id))
        raise AgentError("not_found", "没有这个 v1 接口", status=404)

    def _v1_post(self, path, service, principal):
        payload = self._v1_json()
        workspace = payload.get("workspace_id")
        if path == "/v1/captures":
            if not workspace:
                raise AgentError("workspace_required", "必须指定 workspace_id")
            workspace = self._check_workspace(workspace)
            result = service.capture_text(principal, workspace, payload.get("text"), payload.get("request_id"),
                                          source=payload.get("source", "agent"), received_at=payload.get("received_at"),
                                          context=payload.get("context"), intent=payload.get("intent"),
                                          profile_id=payload.get("profile_id"))
            return self._send(200, result)
        if not workspace:
            raise AgentError("workspace_required", "必须指定 workspace_id")
        workspace = self._check_workspace(workspace)
        if path == "/v1/reviews":
            review = service.review(principal, workspace, payload.get("request_id"),
                                    payload.get("target_id"), payload.get("fields"), payload.get("reason"))
            return self._send(200, {"review": review, "duplicate": review["duplicate"]})
        if path == "/v1/imports/preview":
            return self._send(200, service.preview_import(principal, workspace, payload.get("csv")))
        if path == "/v1/imports/commit":
            return self._send(200, service.commit_import(
                principal, workspace, payload.get("csv"), payload.get("request_id"),
                confirmed=payload.get("confirmed", False),
                filename=payload.get("filename", "agent.csv"),
                preview_token=payload.get("preview_token"),
            ))
        raise AgentError("not_found", "没有这个 v1 接口", status=404)

    def _v1_method_allowed(self, path):
        allow = {
            "/v1/capabilities": "GET", "/v1/workspaces": "GET",
            "/v1/captures": "POST", "/v1/timeline": "GET",
            "/v1/summary": "GET", "/v1/entries": "GET",
            "/v1/sales": "GET", "/v1/observations": "GET",
            "/v1/reviews": "GET, POST",
            "/v1/imports/preview": "POST", "/v1/imports/commit": "POST",
        }.get(path)
        if allow is None and re.fullmatch(r"/v1/captures/[^/]+", path):
            allow = "GET"
        if allow is None:
            raise AgentError("not_found", "没有这个 v1 接口", status=404)
        if self.command not in allow.split(", "):
            self._method_not_allowed(allow)
            return False
        return True

    def _method_not_allowed(self, allow):
        trace = getattr(self, "trace_id", None)
        return self._send(405, {"error": {"code": "method_not_allowed", "message": "请求方法不支持",
                                           "details": {}, "request_id": trace}, "request_id": trace},
                          headers={"Allow": allow})

    def do_PUT(self):
        request_id = self._request_id()
        self.trace_id = request_id
        path = urlsplit(self.path).path
        if path.startswith("/v1/"):
            try:
                self._v1_method_allowed(path)
            except AgentError as exc:
                return self._v1_error(exc, request_id)
            return
        return self._send(405, {"error": "请求方法不支持。"}, headers={"Allow": "GET, POST"})

    def do_PATCH(self):
        return self.do_PUT()

    def do_DELETE(self):
        return self.do_PUT()

    def __getattr__(self, name):
        # BaseHTTPRequestHandler otherwise turns unimplemented verbs into HTML 501s.
        if name.startswith("do_") and urlsplit(self.path).path.startswith("/v1/"):
            return self.do_PUT
        raise AttributeError(name)

    def log_message(self, fmt, *args):
        # 只记录脱敏后的方法和路径，不把 query、正文或 Authorization 写进日志。
        logging.info("http %s %s", self.command, urlsplit(self.path).path)

    def _send(self, code, body, content_type="application/json; charset=utf-8", headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        trace_id = getattr(self, "trace_id", None)
        if trace_id:
            self.send_header("X-Request-ID", trace_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        request_id = self._request_id()
        self.trace_id = request_id
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self._send(200, {"status": "ok", "request_id": request_id})
        if path == "/readyz":
            ok, reason = self._ready_probe()
            if not ok:
                return self._v1_error(AgentError("not_ready", "服务尚未就绪", status=503,
                                                 details={"reason": reason}), request_id)
            return self._send(200, {"status": "ready", "request_id": request_id})
        if path.startswith("/v1/"):
            store = None
            try:
                principal = self._principal()
                if not self._v1_method_allowed(path):
                    return
                if path == "/v1/capabilities":
                    return self._v1_get(path, AgentService(None), principal)
                ok, reason = self._ready_probe()
                if not ok:
                    raise AgentError("not_ready", "服务尚未就绪", status=503,
                                     details={"reason": reason})
                store = self._v1_store()
                return self._v1_get(path, AgentService(store), principal)
            except Exception as exc:
                return self._v1_error(exc, request_id)
            finally:
                if store:
                    store.close()
        if getattr(self.server, "api_only", False):
            return self._send(404, {"error": "没有这个页面。"})
        if not self._local_host():
            return self._send(403, {"error": "请从本机地址打开测试页。"})
        if path in ("/", "/demo.html"):
            return self._send(200, (ROOT / "web" / "demo.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/capabilities":
            return self._send(200, product_capabilities())
        if path == "/api/workspaces":
            store = SqliteStore(str(self.server.db_path))
            try:
                owner = parse_qs(urlsplit(self.path).query).get("owner", ["local"])[0]
                return self._send(200, {"workspaces": store.workspace_list(owner)})
            finally:
                store.close()
        if path == "/api/timeline":
            store = SqliteStore(str(self.server.db_path))
            try:
                query = parse_qs(urlsplit(self.path).query)
                workspace = query.get("workspace", [SHOP])[0]
                page_size = int(query.get("page_size", [30])[0])
                if not 1 <= page_size <= 100:
                    raise ValueError("page_size 必须在 1 到 100 之间")
                result = store.record_timeline(
                    workspace,
                    q=query.get("q", [""])[0],
                    start=query.get("start", [None])[0],
                    end=query.get("end", [None])[0],
                    parse_status=query.get("status", [None])[0],
                    source=query.get("source", [None])[0],
                    page_size=page_size,
                )
                return self._send(200, result)
            except (ValueError, TypeError):
                return self._send(400, {"error": "时间线参数不正确。"})
            finally:
                store.close()
        if path.startswith("/api/records/"):
            record_id = path.rsplit("/", 1)[-1]
            store = SqliteStore(str(self.server.db_path))
            try:
                detail = store.record_detail(SHOP, record_id)
                return self._send(200 if detail else 404,
                                  detail or {"error": "没有这条原话。"})
            finally:
                store.close()
        if path == "/api/state":
            store = SqliteStore(str(self.server.db_path))
            try:
                month = parse_qs(urlsplit(self.path).query).get('month', [None])[0]
                if month is not None and not re.fullmatch(r'[0-9]{4}-[0-9]{2}', month):
                    raise ValueError('月份格式为 YYYY-MM')
                return self._send(200, state(store, month))
            except (ValueError, IndexError):
                return self._send(400, {'error': '月份格式为 YYYY-MM'})
            finally:
                store.close()
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._send(404, {"error": "没有这个页面。"})

    def do_POST(self):
        request_id = self._request_id()
        self.trace_id = request_id
        path = urlsplit(self.path).path
        if path.startswith("/v1/"):
            store = None
            try:
                principal = self._principal()
                if not self._v1_method_allowed(path):
                    return
                ok, reason = self._ready_probe()
                if not ok:
                    raise AgentError("not_ready", "服务尚未就绪", status=503,
                                     details={"reason": reason})
                store = self._v1_store()
                return self._v1_post(path, AgentService(store), principal)
            except Exception as exc:
                return self._v1_error(exc, request_id)
            finally:
                if store:
                    store.close()
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if getattr(self.server, "api_only", False):
            return self._send(404, {"error": "没有这个接口。"})
        if not self._local_host() or (origin and origin != f"http://{host}"):
            return self._send(403, {"error": "请从本机测试页提交。"})
        path = urlsplit(self.path).path
        if path not in ('/api/records', '/api/review', '/api/recipes',
                        '/api/import/preview', '/api/import/commit'):
            return self._send(404, {"error": "没有这个接口。"})
        if self.headers.get_content_type() != "application/json":
            return self._send(415, {"error": "需要 JSON 格式。"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(400, {"error": "请求长度不正确。"})
        if not 0 < size <= MAX_BODY:
            return self._send(413, {"error": "一次请记录 2000 字以内。"})
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError()
            if path == '/api/records':
                text, request_id = payload.get('text'), payload.get('request_id')
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    return self._send(400, {'error': '请输入 1 到 2000 字的记录。'})
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                    return self._send(400, {'error': '缺少记录编号，请刷新页面后重试。'})
            if path == '/api/review' and not isinstance(payload.get('fields'), dict):
                return self._send(400, {'error': '修正内容格式不正确。'})
        except (UnicodeDecodeError, ValueError, RecursionError):
            # RecursionError：深层嵌套的 JSON 会让 json.loads 栈溢出。
            # 接不住的话连接会被直接掐断，客户端只看到网络错误，拿不到任何文案。
            return self._send(400, {"error": "无法读取这条记录，请刷新页面后重试。"})
        store = SqliteStore(str(self.server.db_path))
        try:
            store.ensure_shop(SHOP, "网页测试店")
            if path == '/api/review':
                result = apply_review(store, SHOP, payload.get('request_id'),
                                      payload.get('target_id'), payload.get('fields'),
                                      payload.get('reason'))
                return self._send(200, {'review': result, 'state': state(store)})
            if path == '/api/recipes':
                product = payload.get('product')
                recipe = payload.get('recipe')
                category = payload.get('category')
                effective_date = payload.get('effective_date')
                if not isinstance(product, str) or not product.strip():
                    raise ValueError('商品名必须是非空文字')
                if not isinstance(recipe, str) or not recipe.strip():
                    raise ValueError('配方必须是文字')
                if category is not None and not isinstance(category, str):
                    raise ValueError('商品品类必须是文字')
                if effective_date is not None and not isinstance(effective_date, str):
                    raise ValueError('生效日期必须是 YYYY-MM-DD')
                raw_price = payload.get('price')
                raw_units = payload.get('units', 1)
                if isinstance(raw_price, bool) or isinstance(raw_units, bool):
                    raise ValueError('售价和产出份数要填数字')
                try:
                    price = float(raw_price) if raw_price not in (None, '') else None
                    units = float(raw_units)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError('售价和产出份数要填数字')
                if not math.isfinite(units) or (price is not None and not math.isfinite(price)):
                    raise ValueError('售价和产出份数必须是有限数字')
                result = set_recipe(store, SHOP, product, recipe, price=price,
                    units=units, category=category, effective_date=effective_date)
                return self._send(200, {'recipe': result, 'state': state(store)})
            if path.startswith('/api/import/'):
                csv = payload.get('csv')
                if not isinstance(csv, str) or not csv.strip():
                    raise ValueError('请提供 CSV 内容')
                with tempfile.TemporaryDirectory() as tmp:
                    source = Path(tmp) / 'import.csv'
                    source.write_text(csv, encoding='utf-8')
                    plan = plan_import(str(source))
                if path.endswith('/preview'):
                    return self._send(200, {'plan': plan})
                if plan['skipped']:
                    raise ValueError('请修正无法识别的行后再导入，避免缺行')
                result = commit_import(store, SHOP, plan, '网页CSV', hashlib.sha256(csv.encode()).hexdigest())
                return self._send(200, {'result': result, 'state': state(store)})
            receipt = ingest(store, SHOP, text, source='web', dedup_ref=f'web:{request_id}')
            written = {'entries': [store.get_entry(i) for i in receipt['entry_ids']],
                       'sales': [store.get_sale(i) for i in receipt['sale_ids']]}
            return self._send(200, {'receipt': receipt, 'written': written, 'state': state(store)})
        except (ValueError, TypeError, OverflowError) as exc:
            # OverflowError：几百位的大整数 float() 会溢出，属于输入问题，不是服务器故障。
            return self._send(400, {'error': str(exc)})
        except Exception:
            logging.exception("本地记录失败")
            return self._send(500, {"error": "记录未完成。请重试，系统会用同一编号避免重复记录。"})
        finally:
            store.close()


def make_server(port=8765, db_path=None, *, api_key=None, owner_id="local",
                principal_id="agent", bind="127.0.0.1", workspace_id="", api_only=False):
    if bind not in ("127.0.0.1", "localhost", "::1") and len(api_key or "") < 16:
        raise ValueError("非回环监听的 API key 至少需要 16 个字符")
    server = HTTPServer((bind, port), DemoHandler)
    server.db_path = Path(db_path) if db_path else ROOT / "data" / "web-demo.db"
    server.api_key = api_key or ""
    server.owner_id = owner_id
    server.principal_id = principal_id
    server.workspace_id = workspace_id
    server.api_only = bool(api_only)
    return server


def main():
    p = argparse.ArgumentParser(description="随口记本地试用页")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--db", default=str(ROOT / "data" / "web-demo.db"))
    args = p.parse_args()
    server = make_server(args.port, args.db)
    print(f"随口记试用页：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
