"""随口记 · 存储层。

`Store` 是唯一的数据出入口，先用 SQLite 落本地；以后接腾讯云 CloudBase
时实现同一个接口即可，上层（解析、成本、报表、CLI）不用改。

所有业务表都带 `shop_id`，本地单店用 `default`，上云后天然支持多店。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from contextlib import contextmanager
import math

from .model import (
    FIXED_CATEGORIES,
    SHOP_DEFAULT,
    SOURCE_CHAT,
    TYPE_EXPENSE,
    TYPE_PURCHASE,
    TYPE_REVENUE,
    new_id,
    now_iso,
    round2,
)

SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_request (
  principal_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  request_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  response TEXT,
  created_at TEXT NOT NULL,
  lease_token TEXT,
  PRIMARY KEY(principal_id, workspace_id, operation, request_id)
);

CREATE TABLE IF NOT EXISTS agent_import_preview (
  token TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  plan TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_request_id TEXT
);

CREATE TABLE IF NOT EXISTS shop (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 工作空间元数据旁路保存，避免改变旧 shop 表的结构和语义。
CREATE TABLE IF NOT EXISTS workspace_meta (
  shop_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL DEFAULT 'local',
  profile_id TEXT NOT NULL DEFAULT 'restaurant',
  timezone TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  date TEXT NOT NULL,
  type TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  item TEXT,
  qty REAL,
  unit TEXT,
  note TEXT,
  source TEXT NOT NULL,
  raw_text TEXT,
  batch_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_shop_date ON entry(shop_id, date);

CREATE TABLE IF NOT EXISTS product (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  price REAL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, name)
);

CREATE TABLE IF NOT EXISTS ingredient (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  base_unit TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, name)
);

CREATE TABLE IF NOT EXISTS recipe_line (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  ingredient_id TEXT NOT NULL,
  qty REAL NOT NULL,
  units REAL NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(product_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS sale (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  date TEXT NOT NULL,
  product_id TEXT,
  product_name TEXT NOT NULL,
  qty REAL NOT NULL,
  amount REAL,
  source TEXT NOT NULL,
  batch_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_shop_date ON sale(shop_id, date);

CREATE TABLE IF NOT EXISTS discrepancy (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  date TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  expected REAL,
  actual REAL,
  unit TEXT,
  question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_disc_shop_status ON discrepancy(shop_id, status);

CREATE TABLE IF NOT EXISTS import_batch (
  id TEXT PRIMARY KEY,
  shop_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  kind TEXT NOT NULL,
  rows INTEGER NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_hash ON import_batch(shop_id, sha256);

-- ------------------------------------------------------------------ 三层记录模型
-- 原话层：不可变。加新数据类型永远不动这张表。
CREATE TABLE IF NOT EXISTS record (
  id             TEXT PRIMARY KEY,
  shop_id        TEXT NOT NULL,
  raw_text       TEXT NOT NULL,
  source         TEXT NOT NULL,
  peer           TEXT,
  dedup_ref      TEXT,
  occurred_at    TEXT,
  received_at    TEXT NOT NULL,
  media_path     TEXT,
  media_sha256   TEXT,
  parse_status   TEXT NOT NULL DEFAULT 'pending',
  parser_version TEXT,
  batch_id       TEXT,
  parse_base_date TEXT,
  context        TEXT NOT NULL DEFAULT '',
  intent         TEXT NOT NULL DEFAULT 'unknown',
  routing_status TEXT NOT NULL DEFAULT 'unclassified',
  profile_id     TEXT NOT NULL DEFAULT 'restaurant',
  ledger_refs    TEXT NOT NULL DEFAULT '[]',
  -- 修正来源：本条原话改的是哪一行账目（entry:<id> / sale:<id>），
  -- correction_detail 存改动前后的字段值，账目本身不保留旧值也能追溯。
  correction_of  TEXT,
  correction_detail TEXT,
  created_at     TEXT NOT NULL
);
-- 一个来源只留一条原话：msg:<消息id> / entry:<流水id> / media:<sha256>。
-- 聊天靠它防重复投递，回填靠它保证幂等。
CREATE UNIQUE INDEX IF NOT EXISTS idx_record_dedup
  ON record(shop_id, dedup_ref) WHERE dedup_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_record_time ON record(shop_id, occurred_at);

-- 观测层：所有数据类型归约成的统一形状。
CREATE TABLE IF NOT EXISTS observation (
  id          TEXT PRIMARY KEY,
  shop_id     TEXT NOT NULL,
  record_id   TEXT,
  occurred_at TEXT NOT NULL,
  kind        TEXT NOT NULL,
  subject     TEXT,
  measure     TEXT NOT NULL,
  value_num   REAL,
  value_text  TEXT,
  unit        TEXT,
  confidence  REAL,
  source_span TEXT,
  dedup_key   TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_dedup ON observation(shop_id, dedup_key);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observation(shop_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_obs_kind ON observation(shop_id, kind, measure);
CREATE INDEX IF NOT EXISTS idx_obs_subject ON observation(shop_id, subject);

-- 受控词表：防止「牛肉 / 牛肉类 / 黄牛肉」裂成三个主体，把分析做失真。
CREATE TABLE IF NOT EXISTS dimension (
  id         TEXT PRIMARY KEY,
  shop_id    TEXT NOT NULL,
  kind       TEXT NOT NULL,
  canonical  TEXT NOT NULL,
  aliases    TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(shop_id, kind, canonical)
);
"""


def database_schema_ready(conn) -> bool:
    """只读校验全部运行字段，避免请求时构造 Store 才补列或失败。"""
    required = {
        "meta": "key, value",
        "shop": "id, name, created_at",
        "workspace_meta": "shop_id, owner_id, profile_id, timezone, created_at",
        "entry": "id, shop_id, date, type, category, amount, item, qty, unit, note, "
                 "source, raw_text, batch_id, created_at, period_month, platform, channel, "
                 "amount_basis, external_id, source_system, granularity",
        "sale": "id, shop_id, date, product_id, product_name, qty, amount, source, "
                "batch_id, created_at, platform, channel, product_category, unit, "
                "amount_basis, external_id, source_system, granularity",
        "record": "id, shop_id, raw_text, source, peer, dedup_ref, occurred_at, received_at, "
                  "media_path, media_sha256, parse_status, parser_version, batch_id, "
                  "parse_base_date, context, intent, routing_status, profile_id, ledger_refs, "
                  "correction_of, correction_detail, created_at",
        "agent_request": "principal_id, workspace_id, operation, request_id, fingerprint, "
                         "response, created_at, lease_token",
        "agent_import_preview": "token, principal_id, workspace_id, sha256, plan, "
                                "created_at, consumed_request_id",
        "product": "id, shop_id, name, aliases, price, active, created_at, category",
        "ingredient": "id, shop_id, name, aliases, base_unit, created_at",
        "recipe_line": "id, shop_id, product_id, ingredient_id, qty, units, created_at",
        "recipe_version": "id, shop_id, product_id, effective_date, lines, created_at",
        "discrepancy": "id, shop_id, date, kind, subject, expected, actual, unit, question, "
                       "status, resolution, created_at, resolved_at",
        "import_batch": "id, shop_id, filename, sha256, kind, rows, note, created_at",
        "import_identity": "shop_id, identity, fingerprint, batch_id",
        "observation": "id, shop_id, record_id, occurred_at, kind, subject, measure, value_num, "
                       "value_text, unit, confidence, source_span, dedup_key, created_at",
        "dimension": "id, shop_id, kind, canonical, aliases, created_at",
    }
    try:
        for table, columns in required.items():
            conn.execute(f"SELECT {columns} FROM {table} LIMIT 0")
        return True
    except sqlite3.Error:
        return False


def default_db_path() -> str:
    """默认库位置：项目 data/ 目录，跟代码放一起方便查看和备份。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "suikouji.db")


class Store(ABC):
    """数据出入口。上层只依赖这个接口。"""

    @abstractmethod
    def ensure_shop(self, shop_id: str, name: str = "我的店", *,
                    owner_id: str | None = None, profile_id: str | None = None,
                    timezone: str | None = None) -> dict: ...

    @abstractmethod
    def workspace_get(self, shop_id: str, owner_id: str | None = None) -> dict | None: ...

    @abstractmethod
    def workspace_list(self, owner_id: str | None = None) -> list: ...

    @abstractmethod
    def workspace_set_profile(self, shop_id: str, profile_id: str,
                              owner_id: str | None = None) -> dict: ...

    @abstractmethod
    def add_entries(self, entries: list, source: str = SOURCE_CHAT,
                    batch_id: str | None = None, raw_text: str | None = None,
                    record_id: str | None = None) -> list: ...

    @abstractmethod
    def list_entries(self, shop_id: str, start: str, end: str,
                     type_: str | None = None) -> list: ...

    @abstractmethod
    def get_entry(self, entry_id: str) -> dict | None: ...

    @abstractmethod
    def update_entry(self, entry_id: str, **fields) -> bool: ...

    @abstractmethod
    def delete_entry(self, entry_id: str) -> bool: ...

    @abstractmethod
    def summary(self, shop_id: str, start: str, end: str) -> dict: ...

    @abstractmethod
    def product_upsert(self, shop_id: str, name: str, price: float | None = None,
                       aliases: list | None = None, category: str | None = None) -> dict: ...

    @abstractmethod
    def product_find(self, shop_id: str, name: str) -> dict | None: ...

    @abstractmethod
    def product_list(self, shop_id: str) -> list: ...

    @abstractmethod
    def ingredient_upsert(self, shop_id: str, name: str, base_unit: str | None = None,
                          aliases: list | None = None) -> dict: ...

    @abstractmethod
    def ingredient_find(self, shop_id: str, name: str) -> dict | None: ...

    @abstractmethod
    def ingredient_list(self, shop_id: str) -> list: ...

    @abstractmethod
    def recipe_set(self, shop_id: str, product_id: str, lines: list,
                   units: float = 1, effective_date: str | None = None) -> None: ...

    @abstractmethod
    def recipe_at(self, shop_id: str, product_id: str, as_of: str) -> list: ...

    @abstractmethod
    def recipe_get(self, shop_id: str, product_id: str) -> list: ...

    @abstractmethod
    def ingredient_price(self, shop_id: str, ingredient_id: str,
                         as_of: str | None = None) -> dict | None: ...

    @abstractmethod
    def add_sales(self, shop_id: str, rows: list, source: str = SOURCE_CHAT,
                  batch_id: str | None = None, record_id: str | None = None) -> list: ...

    @abstractmethod
    def sales_by_product(self, shop_id: str, start: str, end: str) -> list: ...

    @abstractmethod
    def discrepancy_add(self, shop_id: str, **fields) -> dict: ...

    @abstractmethod
    def discrepancy_list(self, shop_id: str, status: str = "open") -> list: ...

    @abstractmethod
    def discrepancy_resolve(self, disc_id: str, resolution: str) -> bool: ...

    @abstractmethod
    def batch_add(self, shop_id: str, filename: str, sha256: str, kind: str,
                  rows: int, note: str | None = None) -> dict: ...

    @abstractmethod
    def batch_find_by_hash(self, shop_id: str, sha256: str) -> dict | None: ...

    @abstractmethod
    def batch_set_rows(self, batch_id: str, rows: int) -> None: ...

    @abstractmethod
    def import_identity_get(self, shop_id: str, identity: str) -> dict | None: ...

    @abstractmethod
    def import_identity_add(self, shop_id: str, identity: str,
                            fingerprint: str, batch_id: str) -> None: ...

    @abstractmethod
    def transaction(self): ...

    @abstractmethod
    def list_sales(self, shop_id: str, start: str, end: str) -> list: ...

    @abstractmethod
    def get_sale(self, sale_id: str) -> dict | None: ...

    @abstractmethod
    def update_sale(self, sale_id: str, **fields) -> bool: ...

    # ---------------------------------------------------------- 原话层

    @abstractmethod
    def record_add(self, shop_id: str, raw_text: str, source: str = SOURCE_CHAT,
                   **fields) -> dict: ...

    @abstractmethod
    def record_find(self, shop_id: str, dedup_ref: str) -> dict | None: ...

    @abstractmethod
    def record_link(self, shop_id: str, record_id: str, origin: str) -> None: ...

    @abstractmethod
    def record_for_origin(self, shop_id: str, origin: str) -> dict | None: ...

    @abstractmethod
    def corrections_for(self, shop_id: str, target_id: str) -> list: ...

    @abstractmethod
    def corrections_map(self, shop_id: str) -> dict: ...

    @abstractmethod
    def record_list(self, shop_id: str, start: str, end: str,
                    parse_status: str | None = None, limit: int = 100) -> list: ...

    @abstractmethod
    def record_timeline(self, shop_id: str, *, q: str = "", start: str | None = None,
                        end: str | None = None, parse_status: str | None = None,
                        source: str | None = None, page_size: int = 30,
                        before: tuple[str, str, str] | None = None) -> dict: ...

    @abstractmethod
    def record_detail(self, shop_id: str, record_id: str) -> dict | None: ...

    @abstractmethod
    def record_set_status(self, record_id: str, status: str,
                          parser_version: str | None = None) -> bool: ...

    @abstractmethod
    def record_claim(self, record_id: str) -> bool: ...

    @abstractmethod
    def record_set_routing(self, record_id: str, *, intent: str | None = None,
                           context: str | None = None, profile_id: str | None = None,
                           routing_status: str | None = None) -> bool: ...

    # ---------------------------------------------------------- 观测层

    @abstractmethod
    def observation_add(self, shop_id: str, rows: list) -> list: ...

    @abstractmethod
    def observation_origin(self, shop_id: str, prefix: str) -> list: ...

    @abstractmethod
    def observation_delete_origin(self, shop_id: str, prefix: str) -> int: ...

    @abstractmethod
    def observation_clear(self, shop_id: str, record_id: str) -> int: ...

    @abstractmethod
    def observation_list(self, shop_id: str, start: str, end: str,
                         kind: str | None = None, subject: str | None = None,
                         measure: str | None = None) -> list: ...

    @abstractmethod
    def observation_summary(self, shop_id: str, start: str, end: str) -> list: ...

    @abstractmethod
    def observation_series(self, shop_id: str, start: str, end: str,
                           kind: str, measure: str,
                           subject: str | None = None) -> list: ...

    @abstractmethod
    def observation_kinds(self, shop_id: str) -> list: ...

    # ---------------------------------------------------------- 受控词表

    @abstractmethod
    def dimension_upsert(self, shop_id: str, kind: str, canonical: str,
                         aliases: list | None = None) -> dict: ...

    @abstractmethod
    def dimension_resolve(self, shop_id: str, kind: str, name: str) -> str: ...

    @abstractmethod
    def dimension_list(self, shop_id: str, kind: str | None = None) -> list: ...

    @abstractmethod
    def export_all(self, shop_id: str) -> dict: ...

    @abstractmethod
    def agent_request_get(self, principal_id: str, workspace_id: str,
                          operation: str, request_id: str) -> dict | None: ...

    @abstractmethod
    def agent_request_claim(self, principal_id: str, workspace_id: str,
                            operation: str, request_id: str,
                            fingerprint: str) -> str | None: ...

    @abstractmethod
    def agent_request_finish(self, principal_id: str, workspace_id: str,
                             operation: str, request_id: str,
                             lease_token: str, response: dict) -> bool: ...

    @abstractmethod
    def agent_request_release(self, principal_id: str, workspace_id: str,
                              operation: str, request_id: str,
                              lease_token: str) -> bool: ...

    @abstractmethod
    def agent_import_preview_put(self, token: str, principal_id: str,
                                 workspace_id: str, sha256: str, plan: dict) -> None: ...

    @abstractmethod
    def agent_import_preview_get(self, token: str, principal_id: str,
                                 workspace_id: str) -> dict | None: ...

    @abstractmethod
    def agent_import_preview_consume(self, token: str, principal_id: str,
                                     workspace_id: str, request_id: str) -> bool: ...


class SqliteStore(Store):
    def __init__(self, path: str | None = None):
        self.path = path or default_db_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._transaction_depth = 0
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        existing_meta = self.conn.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone()
        if existing_meta:
            version = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version and int(version[0]) > SCHEMA_VERSION:
                self.close()
                raise ValueError('数据库版本高于当前程序，请使用较新版本')
        self.conn.executescript(_SCHEMA)
        # 增量迁移仅增加字段，旧字段值及原话关联保留。
        additions = {
            'entry': {'period_month': 'TEXT', 'platform': "TEXT NOT NULL DEFAULT ''",
                      'channel': "TEXT NOT NULL DEFAULT ''", 'amount_basis': "TEXT NOT NULL DEFAULT 'gross'",
                      'external_id': 'TEXT', 'source_system': "TEXT NOT NULL DEFAULT ''",
                      'granularity': "TEXT NOT NULL DEFAULT 'daily'"},
            'sale': {'platform': "TEXT NOT NULL DEFAULT ''", 'channel': "TEXT NOT NULL DEFAULT ''",
                     'product_category': "TEXT NOT NULL DEFAULT ''", 'unit': "TEXT NOT NULL DEFAULT ''",
                     'amount_basis': "TEXT NOT NULL DEFAULT 'gross'", 'external_id': 'TEXT',
                     'source_system': "TEXT NOT NULL DEFAULT ''", 'granularity': "TEXT NOT NULL DEFAULT 'daily'"},
            'product': {'category': "TEXT NOT NULL DEFAULT ''"},
            'record': {'context': "TEXT NOT NULL DEFAULT ''",
                       'intent': "TEXT NOT NULL DEFAULT 'unknown'",
                       'routing_status': "TEXT NOT NULL DEFAULT 'unclassified'",
                       'profile_id': "TEXT NOT NULL DEFAULT 'restaurant'"},
            'agent_request': {'lease_token': 'TEXT'},
            'agent_import_preview': {'consumed_request_id': 'TEXT'},
        }
        with self.transaction():
            for table, fields in additions.items():
                columns = {r[1] for r in self.conn.execute(f'PRAGMA table_info({table})')}
                for name, definition in fields.items():
                    if name not in columns:
                        self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS recipe_version (
                id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, product_id TEXT NOT NULL,
                effective_date TEXT NOT NULL, lines TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(shop_id, product_id, effective_date))''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS import_identity (
                shop_id TEXT NOT NULL, identity TEXT NOT NULL, fingerprint TEXT NOT NULL,
                batch_id TEXT NOT NULL, PRIMARY KEY(shop_id, identity))''')
            for shop in self.conn.execute('SELECT id FROM shop').fetchall():
                self.conn.execute(
                    "INSERT OR IGNORE INTO workspace_meta(shop_id, created_at) VALUES(?,?)",
                    (shop[0], now_iso()))
            for p in self.conn.execute('SELECT DISTINCT shop_id, product_id FROM recipe_line').fetchall():
                if not self.conn.execute('SELECT 1 FROM recipe_version WHERE shop_id=? AND product_id=?', tuple(p)).fetchone():
                    lines = self.recipe_get(p[0], p[1])
                    self.conn.execute('INSERT INTO recipe_version VALUES(?,?,?,?,?,?)',
                        (new_id('rv'), p[0], p[1], '0001-01-01', json.dumps(lines), now_iso()))
        # 兼容本轮开发期间已建过的 v2 原型库；只补新表字段，不动账表。
        record_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(record)")}
        for name, definition in (("parse_base_date", "TEXT"),
                                 ("context", "TEXT NOT NULL DEFAULT ''"),
                                 ("intent", "TEXT NOT NULL DEFAULT 'unknown'"),
                                 ("routing_status", "TEXT NOT NULL DEFAULT 'unclassified'"),
                                 ("profile_id", "TEXT NOT NULL DEFAULT 'restaurant'"),
                                 ("ledger_refs", "TEXT NOT NULL DEFAULT '[]'"),
                                 ("correction_of", "TEXT"),
                                 ("correction_detail", "TEXT")):
            if name not in record_columns:
                self.conn.execute(f"ALTER TABLE record ADD COLUMN {name} {definition}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_record_correction"
                          " ON record(shop_id, correction_of)")
        self._commit()
        version = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version and int(version[0]) > SCHEMA_VERSION:
            self.close()
            raise ValueError("数据库版本高于当前程序，请使用较新版本")
        if not version or int(version[0]) < SCHEMA_VERSION:
            from .observe import backfill_records
            with self.transaction():
                shops = self.conn.execute(
                    "SELECT shop_id FROM entry UNION SELECT shop_id FROM sale").fetchall()
                for row in shops:
                    backfill_records(self, row[0])
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ 内部

    def _commit(self):
        if not self._transaction_depth:
            self.conn.commit()

    @contextmanager
    def transaction(self):
        """多表写入一起提交；嵌套调用用 savepoint 保证失败不留下半条数据。"""
        name = f"layer_{self._transaction_depth}"
        self.conn.execute(f"SAVEPOINT {name}")
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            try:
                self.conn.execute(f"ROLLBACK TO {name}")
            finally:
                self._transaction_depth -= 1
                try:
                    self.conn.execute(f"RELEASE {name}")
                except sqlite3.OperationalError:
                    # 外层锁在回滚点释放时才可能解除；若 RELEASE 也遇到锁，
                    # 必须回滚整条连接，不能把失败写入留到下一次无关提交。
                    self.conn.rollback()
            raise
        else:
            self._transaction_depth -= 1
            try:
                self.conn.execute(f"RELEASE {name}")
            except BaseException:
                # A busy/disk failure at the outer RELEASE leaves SQLite's
                # transaction open. Never let the next request commit it.
                if self._transaction_depth == 0:
                    self.conn.rollback()
                else:
                    self.conn.execute(f"ROLLBACK TO {name}")
                    self.conn.execute(f"RELEASE {name}")
                raise

    @staticmethod
    def _row(row) -> dict | None:
        return dict(row) if row is not None else None

    def _rows(self, cur) -> list:
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------ 店铺

    def ensure_shop(self, shop_id: str = SHOP_DEFAULT, name: str = "我的店", *,
                    owner_id: str | None = None, profile_id: str | None = None,
                    timezone: str | None = None) -> dict:
        from .profile import get_profile
        with self.transaction():
            cur = self.conn.execute("SELECT * FROM shop WHERE id = ?", (shop_id,))
            found = self._row(cur.fetchone())
            if not found:
                self.conn.execute(
                    "INSERT INTO shop(id, name, created_at) VALUES(?,?,?)",
                    (shop_id, name, now_iso()),
                )
                found = {"id": shop_id, "name": name}
            meta = self._row(self.conn.execute(
                "SELECT * FROM workspace_meta WHERE shop_id=?", (shop_id,)).fetchone())
            if meta:
                if owner_id is not None and meta["owner_id"] != owner_id:
                    raise ValueError("工作空间不属于当前用户")
                if profile_id is not None:
                    get_profile(profile_id)
                    self.conn.execute("UPDATE workspace_meta SET profile_id=? WHERE shop_id=?", (profile_id, shop_id))
                    meta["profile_id"] = profile_id
                if timezone is not None:
                    self.conn.execute("UPDATE workspace_meta SET timezone=? WHERE shop_id=?", (timezone, shop_id))
                    meta["timezone"] = timezone
            else:
                profile_id = profile_id or "restaurant"
                get_profile(profile_id)
                meta = {"shop_id": shop_id, "owner_id": owner_id or "local",
                        "profile_id": profile_id, "timezone": timezone or "local",
                        "created_at": now_iso()}
                self.conn.execute(
                    "INSERT INTO workspace_meta(shop_id, owner_id, profile_id, timezone, created_at) VALUES(?,?,?,?,?)",
                    tuple(meta.values()),
                )
            workspace_meta = {**meta, "workspace_created_at": meta.pop("created_at")}
            return {**found, **workspace_meta}

    def workspace_get(self, shop_id, owner_id=None):
        sql = "SELECT s.*, m.owner_id, m.profile_id, m.timezone, m.created_at AS workspace_created_at FROM shop s JOIN workspace_meta m ON m.shop_id=s.id WHERE s.id=?"
        args = [shop_id]
        if owner_id is not None:
            sql += " AND m.owner_id=?"
            args.append(owner_id)
        return self._row(self.conn.execute(sql, args).fetchone())

    def workspace_list(self, owner_id=None):
        sql = "SELECT s.*, m.owner_id, m.profile_id, m.timezone, m.created_at AS workspace_created_at FROM shop s JOIN workspace_meta m ON m.shop_id=s.id"
        args = []
        if owner_id is not None:
            sql += " WHERE m.owner_id=?"
            args.append(owner_id)
        return self._rows(self.conn.execute(sql + " ORDER BY s.id", args))

    def workspace_set_profile(self, shop_id, profile_id, owner_id=None):
        from .profile import get_profile
        get_profile(profile_id)
        workspace = self.workspace_get(shop_id, owner_id)
        if not workspace:
            raise ValueError("工作空间不存在")
        self.conn.execute("UPDATE workspace_meta SET profile_id=? WHERE shop_id=?", (profile_id, shop_id))
        self._commit()
        workspace["profile_id"] = profile_id
        return workspace

    # ------------------------------------------------------------ 流水

    def add_entries(self, entries, source=SOURCE_CHAT, batch_id=None, raw_text=None,
                    record_id=None):
        from .observe import project_ledger_row
        ids = []
        with self.transaction():
            for e in entries:
                eid = e.get("id") or new_id("e")
                self.conn.execute(
                    """INSERT INTO entry(id, shop_id, date, type, category, amount, item,
                                         qty, unit, note, source, raw_text, batch_id, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, e.get("shop_id", SHOP_DEFAULT), e["date"], e["type"], e["category"],
                     float(e["amount"]), e.get("item"), e.get("qty"), e.get("unit") or "",
                     e.get("note"), e.get("source") or source,
                     raw_text if raw_text is not None else e.get("raw_text"), batch_id, now_iso()))
                period = e.get('period_month')
                if period:
                    from .model import month_range
                    if len(period) != 7:
                        raise ValueError('费用归属月格式为 YYYY-MM')
                    month_range(period)
                basis = e.get('amount_basis') or 'gross'
                if basis not in ('gross', 'settlement'):
                    raise ValueError('金额口径必须是 gross 或 settlement')
                if not math.isfinite(float(e['amount'])) or float(e['amount']) < 0:
                    raise ValueError('金额必须是非负有限数')
                granularity = e.get('granularity') or ('transaction' if e.get('external_id') else 'daily')
                if granularity not in ('daily', 'transaction'):
                    raise ValueError('流水粒度必须是 daily 或 transaction')
                self.conn.execute('''UPDATE entry SET period_month=?, platform=?, channel=?,
                    amount_basis=?, external_id=?, source_system=?, granularity=? WHERE id=?''',
                    (period, e.get('platform') or '', e.get('channel') or '', basis,
                     e.get('external_id'), e.get('source_system') or '', granularity, eid))
                row = self.get_entry(eid)
                project_ledger_row(self, row["shop_id"], row, "entry", record_id)
                ids.append(eid)
        return ids

    def list_entries(self, shop_id, start, end, type_=None):
        sql = ("SELECT * FROM entry WHERE shop_id=? AND date>=? AND date<=?")
        args = [shop_id, start, end]
        if type_:
            sql += " AND type=?"
            args.append(type_)
        sql += " ORDER BY date, created_at"
        return self._rows(self.conn.execute(sql, args))

    def get_entry(self, entry_id):
        return self._row(self.conn.execute(
            "SELECT * FROM entry WHERE id=?", (entry_id,)).fetchone())

    def update_entry(self, entry_id, **fields):
        allowed = {'date', 'type', 'category', 'amount', 'item', 'qty', 'unit', 'note',
                   'period_month', 'platform', 'channel', 'amount_basis', 'granularity'}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        cols = ", ".join(f"{k}=?" for k in sets)
        from .observe import project_ledger_row
        with self.transaction():
            cur = self.conn.execute(
                f"UPDATE entry SET {cols} WHERE id=?", list(sets.values()) + [entry_id])
            row = self.get_entry(entry_id)
            if row:
                project_ledger_row(self, row["shop_id"], row, "entry")
        return cur.rowcount > 0

    def delete_entry(self, entry_id):
        with self.transaction():
            row = self.get_entry(entry_id)
            if not row:
                return False
            self.observation_delete_origin(row["shop_id"], f"entry:{entry_id}:")
            self.conn.execute("DELETE FROM entry WHERE id=?", (entry_id,))
        return True

    # ------------------------------------------------------------ 汇总

    def summary(self, shop_id, start, end):
        from .accounting import summarize
        return summarize(self, shop_id, start, end)

    # ------------------------------------------------------------ 商品

    def product_upsert(self, shop_id, name, price=None, aliases=None, category=None):
        existing = self.product_find(shop_id, name)
        if existing:
            if price is not None:
                self.conn.execute("UPDATE product SET price=? WHERE id=?",
                                  (float(price), existing["id"]))
                existing["price"] = float(price)
            if category is not None:
                self.conn.execute('UPDATE product SET category=? WHERE id=?', (category, existing['id']))
                existing['category'] = category
            if aliases:
                merged = sorted(set(existing['aliases']) | set(aliases))
                self.conn.execute("UPDATE product SET aliases=? WHERE id=?",
                                  (json.dumps(merged, ensure_ascii=False), existing["id"]))
                existing["aliases"] = merged
            self._commit()
            return existing
        pid = new_id("p")
        self.conn.execute(
            "INSERT INTO product(id, shop_id, name, aliases, price, created_at) VALUES(?,?,?,?,?,?)",
            (pid, shop_id, name, json.dumps(aliases or [], ensure_ascii=False),
             float(price) if price is not None else None, now_iso()),
        )
        self.conn.execute('UPDATE product SET category=? WHERE id=?', (category or '', pid))
        self._commit()
        return {"id": pid, "shop_id": shop_id, "name": name,
                "aliases": aliases or [], "price": price, "active": 1, 'category': category or ''}

    def product_find(self, shop_id, name):
        if not name:
            return None
        target = str(name).strip()
        for row in self._rows(self.conn.execute(
                "SELECT * FROM product WHERE shop_id=? AND active=1", (shop_id,))):
            if row["name"] == target:
                row["aliases"] = json.loads(row["aliases"] or "[]")
                return row
        for row in self._rows(self.conn.execute(
                "SELECT * FROM product WHERE shop_id=? AND active=1", (shop_id,))):
            aliases = json.loads(row["aliases"] or "[]")
            if target in aliases:
                row["aliases"] = aliases
                return row
        return None

    def product_list(self, shop_id):
        rows = self._rows(self.conn.execute(
            "SELECT * FROM product WHERE shop_id=? AND active=1 ORDER BY name", (shop_id,)))
        for r in rows:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        return rows

    # ------------------------------------------------------------ 原料

    def ingredient_upsert(self, shop_id, name, base_unit=None, aliases=None):
        existing = self.ingredient_find(shop_id, name)
        if existing:
            if base_unit and not existing.get("base_unit"):
                self.conn.execute("UPDATE ingredient SET base_unit=? WHERE id=?",
                                  (base_unit, existing["id"]))
                existing["base_unit"] = base_unit
            self._commit()
            return existing
        iid = new_id("i")
        self.conn.execute(
            "INSERT INTO ingredient(id, shop_id, name, aliases, base_unit, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (iid, shop_id, name, json.dumps(aliases or [], ensure_ascii=False),
             base_unit, now_iso()),
        )
        self._commit()
        return {"id": iid, "shop_id": shop_id, "name": name,
                "aliases": aliases or [], "base_unit": base_unit}

    def ingredient_find(self, shop_id, name):
        if not name:
            return None
        target = str(name).strip()
        rows = self._rows(self.conn.execute(
            "SELECT * FROM ingredient WHERE shop_id=?", (shop_id,)))
        for row in rows:
            if row["name"] == target:
                row["aliases"] = json.loads(row["aliases"] or "[]")
                return row
        for row in rows:
            aliases = json.loads(row["aliases"] or "[]")
            if target in aliases:
                row["aliases"] = aliases
                return row
        return None

    def ingredient_list(self, shop_id):
        rows = self._rows(self.conn.execute(
            "SELECT * FROM ingredient WHERE shop_id=? ORDER BY name", (shop_id,)))
        for r in rows:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        return rows

    # ------------------------------------------------------------ 配方

    def recipe_set(self, shop_id, product_id, lines, units=1, effective_date=None):
        from .model import today_iso, parse_date
        effective = effective_date or today_iso()
        if parse_date(effective) != effective or not math.isfinite(float(units)) or units <= 0:
            raise ValueError('配方生效日期或份数无效')
        if not self.conn.execute('SELECT 1 FROM product WHERE shop_id=? AND id=?', (shop_id, product_id)).fetchone():
            raise ValueError('商品不属于当前店铺')
        with self.transaction():
            version = []
            for line in lines:
                if not self.conn.execute('SELECT 1 FROM ingredient WHERE shop_id=? AND id=?',
                                         (shop_id, line['ingredient_id'])).fetchone():
                    raise ValueError('原料不属于当前店铺')
                version.append({**line, 'units': float(units)})
            self.conn.execute('''INSERT INTO recipe_version VALUES(?,?,?,?,?,?)
                ON CONFLICT(shop_id, product_id, effective_date) DO UPDATE SET lines=excluded.lines''',
                (new_id('rv'), shop_id, product_id, effective, json.dumps(version), now_iso()))
            current = self.recipe_at(shop_id, product_id, today_iso())
            self.conn.execute('DELETE FROM recipe_line WHERE shop_id=? AND product_id=?', (shop_id, product_id))
            for line in current:
                self.conn.execute('INSERT INTO recipe_line VALUES(?,?,?,?,?,?,?)',
                    (new_id('r'), shop_id, product_id, line['ingredient_id'], line['qty'], line['units'], now_iso()))

    def recipe_at(self, shop_id, product_id, as_of):
        row = self.conn.execute('''SELECT lines FROM recipe_version
            WHERE shop_id=? AND product_id=? AND effective_date<=?
            ORDER BY effective_date DESC LIMIT 1''', (shop_id, product_id, as_of)).fetchone()
        return json.loads(row[0]) if row else []

    def recipe_get(self, shop_id, product_id):
        # rowid 兜底：老数据的时间戳可能只到秒，光靠 created_at 排不出稳定顺序
        return self._rows(self.conn.execute(
            "SELECT * FROM recipe_line WHERE shop_id=? AND product_id=?"
            " ORDER BY created_at, rowid",
            (shop_id, product_id)))

    def ingredient_price(self, shop_id, ingredient_id, as_of=None):
        """原料单价 = 最近一次进货的 金额 ÷ 数量。

        只认带数量的进货记录；没有数量的进货无法折算单价，跳过。
        """
        sql = ("SELECT * FROM entry WHERE shop_id=? AND type=? AND item IS NOT NULL"
               " AND qty IS NOT NULL AND qty > 0")
        args = [shop_id, TYPE_PURCHASE]
        if as_of:
            sql += " AND date<=?"
            args.append(as_of)
        sql += " ORDER BY date DESC, created_at DESC"
        ing = None
        for r in self.ingredient_list(shop_id):
            if r["id"] == ingredient_id:
                ing = r
                break
        if not ing:
            return None
        names = {ing["name"]} | set(ing.get("aliases") or [])
        for row in self._rows(self.conn.execute(sql, args)):
            item = str(row.get("item") or "")
            if item.strip() in names:
                unit = row.get("unit") or ""
                return {
                    # 单价不能按「分」截断：24.26 元/斤 再折成每克就只剩两位有效数字，
                    # 到了配方行会把 0.0485 元/克 显示成 0.05。留给展示层去定精度。
                    "unit_price": round(float(row["amount"]) / float(row["qty"]), 6),
                    "unit": unit,
                    "date": row["date"],
                    "entry_id": row["id"],
                }
        return None

    # ------------------------------------------------------------ 销量

    def add_sales(self, shop_id, rows, source=SOURCE_CHAT, batch_id=None, record_id=None):
        from .observe import project_ledger_row
        ids = []
        with self.transaction():
            for r in rows:
                r = dict(r)
                product = self.product_find(shop_id, r['product_name'])
                if product:
                    r.update(product_id=product['id'], product_name=product['name'])
                elif r.get('product_id'):
                    raise ValueError('商品ID与当前店铺商品名称不一致')
                sid, created = new_id("s"), now_iso()
                self.conn.execute(
                    """INSERT INTO sale(id, shop_id, date, product_id, product_name, qty,
                                        amount, source, batch_id, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (sid, shop_id, r["date"], r.get("product_id"), r["product_name"],
                     float(r["qty"]), r.get("amount"), r.get("source") or source,
                     batch_id, created))
                product = self.product_find(shop_id, r['product_name'])
                basis = r.get('amount_basis') or 'gross'
                granularity = r.get('granularity') or 'daily'
                if basis not in ('gross', 'settlement') or granularity not in ('daily', 'transaction'):
                    raise ValueError('销售金额口径或数据粒度无效')
                if not math.isfinite(float(r['qty'])) or float(r['qty']) < 0:
                    raise ValueError('销量必须是非负有限数，退款需独立处理')
                if r.get('amount') is not None and (not math.isfinite(float(r['amount'])) or float(r['amount']) < 0):
                    raise ValueError('销售金额必须是非负有限数')
                self.conn.execute('''UPDATE sale SET platform=?, channel=?, product_category=?, unit=?,
                    amount_basis=?, external_id=?, source_system=?, granularity=? WHERE id=?''',
                    (r.get('platform') or '', r.get('channel') or '',
                     r.get('product_category') or (product or {}).get('category', ''), r.get('unit') or '',
                     basis, r.get('external_id'), r.get('source_system') or '', granularity, sid))
                row = dict(self.conn.execute('SELECT * FROM sale WHERE id=?', (sid,)).fetchone())
                row['raw_text'] = r.get('raw_text')
                project_ledger_row(self, shop_id, row, "sale", record_id)
                ids.append(sid)
        return ids

    def get_sale(self, sale_id):
        return self._row(self.conn.execute('SELECT * FROM sale WHERE id=?', (sale_id,)).fetchone())

    def update_sale(self, sale_id, **fields):
        allowed = {'date', 'product_name', 'product_id', 'qty', 'amount', 'platform',
                   'channel', 'product_category', 'unit', 'amount_basis', 'granularity'}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        from .observe import project_ledger_row
        with self.transaction():
            row = self.get_sale(sale_id)
            if not row:
                return False
            if 'product_name' in sets:
                product = self.product_find(row['shop_id'], sets['product_name'])
                if product:
                    sets.update(product_id=product['id'], product_name=product['name'],
                                product_category=sets.get('product_category') or product.get('category', ''))
                else:
                    # 改成商品库外的名字：不能继续挂着旧商品的ID，否则会按旧配方算这行的成本。
                    sets.update(product_id=None, product_name=str(sets['product_name']).strip())
            cols = ', '.join(f'{k}=?' for k in sets)
            self.conn.execute(f'UPDATE sale SET {cols} WHERE id=?', list(sets.values()) + [sale_id])
            updated = self.get_sale(sale_id)
            project_ledger_row(self, updated['shop_id'], updated, 'sale')
        return True

    def list_sales(self, shop_id, start, end):
        return self._rows(self.conn.execute(
            "SELECT * FROM sale WHERE shop_id=? AND date>=? AND date<=?"
            " ORDER BY date, created_at, rowid", (shop_id, start, end)))

    def sales_by_product(self, shop_id, start, end):
        from .accounting import selected_sales
        rows, _ = selected_sales(self.list_sales(shop_id, start, end))
        groups = {}
        for row in rows:
            group = groups.setdefault(row['product_name'], {'product_id': row['product_id'],
                'product_name': row['product_name'], 'qty': 0, 'amount': 0, 'missing_amount': False})
            group['qty'] += row['qty']
            group['amount'] += row['amount'] or 0
            group['missing_amount'] |= row['amount'] is None
        for group in groups.values():
            if group['missing_amount']:
                group['amount'] = None
        return sorted(groups.values(), key=lambda r: -r['qty'])

    # ------------------------------------------------------------ 差异

    def discrepancy_add(self, shop_id, **fields):
        did = new_id("d")
        self.conn.execute(
            """INSERT INTO discrepancy(id, shop_id, date, kind, subject, expected, actual,
                                       unit, question, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,'open',?)""",
            (did, shop_id, fields["date"], fields["kind"], fields["subject"],
             fields.get("expected"), fields.get("actual"), fields.get("unit"),
             fields["question"], now_iso()),
        )
        self._commit()
        return {"id": did, **fields, "status": "open"}

    def discrepancy_list(self, shop_id, status="open"):
        if status == "all":
            return self._rows(self.conn.execute(
                "SELECT * FROM discrepancy WHERE shop_id=? ORDER BY date, created_at",
                (shop_id,)))
        return self._rows(self.conn.execute(
            "SELECT * FROM discrepancy WHERE shop_id=? AND status=? ORDER BY date, created_at",
            (shop_id, status)))

    def discrepancy_resolve(self, disc_id, resolution):
        cur = self.conn.execute(
            "UPDATE discrepancy SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
            (resolution, now_iso(), disc_id))
        self._commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 导入批次

    def batch_add(self, shop_id, filename, sha256, kind, rows, note=None):
        bid = new_id("b")
        self.conn.execute(
            """INSERT INTO import_batch(id, shop_id, filename, sha256, kind, rows, note, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (bid, shop_id, filename, sha256, kind, int(rows), note, now_iso()),
        )
        self._commit()
        return {"id": bid, "filename": filename, "sha256": sha256,
                "kind": kind, "rows": rows}

    def batch_find_by_hash(self, shop_id, sha256):
        return self._row(self.conn.execute(
            "SELECT * FROM import_batch WHERE shop_id=? AND sha256=? ORDER BY created_at DESC",
            (shop_id, sha256)).fetchone())

    def batch_set_rows(self, batch_id, rows):
        self.conn.execute("UPDATE import_batch SET rows=? WHERE id=?", (int(rows), batch_id))

    def import_identity_get(self, shop_id, identity):
        row = self.conn.execute(
            "SELECT fingerprint, batch_id FROM import_identity WHERE shop_id=? AND identity=?",
            (shop_id, identity),
        ).fetchone()
        return dict(row) if row else None

    def import_identity_add(self, shop_id, identity, fingerprint, batch_id):
        self.conn.execute(
            "INSERT INTO import_identity(shop_id, identity, fingerprint, batch_id) VALUES(?,?,?,?)",
            (shop_id, identity, fingerprint, batch_id),
        )

    # ------------------------------------------------------------ 原话层

    def record_add(self, shop_id, raw_text, source=SOURCE_CHAT, **fields):
        """记一条原话。同一 dedup_ref 已经存在时返回原有那条，不重复写。

        dedup_ref 形如 msg:<消息id> / entry:<流水id> / media:<sha256>。
        去重必须在最靠近入口的地方做 —— 重复入库会直接污染求和，
        这一条 flomo 的 API 是没有的，我们得自己保证。
        """
        from .profile import get_profile
        workspace = self.workspace_get(shop_id)
        profile_id = fields.get("profile_id") or (workspace or {}).get("profile_id", "restaurant")
        get_profile(profile_id)
        dedup_ref = fields.get("dedup_ref")
        if dedup_ref:
            existing = self.record_find(shop_id, dedup_ref)
            if existing:
                return {**existing, "duplicate": True}

        rid = new_id("rec")
        now = now_iso()
        try:
            self.conn.execute(
                """INSERT INTO record(id, shop_id, raw_text, source, peer, dedup_ref,
                                      occurred_at, received_at, media_path, media_sha256,
                                      parse_status, parser_version, batch_id, parse_base_date,
                                      context, intent, routing_status, profile_id,
                                      correction_of, correction_detail, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, shop_id, raw_text, source, fields.get("peer"), dedup_ref,
                 fields.get("occurred_at"), fields.get("received_at") or now,
                 fields.get("media_path"), fields.get("media_sha256"),
                 fields.get("parse_status") or "pending", fields.get("parser_version"),
                 fields.get("batch_id"), fields.get("parse_base_date") or
                 str(fields.get("received_at") or now)[:10],
                 fields.get("context") or "", fields.get("intent") or "unknown",
                 fields.get("routing_status") or "unclassified", profile_id,
                 fields.get("correction_of"), fields.get("correction_detail"), now),
            )
        except sqlite3.IntegrityError:
            # record_find→INSERT 之间可能被另一个连接抢先写入；唯一键命中时，
            # 按顺序重试一样返回 duplicate，不能把并发正常路径变成 500/锁住事务。
            if dedup_ref:
                existing = self.record_find(shop_id, dedup_ref)
                if existing:
                    return {**existing, "duplicate": True}
            raise
        self._commit()
        if dedup_ref:
            row = self.record_find(shop_id, dedup_ref)
        else:
            row = self._row(self.conn.execute("SELECT * FROM record WHERE id=?",
                                              (rid,)).fetchone())
        return {**(row or {}), "duplicate": False}

    def record_find(self, shop_id, dedup_ref):
        return self._row(self.conn.execute(
            "SELECT * FROM record WHERE shop_id=? AND dedup_ref=?",
            (shop_id, dedup_ref)).fetchone())

    def record_link(self, shop_id, record_id, origin):
        row = self._row(self.conn.execute(
            "SELECT * FROM record WHERE shop_id=? AND id=?", (shop_id, record_id)).fetchone())
        if not row:
            raise ValueError("账目必须引用本店原话")
        refs = json.loads(row["ledger_refs"])
        if origin not in refs:
            refs.append(origin)
            self.conn.execute("UPDATE record SET ledger_refs=? WHERE id=?",
                              (json.dumps(refs), record_id))
            self._commit()

    def record_for_origin(self, shop_id, origin):
        for row in self._rows(self.conn.execute("SELECT * FROM record WHERE shop_id=?", (shop_id,))):
            if origin in json.loads(row["ledger_refs"]):
                return row
        return None

    def corrections_for(self, shop_id, target_id):
        """这行账目上改过的所有原话，按时间正序；原始原话不在其中。"""
        return self._rows(self.conn.execute(
            "SELECT * FROM record WHERE shop_id=? AND correction_of=?"
            " ORDER BY created_at, rowid", (shop_id, target_id)))

    def corrections_map(self, shop_id):
        """被改过的账目行 → 改过它的原话，按时间正序。

        原话列表只取最近 100 条，界面上的「改过几次」不能靠它算。
        """
        out = {}
        for row in self._rows(self.conn.execute(
                "SELECT * FROM record WHERE shop_id=? AND correction_of IS NOT NULL"
                " ORDER BY created_at, rowid", (shop_id,))):
            out.setdefault(row['correction_of'], []).append(row)
        return out

    def record_claim(self, record_id):
        """把一条 pending 原话认领过来续跑；认领不到说明别的写者正在处理。

        认领必须放在同一个事务里：提交前别的连接看不见 'parsing'，
        而写锁会把它挡到这边提交之后，那时状态已经不是 pending，认领自然失败。
        事务回滚也把认领一起撤回，不会留下永远续不了的 'parsing'。
        """
        cur = self.conn.execute(
            "UPDATE record SET parse_status='parsing' WHERE id=? AND parse_status='pending'",
            (record_id,))
        return cur.rowcount == 1

    def record_list(self, shop_id, start, end, parse_status=None, limit=100):
        sql = ("SELECT * FROM record WHERE shop_id=?"
               " AND substr(COALESCE(occurred_at, received_at),1,10)>=?"
               " AND substr(COALESCE(occurred_at, received_at),1,10)<=?")
        args = [shop_id, start, end]
        if parse_status:
            sql += " AND parse_status=?"
            args.append(parse_status)
        sql += " ORDER BY occurred_at, created_at, rowid LIMIT ?"
        args.append(int(limit))
        return self._rows(self.conn.execute(sql, args))

    @staticmethod
    def _safe_json_list(value):
        try:
            decoded = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return [], 1
        if not isinstance(decoded, list):
            return [], 1
        return decoded, 0

    @staticmethod
    def _event_key(record):
        event_time = record.get("occurred_at") or record.get("received_at") or ""
        return (str(event_time), str(record.get("created_at") or ""),
                str(record.get("id") or ""))

    def _linked_rows(self, shop_id, record):
        refs, malformed = self._safe_json_list(record.get("ledger_refs"))
        entry_ids, sale_ids = [], []
        for ref in refs:
            if not isinstance(ref, str):
                malformed += 1
            elif ref.startswith("entry:") and ref[6:]:
                entry_ids.append(ref[6:])
            elif ref.startswith("sale:") and ref[5:]:
                sale_ids.append(ref[5:])
            else:
                malformed += 1

        def fetch(table, ids):
            if not ids:
                return [], 0
            unique = list(dict.fromkeys(ids))
            marks = ",".join("?" for _ in unique)
            rows = self._rows(self.conn.execute(
                f"SELECT * FROM {table} WHERE shop_id=? AND id IN ({marks})",
                [shop_id, *unique]))
            found = {row["id"] for row in rows}
            return rows, len(set(unique) - found)

        entries, missing_entries = fetch("entry", entry_ids)
        sales, missing_sales = fetch("sale", sale_ids)
        entries.sort(key=lambda row: (row.get("created_at") or "", row["id"]))
        sales.sort(key=lambda row: (row.get("created_at") or "", row["id"]))
        return entries, sales, malformed + missing_entries + missing_sales

    @staticmethod
    def _search_values(record, entries, sales, observations):
        values = [record.get("raw_text"), record.get("source"), record.get("peer"),
                  record.get("correction_detail")]
        for row in entries:
            values.extend(row.get(name) for name in
                          ("item", "category", "platform", "channel", "unit", "note", "source"))
        for row in sales:
            values.extend(row.get(name) for name in
                          ("product_name", "product_category", "platform", "channel", "unit", "source"))
        for row in observations:
            values.extend(row.get(name) for name in
                          ("kind", "subject", "measure", "unit", "value_text"))
        return " ".join(str(value) for value in values if value not in (None, ""))

    def _timeline_item(self, shop_id, record):
        entries, sales, unresolved = self._linked_rows(shop_id, record)
        observations = self._rows(self.conn.execute(
            "SELECT * FROM observation WHERE shop_id=? AND record_id=?"
            " ORDER BY occurred_at, created_at, id", (shop_id, record["id"])))
        target_ids = [record.get("correction_of")]
        target_ids.extend(f"entry:{row['id']}" for row in entries)
        target_ids.extend(f"sale:{row['id']}" for row in sales)
        correction_count = sum(len(self.corrections_for(shop_id, target))
                               for target in dict.fromkeys(x for x in target_ids if x))
        event_time = self._event_key(record)[0]
        return {
            "record": record,
            "event_time": event_time,
            "projection_counts": {"entries": len(entries), "sales": len(sales),
                                   "observations": len(observations)},
            "correction_count": correction_count,
            "ledger_preview": [
                {"kind": "entry", "id": row["id"], "date": row.get("date"),
                 "summary": row.get("item") or row.get("category"),
                 "amount": row.get("amount")} for row in entries
            ] + [
                {"kind": "sale", "id": row["id"], "date": row.get("date"),
                 "summary": row.get("product_name"), "amount": row.get("amount")}
                for row in sales
            ],
            "warnings": (["关联账目有 %d 个未解析引用" % unresolved]
                          if unresolved else []),
        }

    def record_timeline(self, shop_id, *, q="", start=None, end=None,
                        parse_status=None, source=None, page_size=30, before=None):
        """返回只读原话收件箱；不改变旧 record_list 的日期/顺序契约。"""
        rows = self._rows(self.conn.execute(
            "SELECT * FROM record WHERE shop_id=?", (shop_id,)))
        q_fold = str(q or "").casefold()
        matched = []
        for record in rows:
            event_time = self._event_key(record)[0]
            event_date = event_time[:10]
            if start and event_date < start:
                continue
            if end and event_date > end:
                continue
            if parse_status and record.get("parse_status") != parse_status:
                continue
            if source and record.get("source") != source:
                continue
            item = self._timeline_item(shop_id, record)
            if q_fold:
                entries, sales, _ = self._linked_rows(shop_id, record)
                observations = self._rows(self.conn.execute(
                    "SELECT * FROM observation WHERE shop_id=? AND record_id=?",
                    (shop_id, record["id"])))
                if q_fold not in self._search_values(record, entries, sales, observations).casefold():
                    continue
            if before is not None and self._event_key(record) >= tuple(str(x) for x in before):
                continue
            item["record"] = record
            matched.append(item)

        matched.sort(key=lambda item: self._event_key(item["record"]), reverse=True)
        page = matched[:int(page_size)]
        return {"items": page, "has_more": len(matched) > len(page),
                "next_before": self._event_key(page[-1]["record"]) if len(matched) > len(page) else None}

    def record_detail(self, shop_id, record_id):
        """返回一条原话及其同店投影；损坏引用只产生 warning，不跨店读取。"""
        record = self._row(self.conn.execute(
            "SELECT * FROM record WHERE shop_id=? AND id=?", (shop_id, record_id)).fetchone())
        if not record:
            return None
        entries, sales, unresolved = self._linked_rows(shop_id, record)
        observations = self._rows(self.conn.execute(
            "SELECT * FROM observation WHERE shop_id=? AND record_id=?"
            " ORDER BY occurred_at, created_at, id", (shop_id, record_id)))
        target_id = record.get("correction_of")
        correction_target = None
        warnings = []
        if unresolved:
            warnings.append("存在 %d 个无法安全解析的关联引用" % unresolved)
        if target_id:
            prefix, _, target_row_id = str(target_id).partition(":")
            if prefix not in ("entry", "sale") or not target_row_id:
                warnings.append("修正目标引用格式不完整")
            else:
                target = self._row(self.conn.execute(
                    f"SELECT * FROM {prefix} WHERE shop_id=? AND id=?",
                    (shop_id, target_row_id)).fetchone())
                if target:
                    correction_target = {"kind": prefix, "row": target}
                else:
                    warnings.append("修正目标在当前账本中不可用")

        target_ids = [target_id]
        target_ids.extend(f"entry:{row['id']}" for row in entries)
        target_ids.extend(f"sale:{row['id']}" for row in sales)
        corrections = []
        seen = set()
        for target in (x for x in target_ids if x):
            for correction in self.corrections_for(shop_id, target):
                if correction["id"] not in seen:
                    seen.add(correction["id"])
                    corrections.append(correction)
        corrections.sort(key=lambda row: (row.get("created_at") or "", row["id"]))
        return {"record": record, "entries": entries, "sales": sales,
                "observations": observations, "corrections": corrections,
                "correction_target": correction_target, "warnings": warnings}

    def record_set_status(self, record_id, status, parser_version=None):
        cur = self.conn.execute(
            "UPDATE record SET parse_status=?, parser_version=COALESCE(?, parser_version)"
            " WHERE id=?", (status, parser_version, record_id))
        self._commit()
        return cur.rowcount > 0

    def record_set_routing(self, record_id, *, intent=None, context=None,
                           profile_id=None, routing_status=None):
        if profile_id is not None:
            from .profile import get_profile
            get_profile(profile_id)
        fields = {
            "intent": intent,
            "context": context,
            "profile_id": profile_id,
            "routing_status": routing_status,
        }
        sets = {name: value for name, value in fields.items() if value is not None}
        if not sets:
            return False
        sql = ", ".join(f"{name}=?" for name in sets)
        cur = self.conn.execute(f"UPDATE record SET {sql} WHERE id=?",
                                [*sets.values(), record_id])
        self._commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ 观测层

    def observation_add(self, shop_id, rows):
        """仅去重键冲突可跳过；无来源、非法数值不能悄悄写入。"""
        ids = []
        with self.transaction():
            for r in rows:
                record = self.conn.execute(
                    "SELECT id FROM record WHERE id=? AND shop_id=?",
                    (r.get("record_id"), shop_id)).fetchone()
                if not record:
                    raise ValueError("观测必须引用本店原话")
                num, text = r.get("value_num"), r.get("value_text")
                if (num is None) == (text is None):
                    raise ValueError("观测必须且只能有一个数值或文本取值")
                if num is not None and (isinstance(num, bool) or not math.isfinite(float(num))):
                    raise ValueError("观测数值必须有限")
                oid = new_id("o")
                cur = self.conn.execute(
                    """INSERT INTO observation(
                           id, shop_id, record_id, occurred_at, kind, subject, measure,
                           value_num, value_text, unit, confidence, source_span,
                           dedup_key, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(shop_id, dedup_key) DO NOTHING""",
                    (oid, shop_id, r["record_id"], r["occurred_at"], r["kind"],
                     r.get("subject"), r["measure"], num, text, r.get("unit") or "",
                     r.get("confidence"), r.get("source_span"), r["dedup_key"], now_iso()))
                if cur.rowcount:
                    ids.append(oid)
        return ids

    def observation_origin(self, shop_id, prefix):
        return self._rows(self.conn.execute(
            "SELECT * FROM observation WHERE shop_id=? AND substr(dedup_key,1,?)=?",
            (shop_id, len(prefix), prefix)))

    def observation_delete_origin(self, shop_id, prefix):
        cur = self.conn.execute(
            "DELETE FROM observation WHERE shop_id=? AND substr(dedup_key,1,?)=?",
            (shop_id, len(prefix), prefix))
        self._commit()
        return cur.rowcount

    def observation_clear(self, shop_id, record_id):
        """删掉一条原话的全部观测，用于换了解析器之后重新投影。

        dedup_key 只能挡住「一字不差地重跑一遍」；解析器改了、结果跟着变了，
        就得先清后写，否则同一句原话会留下新旧两份观测，求和直接翻倍。
        """
        cur = self.conn.execute(
            "DELETE FROM observation WHERE shop_id=? AND record_id=?",
            (shop_id, record_id))
        self._commit()
        return cur.rowcount

    def observation_list(self, shop_id, start, end, kind=None, subject=None,
                         measure=None):
        sql = ("SELECT * FROM observation WHERE shop_id=? AND occurred_at>=?"
               " AND occurred_at<=?")
        args = [shop_id, start, end]
        for column, value in (("kind", kind), ("subject", subject), ("measure", measure)):
            if value:
                sql += f" AND {column}=?"
                args.append(value)
        sql += " ORDER BY occurred_at, kind, subject, rowid"
        return self._rows(self.conn.execute(sql, args))

    def observation_summary(self, shop_id, start, end):
        """按「类型 + 指标」汇总。这就是加新数据类型不用改分析代码的原因。"""
        return self._rows(self.conn.execute(
            """SELECT kind, measure, unit, value_text,
                      COUNT(*) AS n,
                      SUM(CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) AS n_num,
                      SUM(value_num) AS total,
                      AVG(value_num) AS avg,
                      MIN(value_num) AS min,
                      MAX(value_num) AS max
               FROM observation
               WHERE shop_id=? AND occurred_at>=? AND occurred_at<=?
               GROUP BY kind, measure, unit, value_text
               ORDER BY kind, measure""",
            (shop_id, start, end)))

    def observation_series(self, shop_id, start, end, kind, measure, subject=None):
        """按天出趋势，用来做「营业额 vs 客流」这类关联。"""
        sql = ("SELECT substr(occurred_at,1,10) AS date, unit, value_text,"
               " SUM(value_num) AS total, AVG(value_num) AS avg, COUNT(*) AS n"
               " FROM observation WHERE shop_id=? AND substr(occurred_at,1,10)>=?"
               " AND substr(occurred_at,1,10)<=? AND kind=? AND measure=?")
        args = [shop_id, start, end, kind, measure]
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        sql += " GROUP BY substr(occurred_at,1,10), unit, value_text ORDER BY date, unit, value_text"
        return self._rows(self.conn.execute(sql, args))

    def observation_kinds(self, shop_id):
        return self._rows(self.conn.execute(
            """SELECT kind, COUNT(*) AS n, COUNT(DISTINCT measure) AS measures
               FROM observation WHERE shop_id=? GROUP BY kind ORDER BY n DESC""",
            (shop_id,)))

    # ------------------------------------------------------------ 受控词表

    def dimension_upsert(self, shop_id, kind, canonical, aliases=None):
        canonical = str(canonical).strip()
        if not canonical or isinstance(aliases, str):
            raise ValueError("规范名不能为空，别名必须是列表")
        aliases = sorted({str(a).strip() for a in (aliases or []) if str(a).strip()} - {canonical})
        for existing in self.dimension_list(shop_id, kind):
            if existing["canonical"] != canonical and ({canonical, *aliases} &
                    {existing["canonical"], *existing["aliases"]}):
                raise ValueError("同类维度的别名不能指向多个规范名")
        row = self._row(self.conn.execute(
            "SELECT * FROM dimension WHERE shop_id=? AND kind=? AND canonical=?",
            (shop_id, kind, canonical)).fetchone())
        if row:
            row["aliases"] = json.loads(row["aliases"] or "[]")
            merged = sorted(set(row["aliases"]) | set(aliases))
            if merged != row["aliases"]:
                self.conn.execute("UPDATE dimension SET aliases=? WHERE id=?",
                                  (json.dumps(merged, ensure_ascii=False), row["id"]))
                self._commit()
            return {**row, "aliases": merged}

        did = new_id("dim")
        self.conn.execute(
            """INSERT INTO dimension(id, shop_id, kind, canonical, aliases, created_at)
               VALUES(?,?,?,?,?,?)""",
            (did, shop_id, kind, canonical,
             json.dumps(aliases, ensure_ascii=False), now_iso()),
        )
        self._commit()
        return {"id": did, "shop_id": shop_id, "kind": kind,
                "canonical": canonical, "aliases": aliases}

    def dimension_resolve(self, shop_id, kind, name):
        """把叫法归一到规范名。认不出就原样返回，不硬猜。"""
        name = str(name or "").strip()
        if not name:
            return ""
        for row in self.dimension_list(shop_id, kind):
            if name == row["canonical"] or name in (row.get("aliases") or []):
                return row["canonical"]
        return name

    def dimension_list(self, shop_id, kind=None):
        sql = "SELECT * FROM dimension WHERE shop_id=?"
        args = [shop_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY kind, canonical"
        rows = self._rows(self.conn.execute(sql, args))
        for r in rows:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        return rows

    # ------------------------------------------------------------ 导出

    def agent_request_get(self, principal_id, workspace_id, operation, request_id):
        row = self._row(self.conn.execute(
            "SELECT * FROM agent_request WHERE principal_id=? AND workspace_id=? AND operation=? AND request_id=?",
            (principal_id, workspace_id, operation, request_id)).fetchone())
        if row and row.get("response"):
            try:
                row["response"] = json.loads(row["response"])
            except (TypeError, ValueError):
                row["response"] = None
        return row

    def agent_request_claim(self, principal_id, workspace_id, operation, request_id,
                            fingerprint, lease_seconds=300):
        now = now_iso()
        token = new_id("claim")
        # A failed INSERT opens an implicit transaction too. Own a savepoint so
        # every return closes it without committing an enclosing service request.
        with self.transaction():
            try:
                self.conn.execute(
                    "INSERT INTO agent_request(principal_id, workspace_id, operation, request_id, fingerprint, response, created_at, lease_token) VALUES(?,?,?,?,?,?,?,?)",
                    (principal_id, workspace_id, operation, request_id, fingerprint, None, now, token),
                )
                return token
            except sqlite3.IntegrityError:
                row = self.conn.execute(
                    "SELECT fingerprint, response, created_at FROM agent_request WHERE principal_id=? AND workspace_id=? AND operation=? AND request_id=?",
                    (principal_id, workspace_id, operation, request_id),
                ).fetchone()
                if not row or row[1] is not None:
                    return None
                try:
                    created = dt.datetime.fromisoformat(str(row[2]))
                    stale = created < dt.datetime.now() - dt.timedelta(seconds=lease_seconds)
                except (TypeError, ValueError):
                    stale = False
                if stale and row[0] == fingerprint:
                    cur = self.conn.execute(
                        "UPDATE agent_request SET created_at=?, lease_token=? WHERE principal_id=? AND workspace_id=? AND operation=? AND request_id=? AND response IS NULL AND fingerprint=?",
                        (now, token, principal_id, workspace_id, operation, request_id, fingerprint),
                    )
                    return token if cur.rowcount == 1 else None
                return None

    def agent_request_finish(self, principal_id, workspace_id, operation, request_id,
                             lease_token, response):
        cur = self.conn.execute(
            "UPDATE agent_request SET response=? WHERE principal_id=? AND workspace_id=? AND operation=? AND request_id=? AND lease_token=? AND response IS NULL",
            (json.dumps(response, ensure_ascii=False), principal_id, workspace_id,
             operation, request_id, lease_token),
        )
        self._commit()
        return cur.rowcount == 1

    def agent_request_release(self, principal_id, workspace_id, operation, request_id,
                              lease_token):
        cur = self.conn.execute(
            "DELETE FROM agent_request WHERE principal_id=? AND workspace_id=? AND operation=? AND request_id=? AND lease_token=? AND response IS NULL",
            (principal_id, workspace_id, operation, request_id, lease_token),
        )
        self._commit()
        return cur.rowcount == 1

    def agent_import_preview_put(self, token, principal_id, workspace_id, sha256, plan):
        self.conn.execute(
            "INSERT INTO agent_import_preview(token, principal_id, workspace_id, sha256, plan, created_at) VALUES(?,?,?,?,?,?)",
            (token, principal_id, workspace_id, sha256,
             json.dumps(plan, ensure_ascii=False, sort_keys=True), now_iso()),
        )
        self._commit()

    def agent_import_preview_get(self, token, principal_id, workspace_id):
        row = self._row(self.conn.execute(
            "SELECT * FROM agent_import_preview WHERE token=? AND principal_id=? AND workspace_id=?",
            (token, principal_id, workspace_id)).fetchone())
        if not row:
            return None
        try:
            row["plan"] = json.loads(row["plan"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return row

    def agent_import_preview_consume(self, token, principal_id, workspace_id, request_id):
        cur = self.conn.execute(
            "UPDATE agent_import_preview SET consumed_request_id=? "
            "WHERE token=? AND principal_id=? AND workspace_id=? AND consumed_request_id IS NULL",
            (request_id, token, principal_id, workspace_id),
        )
        self._commit()
        return cur.rowcount == 1

    def export_all(self, shop_id):
        """全量导出，供以后同步到云端用。"""
        tables = ["shop", "entry", "product", "ingredient", "recipe_line",
                  "sale", "discrepancy", "import_batch",
                  'record', 'observation', 'dimension', 'recipe_version', 'import_identity',
                  'workspace_meta', 'agent_request', 'agent_import_preview']
        out = {"schema_version": SCHEMA_VERSION, "shop_id": shop_id, "tables": {}}
        for t in tables:
            if t == "shop":
                cur = self.conn.execute(f"SELECT * FROM {t} WHERE id=?", (shop_id,))
            elif t == "workspace_meta":
                cur = self.conn.execute(f"SELECT * FROM {t} WHERE shop_id=?", (shop_id,))
            elif t in ("agent_request", "agent_import_preview"):
                cur = self.conn.execute(f"SELECT * FROM {t} WHERE workspace_id=?", (shop_id,))
            else:
                cur = self.conn.execute(f"SELECT * FROM {t} WHERE shop_id=?", (shop_id,))
            out["tables"][t] = self._rows(cur)
        return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
