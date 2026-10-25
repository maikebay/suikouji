"""Preparation uses fresh temporary directories and fake/generated test keys only."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from tools.prepare_deploy import ROOT, main, prepare


class PrepareDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "new state"

    def test_secure_configuration_has_paths_but_never_secret(self):
        with patch("tools.prepare_deploy.os.getuid", return_value=501), \
                patch("tools.prepare_deploy.os.getgid", return_value=20):
            config = prepare(self.state, workspace_name="糖水 店")
        key = self.state / "secrets" / "api_key"
        contents = config.read_text()
        secret = key.read_text().strip()
        self.assertGreaterEqual(len(secret), 32)
        self.assertNotIn(secret, contents)
        self.assertIn("SUIKOUJI_UID='501'", contents)
        self.assertIn("SUIKOUJI_GID='20'", contents)
        self.assertIn("SUIKOUJI_WORKSPACE_NAME='糖水 店'", contents)
        self.assertIn(str(self.state / "data"), contents)
        for item in (key, config):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)
        for item in (self.state, self.state / "data", self.state / "backups", self.state / "secrets"):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o700)

    def test_rerun_does_not_change_any_created_file(self):
        prepare(self.state)
        snapshots = {path.relative_to(self.state): path.read_bytes()
                     for path in self.state.rglob("*") if path.is_file()}
        with self.assertRaises(ValueError):
            prepare(self.state)
        self.assertEqual(snapshots, {path.relative_to(self.state): path.read_bytes()
                                     for path in self.state.rglob("*") if path.is_file()})

    def test_existing_or_dangling_symlink_is_not_followed(self):
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "preserve"
        sentinel.write_bytes(b"keep")
        self.state.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare(self.state)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual(list(destination.iterdir()), [sentinel])
        self.state.unlink()
        self.state.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare(self.state)
        self.assertFalse((self.root / "missing").exists())

    def test_invalid_values_fail_before_creating_state(self):
        for kwargs in ({"port": 0}, {"port": True}, {"port": 65536}, {"profile": "unknown"},
                       {"timezone": "bad-zone"}, {"owner_id": "owner\n"},
                       {"workspace_id": ""}, {"workspace_name": "$(print)"},
                       {"workspace_name": "a'b"}, {"workspace_name": "a\\b"},
                       {"workspace_name": "line\nbreak"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                prepare(self.state, **kwargs)
            self.assertFalse(self.state.exists())
        for path in ("relative-state", ROOT / "never-create-state", self.root / "missing" / "child"):
            with self.subTest(path=str(path)), self.assertRaises(ValueError):
                prepare(path)
        self.assertFalse((ROOT / "never-create-state").exists())

    def test_root_preparation_assigns_only_new_data_and_key(self):
        changes = []
        with patch("tools.prepare_deploy.os.getuid", return_value=0), \
                patch("tools.prepare_deploy.os.chown", side_effect=lambda path, uid, gid: changes.append((Path(path), uid, gid))), \
                patch("tools.prepare_deploy.os.fchown") as fchown:
            config = prepare(self.state)
        self.assertIn("SUIKOUJI_UID='10001'", config.read_text())
        self.assertEqual({path for path, _, _ in changes},
                         {self.state.resolve() / "data", self.state.resolve() / "backups", self.state.resolve() / "secrets"})
        self.assertTrue(all((uid, gid) == (10001, 10001) for _, uid, gid in changes))
        self.assertEqual(fchown.call_args.args[1:], (10001, 10001))

    def test_cli_never_prints_key_and_refuses_repeat(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--state-dir", str(self.state)]), 0)
        key = (self.state / "secrets" / "api_key").read_text().strip()
        self.assertNotIn(key, output.getvalue())
        self.assertIn("docker compose --env-file", output.getvalue())
        self.assertIn("up -d --wait", output.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["--state-dir", str(self.state)]), 1)


if __name__ == "__main__":
    unittest.main()
