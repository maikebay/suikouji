"""Release entrypoint tests: disposable databases and fake secrets only."""
from __future__ import annotations

from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engine import deployment
from engine.store import SCHEMA_VERSION
from engine.agent_service import AgentService, Principal
from engine.runtime import probe_database
from engine.store import SqliteStore


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "data" / "accounting.db"
        self.env = {
            "SUIKOUJI_DB": str(self.db), "SUIKOUJI_OWNER_ID": "owner",
            "SUIKOUJI_PRINCIPAL_ID": "bot", "SUIKOUJI_WORKSPACE_ID": "shop",
            "SUIKOUJI_WORKSPACE_NAME": "测试店", "SUIKOUJI_PROFILE_ID": "restaurant",
            "SUIKOUJI_TIMEZONE": "Asia/Shanghai", "SUIKOUJI_API_ONLY": "1",
            "SUIKOUJI_BIND": "0.0.0.0", "SUIKOUJI_API_KEY": "fake-key-not-a-real-secret",
        }

    def test_new_database_is_private_and_restart_is_readonly(self):
        self.assertEqual(deployment.initialize_database(self.env)["status"], "created")
        self.assertTrue(probe_database(str(self.db), "owner", "shop")["ok"])
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        before = self.db.read_bytes()
        with patch("engine.deployment.SqliteStore", side_effect=AssertionError("writer constructed")):
            result = deployment.initialize_database(self.env, allow_existing=True)
        self.assertEqual(result["status"], "existing")
        self.assertEqual(self.db.read_bytes(), before)
        with self.assertRaises(ValueError):
            deployment.initialize_database(self.env)
        self.assertEqual(self.db.read_bytes(), before)

    def test_invalid_configuration_creates_no_database(self):
        for key, value in (("SUIKOUJI_PROFILE_ID", "missing"),
                           ("SUIKOUJI_TIMEZONE", "not/a/zone"),
                           ("SUIKOUJI_DB", "data/accounting-demo.db"),
                           ("SUIKOUJI_DB", str(deployment.ROOT / "data" / "never-create.db")),
                           ("SUIKOUJI_WORKSPACE_ID", "")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                deployment.initialize_database({**self.env, key: value})
            self.assertFalse(self.db.exists())

    def test_existing_wrong_workspace_or_metadata_never_changes(self):
        deployment.initialize_database(self.env)
        before = self.db.read_bytes()
        for key, value in (("SUIKOUJI_OWNER_ID", "other"), ("SUIKOUJI_WORKSPACE_ID", "other"),
                           ("SUIKOUJI_PROFILE_ID", "retail"), ("SUIKOUJI_TIMEZONE", "UTC")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                deployment.initialize_database({**self.env, key: value}, allow_existing=True)
            self.assertEqual(self.db.read_bytes(), before)

    def test_incomplete_or_old_schema_is_not_migrated(self):
        deployment.initialize_database(self.env)
        for sql, undo in (("ALTER TABLE product RENAME COLUMN category TO broken_category",
                           "ALTER TABLE product RENAME COLUMN broken_category TO category"),
                          ("UPDATE meta SET value='1' WHERE key='schema_version'",
                           "UPDATE meta SET value='%d' WHERE key='schema_version'" % SCHEMA_VERSION)):
            with closing(sqlite3.connect(str(self.db))) as conn:
                conn.execute(sql)
                conn.commit()
            before = self.db.read_bytes()
            with self.assertRaises(ValueError):
                deployment.initialize_database(self.env, allow_existing=True)
            self.assertEqual(self.db.read_bytes(), before)
            with closing(sqlite3.connect(str(self.db))) as conn:
                conn.execute(undo)
                conn.commit()

    def test_symlink_target_and_existing_unknown_file_are_preserved(self):
        self.db.parent.mkdir()
        victim = self.root / "victim"
        victim.write_bytes(b"preserve")
        self.db.symlink_to(victim)
        with self.assertRaises(ValueError):
            deployment.initialize_database(self.env, allow_existing=True)
        self.assertEqual(victim.read_bytes(), b"preserve")
        self.db.unlink()
        self.db.write_bytes(b"not sqlite")
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            deployment.initialize_database(self.env, allow_existing=True)
        self.assertEqual(self.db.read_bytes(), before)

    def test_publication_never_overwrites_destination_created_during_init(self):
        original_link = os.link

        def racing_link(source, target):
            Path(target).write_bytes(b"concurrent database")
            original_link(source, target)

        with patch("engine.deployment.os.link", side_effect=racing_link):
            with self.assertRaises(FileExistsError):
                deployment.initialize_database(self.env)
        self.assertEqual(self.db.read_bytes(), b"concurrent database")
        self.assertEqual(list(self.db.parent.glob(".init-*")), [])

    def test_failed_initialization_leaves_no_database_or_temporary_file(self):
        with patch("engine.deployment.SqliteStore", side_effect=OSError("injected failure")):
            with self.assertRaises(OSError):
                deployment.initialize_database(self.env)
        self.assertFalse(self.db.exists())
        self.assertEqual(list(self.db.parent.iterdir()), [])

    def test_secret_file_loaded_without_output_or_environment_mutation(self):
        secret = self.root / "fake-api-key"
        secret.write_text("fake-file-key-not-real-secret\n", encoding="ascii")
        env = {**self.env, "SUIKOUJI_API_KEY": "", "SUIKOUJI_API_KEY_FILE": str(secret)}
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(deployment.load_api_key(env), "fake-file-key-not-real-secret")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(env["SUIKOUJI_API_KEY"], "")
        with self.assertRaises(ValueError):
            deployment.load_api_key({**env, "SUIKOUJI_API_KEY": "another-fake-key"})

    def test_malformed_key_fails_before_database_initialization(self):
        for value in ("", "short", "f" * 513, "é" * 32, "f" * 31 + " ", "f" * 31 + "\n"):
            with self.subTest(length=len(value)):
                with patch("engine.deployment.initialize_database") as initialize:
                    with self.assertRaises(ValueError):
                        deployment.serve({**self.env, "SUIKOUJI_API_KEY": value})
                    initialize.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_secret_file_limit_and_invalid_bytes(self):
        secret = self.root / "fake-api-key"
        env = {"SUIKOUJI_API_KEY_FILE": str(secret)}
        for raw in (b"f" * 513, b"f" * 20 + b"\x00", b"f" * 20 + b"\xff", b"f" * 20 + b"\n\n"):
            with self.subTest(raw_length=len(raw)):
                secret.write_bytes(raw)
                with self.assertRaises(ValueError):
                    deployment.load_api_key(env)

    def test_serve_preserves_runtime_contract_and_timezone(self):
        with patch.dict(os.environ, {}, clear=False):
            original_umask = os.umask(0o077)
            try:
                with patch("engine.deployment.runtime.run") as run:
                    deployment.serve(self.env)
                config = run.call_args.args[0]
                self.assertEqual(config.db_path, str(self.db))
                self.assertEqual(config.api_key, self.env["SUIKOUJI_API_KEY"])
                self.assertEqual(config.bind, "0.0.0.0")
                self.assertTrue(config.api_only)
                self.assertFalse(config.ilink_account_path)
                self.assertEqual(os.environ["TZ"], "Asia/Shanghai")
            finally:
                os.umask(original_umask)
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()

    def test_api_only_required_before_new_database(self):
        with patch("engine.deployment.runtime.run") as run:
            with self.assertRaises(ValueError):
                deployment.serve({**self.env, "SUIKOUJI_API_ONLY": "0"})
            run.assert_not_called()
        self.assertFalse(self.db.exists())

    def test_check_cli_returns_nonzero_without_creating_missing_file(self):
        env = {**os.environ, **self.env}
        result = subprocess.run([sys.executable, "-m", "engine.deployment", "check", str(self.db)],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.db.exists())
        self.assertNotIn(self.env["SUIKOUJI_API_KEY"], result.stdout + result.stderr)

    def test_backup_publication_does_not_replace_concurrent_destination(self):
        deployment.initialize_database(self.env)
        destination = self.root / "backup.db"
        original_link = os.link

        def racing_link(source, target):
            Path(target).write_bytes(b"keep concurrent file")
            original_link(source, target)

        with patch("engine.deployment.os.link", side_effect=racing_link), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(deployment.main(["backup", str(self.db), str(destination)]), 1)
        self.assertEqual(destination.read_bytes(), b"keep concurrent file")
        self.assertEqual(list(self.root.glob(".deploy-copy-*")), [])

    def test_healthcheck_uses_readiness_and_bypasses_proxy_settings(self):
        from unittest.mock import MagicMock
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"status":"ready"}'
        with patch("engine.deployment.urllib.request.build_opener", return_value=opener) as build:
            deployment.healthcheck({"SUIKOUJI_PORT": "8765"})
        self.assertEqual(build.call_args.args[0].proxies, {})
        opener.open.assert_called_once_with("http://127.0.0.1:8765/readyz", timeout=3)
        response.read.return_value = b'{"status":"not_ready"}'
        with patch("engine.deployment.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                deployment.healthcheck({})

    def test_init_module_cli_creates_once_and_never_prints_key(self):
        env = {**os.environ, **self.env}
        arguments = [sys.executable, "-m", "engine.deployment", "init"]
        created = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(json.loads(created.stdout)["status"], "created")
        self.assertNotIn(self.env["SUIKOUJI_API_KEY"], created.stdout + created.stderr)
        before = self.db.read_bytes()
        refused = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertNotIn(self.env["SUIKOUJI_API_KEY"], refused.stdout + refused.stderr)

    def test_backup_restore_cli_retains_full_response_and_refuses_existing(self):
        deployment.initialize_database(self.env)
        with closing(SqliteStore(str(self.db))) as store:
            expected = AgentService(store).capture_text(Principal("bot", "owner"), "shop",
                                                        "今天营业额100", "durable")
        backup, restored = self.root / "backup.db", self.root / "restored.db"
        for command in (("backup", self.db, backup), ("check", backup), ("restore", backup, restored)):
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(deployment.main(list(map(str, command))), 0)
        with closing(SqliteStore(str(restored))) as store:
            self.assertEqual(AgentService(store).capture_text(Principal("bot", "owner"), "shop",
                "今天营业额100", "durable"), {**expected, "duplicate": True})
        for command, target in (("backup", backup), ("restore", restored)):
            before = target.read_bytes()
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(deployment.main([command, str(self.db), str(target)]), 1)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
