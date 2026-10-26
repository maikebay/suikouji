"""A fixed released v5 fixture must upgrade without reinterpreting receipts."""
from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from engine.agent_service import AgentError, AgentService, Principal
from tests import test_agent_http_contract as http_contract
from types import SimpleNamespace
from engine.deployment import main as deployment_main
from engine.runtime import probe_database
from engine.store import SqliteStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/schema-v5.sql"
P = Principal("agent", "owner")
RECEIVED = "2026-10-26T20:00:00+08:00"
TEXT = "今天营业额1300元，阴天"
STRUCTURED = {"entries": [{"date": "2026-10-26", "type": "revenue", "category": "营业额",
    "amount": 1300, "amount_basis": "gross", "granularity": "daily", "assertion": "actual",
    "source_quote": "今天营业额1300元"}], "sales": [], "observations": [{"date": "2026-10-26",
    "kind": "天气", "measure": "天气", "value_text": "阴", "assertion": "actual", "source_quote": "阴天"}],
    "leftovers": []}


class ThoughtsUpgradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / "old.db"
        self.destination = Path(tmp.name) / "new.db"
        with closing(sqlite3.connect(self.source)) as conn:
            conn.executescript(FIXTURE.read_text(encoding="utf-8"))
            conn.commit()
            self.saved = {row[0]: json.loads(row[1]) for row in conn.execute(
                "SELECT request_id,response FROM agent_request")}
        self.before = self.source.read_bytes()

    def migrate(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return deployment_main(["migrate", str(self.source), str(self.destination)])

    def test_explicit_copy_upgrade_and_old_receipts_are_exactly_replayed(self):
        self.assertFalse(probe_database(str(self.source), "owner", "shop")["ok"])
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertTrue(probe_database(str(self.destination), "owner", "shop")["ok"])
        with closing(SqliteStore(str(self.destination))) as store:
            service = AgentService(store)
            structured = service.capture_text(P, "shop", TEXT, "v5-structured", received_at=RECEIVED,
                                               structured=STRUCTURED)
            raw = service.capture_text(P, "shop", "记个想法：明天试南瓜甜品", "v5-native", received_at=RECEIVED)
            self.assertEqual(structured, {**self.saved["v5-structured"], "duplicate": True})
            self.assertEqual(raw, {**self.saved["v5-native"], "duplicate": True})
            self.assertNotIn("thoughts", raw)
            self.assertEqual(store.thought_list("shop")["items"], [])
            self.assertEqual(service.summary(P, "shop")["revenue"], 1300)
            with self.assertRaises(AgentError) as caught:
                service.capture_text(P, "shop", TEXT, "v5-structured", received_at=RECEIVED,
                                     structured={**STRUCTURED, "thoughts": []})
            self.assertEqual(caught.exception.code, "idempotency_conflict")
            fresh = service.capture_text(P, "shop", "记个想法：明天试南瓜甜品", "new-native", received_at=RECEIVED)
            self.assertEqual(len(fresh["thoughts"]), 1)
        # Reopen, backup and restore retain note tags and the complete response.
        backup = self.destination.with_name("backup.db")
        restored = self.destination.with_name("restored.db")
        for command, src, dest in (("backup", self.destination, backup), ("restore", backup, restored)):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(deployment_main([command, str(src), str(dest)]), 0)
        with closing(SqliteStore(str(restored))) as store:
            replay = AgentService(store).capture_text(P, "shop", "记个想法：明天试南瓜甜品", "new-native", received_at=RECEIVED)
            self.assertEqual(replay, {**fresh, "duplicate": True})
            self.assertEqual(len(store.thought_list("shop")["items"]), 1)

    def test_http_refuses_v5_without_opening_a_migrating_store(self):
        self.server = SimpleNamespace(db_path=self.source, api_key="fake-contract-token", owner_id="owner",
            principal_id="agent", workspace_id="shop", api_only=True, server_port=8765)
        status, _, _ = http_contract.AgentHttpContractTests.request(self, "POST", "/v1/captures", {
            "workspace_id": "shop", "request_id": "new", "text": "记个想法：新品"})
        self.assertEqual(status, 503)
        self.assertEqual(self.source.read_bytes(), self.before)
        with self.assertRaises(ValueError):
            SqliteStore(str(self.source), migrate=False)
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_legacy_http_get_and_post_never_migrate_v5(self):
        self.server = SimpleNamespace(db_path=self.source, api_key="fake-contract-token", owner_id="owner",
            principal_id="agent", workspace_id="", api_only=False, server_port=8765)
        for method, path, payload in (("GET", "/api/workspaces", None),
                                      ("GET", "/api/timeline?workspace=shop", None),
                                      ("GET", "/api/state", None),
                                      ("GET", "/api/records/missing", None),
                                      ("POST", "/api/records", {"text": "今天营业额9999", "request_id": "new"})):
            with self.subTest(path=path):
                status, _, body = http_contract.AgentHttpContractTests.request(self, method, path, payload, authorization=None)
                self.assertEqual(status, 503)
                self.assertIsInstance(body["error"], str)
                self.assertEqual(self.source.read_bytes(), self.before)

    def test_demo_startup_preserves_existing_v5_and_only_initializes_new_path(self):
        from unittest.mock import patch
        from engine.web import make_server
        with patch("engine.web.HTTPServer", return_value=SimpleNamespace()):
            make_server(8765, self.source)
            self.assertEqual(self.source.read_bytes(), self.before)
            self.assertFalse(probe_database(str(self.source))["ok"])
            make_server(8765, self.destination)
            self.assertTrue(probe_database(str(self.destination))["ok"])
            untouched = self.destination.with_name("api-only-missing.db")
            make_server(8765, untouched, api_only=True)
            self.assertFalse(untouched.exists())

    def test_operational_checks_reject_incomplete_v6_without_publishing(self):
        from engine.backup import inspect_database
        for ddl in ("DROP TABLE thought_tags", "DROP INDEX idx_thought_tags_lookup"):
            with self.subTest(ddl=ddl), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "broken.db"
                with closing(SqliteStore(str(source))) as store:
                    store.ensure_shop("s")
                    store.conn.execute(ddl)
                    store.conn.commit()
                before = source.read_bytes()
                self.assertFalse(inspect_database(str(source))["ok"])
                for command in ("backup", "restore"):
                    destination = Path(tmp) / (command + ".db")
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(deployment_main([command, str(source), str(destination)]), 1)
                    self.assertFalse(destination.exists())
                    self.assertEqual(source.read_bytes(), before)

    def test_migration_never_overwrites_existing_destination(self):
        self.destination.write_bytes(b"KEEP TARGET")
        self.assertNotEqual(self.migrate(), 0)
        self.assertEqual(self.destination.read_bytes(), b"KEEP TARGET")
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_real_cli_mixed_capture_and_replay_in_temporary_database(self):
        self.assertEqual(self.migrate(), 0)
        text = "2026年10月25日营业额230元。记个随想：明天试南瓜甜品 #新品"
        command = [sys.executable, "-B", "-m", "engine.cli", "--db", str(self.destination),
                   "--shop", "shop", "--json", "record", text, "--dedup-ref", "cli-thought"]
        first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        receipt = json.loads(first.stdout)
        self.assertEqual(len(receipt["thoughts"]), 1)
        self.assertEqual(receipt["thoughts"][0]["tags"], ["新品"])
        replay = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)
        self.assertTrue(json.loads(replay.stdout)["duplicate"])
        with closing(SqliteStore(str(self.destination))) as store:
            self.assertEqual(len(store.thought_list("shop")["items"]), 1)
            self.assertEqual(store.summary("shop", "0000", "9999")["revenue"], 1530)


if __name__ == "__main__":
    unittest.main()
