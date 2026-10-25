"""A source release must use an explicit allowlist and deterministic archives."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.build_release import FILES, VERSION, build_release


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for relative in FILES:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public source fixture " + relative + "\n", encoding="utf-8")

    def test_allowlist_excludes_private_files_without_reading_them(self):
        forbidden = ("data/accounting-demo.db", "data/web-demo.db", "data/demo.db", "data/account.json",
                     ".env", ".claude/settings.local.json", "credentials/token.json", "engine/private.json",
                     "engine/unlisted.py", "old-backup.zip")
        for relative in forbidden:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PRIVATE_SENTINEL")
        read = Path.read_bytes
        opened = []

        def guarded(path):
            opened.append(path.relative_to(self.source).as_posix())
            self.assertNotIn(path.relative_to(self.source).as_posix(), forbidden)
            return read(path)

        with patch.object(Path, "read_bytes", guarded):
            archive, checksum = build_release(self.source, self.root / "out")
        self.assertEqual(set(opened), set(FILES))
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            names = {member.name for member in members}
            prefix = "suikouji-" + VERSION + "/"
            self.assertEqual(names, {prefix + name for name in FILES} | {prefix + "RELEASE.json", prefix + "SHA256SUMS"})
            for member in members:
                self.assertTrue(member.isfile())
                self.assertEqual(member.mode, 0o644)
                self.assertEqual(member.mtime, 0)
                self.assertNotIn("..", Path(member.name).parts)
                self.assertNotIn(b"PRIVATE_SENTINEL", bundle.extractfile(member).read())
            manifest = json.load(bundle.extractfile(prefix + "RELEASE.json"))
            self.assertEqual(set(manifest["files"]), set(FILES))
            self.assertFalse(manifest["contains_credentials"])
            sums = bundle.extractfile(prefix + "SHA256SUMS").read().decode().splitlines()
            for line in sums:
                digest, name = line.split("  ", 1)
                self.assertEqual(hashlib.sha256(bundle.extractfile(prefix + name).read()).hexdigest(), digest)
        self.assertEqual(checksum.read_text(), f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")

    def test_build_is_deterministic_across_output_locations(self):
        first, _ = build_release(self.source, self.root / "one")
        second, _ = build_release(self.source, self.root / "two")
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_overwrite_refused_without_changes(self):
        archive, checksum = build_release(self.source, self.root / "out")
        before = (archive.read_bytes(), checksum.read_bytes())
        with self.assertRaises(ValueError):
            build_release(self.source, archive.parent)
        self.assertEqual((archive.read_bytes(), checksum.read_bytes()), before)

    def test_symlink_file_or_parent_refused(self):
        path = self.source / "README.md"
        target = self.root / "private"
        target.write_bytes(b"do not open")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            build_release(self.source, self.root / "out")
        path.unlink()
        path.write_text("fixture")
        directory = self.source / "web"
        (directory / "demo.html").unlink()
        directory.rmdir()
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            build_release(self.source, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_missing_source_or_invalid_version_is_rejected(self):
        for version in ("../bad", "", "v1/../../bad", "1\nsecret"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                build_release(self.source, self.root / "out", version)
        (self.source / "compose.yaml").unlink()
        with self.assertRaises(ValueError):
            build_release(self.source, self.root / "out")
        self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
    unittest.main()
