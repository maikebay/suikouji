"""Agent Store compatibility; all databases and import files are disposable."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from engine.importer import commit_import, plan_import
from engine.store import SCHEMA_VERSION, SqliteStore, Store, sha256_file


# Fixed pre-lease DDL: do not derive historical fixtures from today's _SCHEMA.
_LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE agent_request (
    principal_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, workspace_id, operation, request_id)
);
"""
_LEGACY_ROWS = """SELECT principal_id, workspace_id, operation, request_id,
                         fingerprint, response, created_at
                  FROM agent_request ORDER BY request_id"""
_KEY = ("agent-a", "shop-a", "capture", "legacy-request")
_FINGERPRINT = "f" * 64
_NOW = dt.datetime(2026, 10, 25, 12, 0)
_STALE = (_NOW - dt.timedelta(seconds=600)).isoformat(timespec="microseconds")


class _FixedDateTime(dt.datetime):
    @classmethod
    def now(cls):
        return _NOW


class _StoreProxy:
    """Expose only declared Store operations, with no SQLite escape hatch."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        if name not in Store.__abstractmethods__:
            raise AttributeError(name)
        return getattr(self._store, name)


class AgentStoreCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clock = SimpleNamespace(datetime=_FixedDateTime, timedelta=dt.timedelta)
        # Patch Store's namespace rather than the shared datetime module.
        for mock in (patch("engine.store.dt", clock),
                     patch("engine.store.now_iso", _NOW.isoformat)):
            mock.start()
            self.addCleanup(mock.stop)

    def legacy_database(self, version, response=None, created_at=_STALE):
        path = Path(self.tmp.name) / f"legacy-v{version}.db"
        with closing(sqlite3.connect(str(path))) as conn:
            conn.executescript(_LEGACY_SCHEMA)
            conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(version),))
            conn.execute("INSERT INTO agent_request VALUES(?,?,?,?,?,?,?)",
                         (*_KEY, _FINGERPRINT, response, created_at))
            self.assertNotIn("lease_token", {
                row[1] for row in conn.execute("PRAGMA table_info(agent_request)")})
            before = conn.execute(_LEGACY_ROWS).fetchall()
            conn.commit()
        return path, before

    def assert_upgraded(self, store, before):
        self.assertIn("lease_token", {
            row[1] for row in store.conn.execute("PRAGMA table_info(agent_request)")})
        self.assertEqual(store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
            str(SCHEMA_VERSION))
        self.assertEqual([tuple(row) for row in store.conn.execute(_LEGACY_ROWS)], before)

    def test_old_completed_response_survives_migration_and_reopen(self):
        response = {"capture": {"id": "old-capture", "raw_text": "旧原话"},
                    "entries": [{"amount": 10.5}], "duplicate": False}
        raw_response = json.dumps(response, ensure_ascii=False, indent=2)
        for version in (SCHEMA_VERSION - 1, SCHEMA_VERSION):
            with self.subTest(schema_version=version):
                path, before = self.legacy_database(version, raw_response)
                for opening in range(2):
                    with self.subTest(opening=opening), closing(SqliteStore(str(path))) as store:
                        self.assert_upgraded(store, before)
                        saved = store.agent_request_get(*_KEY)
                        self.assertEqual(saved["response"], response)
                        self.assertIsNone(saved["lease_token"])
                        for fingerprint in (_FINGERPRINT, "different-payload"):
                            self.assertIsNone(store.agent_request_claim(*_KEY, fingerprint))
                        self.assertFalse(store.agent_request_finish(
                            *_KEY, "unowned-lease", {"overwritten": True}))
                        self.assertFalse(store.agent_request_release(*_KEY, "unowned-lease"))
                        self.assertEqual(store.agent_request_get(*_KEY), saved)
                        # Preserve the stored response bytes, not just decoded JSON.
                        self.assert_upgraded(store, before)

    def test_recent_legacy_pending_request_cannot_be_reclaimed(self):
        recent = (_NOW - dt.timedelta(seconds=10)).isoformat(timespec="microseconds")
        for version in (SCHEMA_VERSION - 1, SCHEMA_VERSION):
            with self.subTest(schema_version=version):
                path, before = self.legacy_database(version, created_at=recent)
                with closing(SqliteStore(str(path))) as store:
                    self.assert_upgraded(store, before)
                    saved = store.agent_request_get(*_KEY)
                    self.assertIsNone(saved["response"])
                    self.assertIsNone(saved["lease_token"])
                    for fingerprint in (_FINGERPRINT, "different-payload"):
                        self.assertIsNone(store.agent_request_claim(*_KEY, fingerprint))
                    for token in (None, "unowned-lease"):
                        self.assertFalse(store.agent_request_finish(*_KEY, token, {"wrong": True}))
                        self.assertFalse(store.agent_request_release(*_KEY, token))
                    self.assertEqual(store.agent_request_get(*_KEY), saved)
                with closing(SqliteStore(str(path))) as reopened:
                    self.assertEqual(reopened.agent_request_get(*_KEY), saved)

    def test_stale_legacy_pending_request_recovers_only_matching_payload(self):
        response = {"capture": {"id": "recovered", "raw_text": "恢复结果"}}
        for version in (SCHEMA_VERSION - 1, SCHEMA_VERSION):
            with self.subTest(schema_version=version):
                path, before = self.legacy_database(version)
                with closing(SqliteStore(str(path))) as store:
                    self.assert_upgraded(store, before)
                    saved = store.agent_request_get(*_KEY)
                    self.assertIsNone(store.agent_request_claim(*_KEY, "different-payload"))
                    self.assertEqual(store.agent_request_get(*_KEY), saved)
                    token = store.agent_request_claim(*_KEY, _FINGERPRINT)
                    self.assertIsInstance(token, str)
                    self.assertTrue(token)
                    recovered = store.agent_request_get(*_KEY)
                    self.assertEqual(recovered["lease_token"], token)
                    self.assertEqual(recovered["fingerprint"], _FINGERPRINT)
                    self.assertEqual(recovered["created_at"], _NOW.isoformat())
                    self.assertIsNone(recovered["response"])
                    self.assertIsNone(store.agent_request_claim(*_KEY, _FINGERPRINT))
                    self.assertFalse(store.agent_request_finish(*_KEY, None, {"wrong": True}))
                    self.assertFalse(store.agent_request_release(*_KEY, None))
                    self.assertTrue(store.agent_request_finish(*_KEY, token, response))
                    saved = store.agent_request_get(*_KEY)
                with closing(SqliteStore(str(path))) as reopened:
                    self.assertEqual(reopened.agent_request_get(*_KEY), saved)
                    self.assertEqual(saved["response"], response)
                    self.assertIsNone(reopened.agent_request_claim(*_KEY, _FINGERPRINT))

    def test_reclaimed_and_released_leases_fence_previous_workers(self):
        path = str(Path(self.tmp.name) / "workers.db")
        with closing(SqliteStore(path)) as first, closing(SqliteStore(path)) as second:
            with patch("engine.store.now_iso", return_value=_STALE):
                old_token = first.agent_request_claim(*_KEY, _FINGERPRINT)
            self.assertTrue(old_token)
            token = second.agent_request_claim(*_KEY, _FINGERPRINT)
            self.assertTrue(token)
            self.assertNotEqual(token, old_token)
            active = second.agent_request_get(*_KEY)
            self.assertFalse(first.agent_request_finish(*_KEY, old_token, {"old": True}))
            self.assertFalse(first.agent_request_release(*_KEY, old_token))
            self.assertEqual(second.agent_request_get(*_KEY), active)

            self.assertTrue(second.agent_request_release(*_KEY, token))
            self.assertIsNone(first.agent_request_get(*_KEY))
            fresh_token = first.agent_request_claim(*_KEY, _FINGERPRINT)
            self.assertTrue(fresh_token)
            self.assertNotIn(fresh_token, (old_token, token))
            active = first.agent_request_get(*_KEY)
            self.assertFalse(second.agent_request_finish(*_KEY, token, {"released": True}))
            self.assertFalse(second.agent_request_release(*_KEY, token))
            self.assertEqual(first.agent_request_get(*_KEY), active)

            self.assertTrue(first.agent_request_finish(*_KEY, fresh_token, {"current": True}))
            self.assertFalse(first.agent_request_finish(*_KEY, fresh_token, {"overwritten": True}))
            self.assertFalse(first.agent_request_release(*_KEY, fresh_token))
            self.assertIsNone(second.agent_request_claim(*_KEY, _FINGERPRINT))
        with closing(SqliteStore(path)) as reopened:
            self.assertEqual(reopened.agent_request_get(*_KEY)["response"], {"current": True})

    def test_import_preview_keeps_principal_and_workspace_scope_after_reopen(self):
        path = str(Path(self.tmp.name) / "previews.db")
        plan = {"kind": "entries", "rows": [{"date": "2026-10-25", "amount": 10,
                                               "note": "中文预览"}],
                "mapping": {"amount": "金额"}, "skipped": [], "row_count": 1}
        scopes = (("agent-b", "shop-a"), ("agent-a", "shop-b"), ("agent-b", "shop-b"))
        with closing(SqliteStore(path)) as store:
            store.agent_import_preview_put("preview-a", "agent-a", "shop-a", _FINGERPRINT, plan)
            store.agent_import_preview_put("preview-b", "agent-b", "shop-b", "b" * 64, {"rows": []})
            saved = store.agent_import_preview_get("preview-a", "agent-a", "shop-a")
            self.assertEqual(saved["plan"], plan)
            self.assertEqual(saved["sha256"], _FINGERPRINT)
            for principal, workspace in scopes:
                self.assertIsNone(store.agent_import_preview_get("preview-a", principal, workspace))
        with closing(SqliteStore(path)) as reopened:
            self.assertEqual(reopened.agent_import_preview_get("preview-a", "agent-a", "shop-a"), saved)
            for principal, workspace in scopes:
                with self.subTest(principal=principal, workspace=workspace):
                    self.assertIsNone(reopened.agent_import_preview_get("preview-a", principal, workspace))
            self.assertEqual(reopened.agent_import_preview_get(
                "preview-b", "agent-b", "shop-b")["plan"], {"rows": []})
            self.assertIsNone(reopened.agent_import_preview_get("missing", "agent-a", "shop-a"))
            exported = reopened.export_all("shop-a")["tables"]["agent_import_preview"]
            self.assertEqual([row["token"] for row in exported], ["preview-a"])


class ImporterStoreInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def import_plan(self, kind):
        csv = {
            "entries": "日期,类型,分类,金额,明细ID,来源系统\n"
                       "2026-10-25,支出,包装,10,entry-1,test\n",
            "sales": "日期,商品,数量,销售额,明细ID,来源系统\n"
                     "2026-10-25,牛肉面,2,24,sale-1,test\n",
        }[kind]
        path = Path(self.tmp.name) / f"{kind}.csv"
        path.write_text(csv, encoding="utf-8")
        plan = plan_import(str(path))
        self.assertEqual(plan["kind"], kind)
        self.assertEqual(plan["skipped"], [])
        return plan, sha256_file(str(path))

    def test_commit_and_deduplication_use_only_store_interface(self):
        for kind in ("entries", "sales"):
            with self.subTest(kind=kind), closing(SqliteStore(":memory:")) as store:
                proxy = _StoreProxy(store)
                self.assertFalse(hasattr(proxy, "conn"))
                proxy.ensure_shop("shop-a")
                proxy.ensure_shop("shop-b")
                plan, digest = self.import_plan(kind)
                first = commit_import(proxy, "shop-a", plan, "first.csv", digest)
                self.assertFalse(first["duplicate"])
                self.assertEqual(first["imported"], 1)
                self.assertEqual(proxy.batch_find_by_hash("shop-a", digest)["rows"], 1)
                before = proxy.export_all("shop-a")
                replay = commit_import(proxy, "shop-a", plan, "renamed.csv", digest)
                self.assertTrue(replay["duplicate"])
                self.assertEqual(replay["imported"], 0)
                self.assertEqual(proxy.export_all("shop-a"), before)

                duplicate = commit_import(proxy, "shop-a", plan, "overlap.csv", "overlap-hash")
                self.assertEqual((duplicate["imported"], duplicate["duplicates"]), (0, 1))
                self.assertEqual(proxy.batch_find_by_hash("shop-a", "overlap-hash")["rows"], 0)
                self.assertEqual(len(proxy.export_all("shop-a")["tables"]["import_identity"]), 1)
                other = commit_import(proxy, "shop-b", plan, "other.csv", digest)
                self.assertEqual(other["imported"], 1)
                list_rows = proxy.list_entries if kind == "entries" else proxy.list_sales
                for workspace in ("shop-a", "shop-b"):
                    rows = list_rows(workspace, "2026-10-25", "2026-10-25")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["amount"], plan["rows"][0]["amount"])
                    self.assertEqual(rows[0]["source"], "import")

    def test_conflict_rolls_back_batch_and_prior_rows_through_proxy(self):
        for kind in ("entries", "sales"):
            with self.subTest(kind=kind), closing(SqliteStore(":memory:")) as store:
                proxy = _StoreProxy(store)
                self.assertFalse(hasattr(proxy, "conn"))
                proxy.ensure_shop("shop-a")
                plan, digest = self.import_plan(kind)
                commit_import(proxy, "shop-a", plan, "first.csv", digest)
                before = proxy.export_all("shop-a")
                original = plan["rows"][0]
                new_row = {**original, "external_id": "new-first"}
                if kind == "sales":
                    # A new product as well as its sale must roll back.
                    new_row["product_name"] = "米饭"
                conflict = {**plan, "rows": [new_row, {**original, "amount": original["amount"] + 1}],
                            "row_count": 2, "source_rows": 2}
                with self.assertRaisesRegex(ValueError, "同一来源明细或日汇总数值发生变化"):
                    commit_import(proxy, "shop-a", conflict, "conflict.csv", "conflict-hash")
                self.assertIsNone(proxy.batch_find_by_hash("shop-a", "conflict-hash"))
                self.assertEqual(proxy.export_all("shop-a"), before)


if __name__ == "__main__":
    unittest.main()
