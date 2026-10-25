"""只打包明确列出的公共源码；不读取或遍历账本、密钥和本地配置。"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1-staging"
FILES = (
    "Dockerfile", "compose.yaml", ".dockerignore", ".gitignore", "README.md", "CONTEXT.md",
    "engine/__init__.py", "engine/accounting.py", "engine/agent_service.py", "engine/agent_normalization.py", "engine/backup.py",
    "engine/capabilities.py", "engine/cli.py", "engine/cost.py", "engine/deployment.py",
    "engine/importer.py", "engine/model.py", "engine/observe.py", "engine/parser.py",
    "engine/profile.py", "engine/reconcile.py", "engine/report.py", "engine/review.py",
    "engine/runtime.py", "engine/sheet.py", "engine/store.py", "engine/web.py",
    "channel/__init__.py", "channel/aes.py", "channel/bot.py", "channel/ilink.py",
    "web/demo.html", "deploy/README.md", "deploy/.env.example", "deploy/compose.env.example",
    "deploy/BOT_DEPLOY.md", "docs/openapi.yaml", "docs/agent-capabilities.md", "docs/report-output-design.md",
    "docs/accounting-v1.md", "docs/review-and-correction.md", "docs/parser-boundaries.md",
    "docs/ilink-protocol-notes.md", "docs/adr/0001-legacy-shop-as-workspace.md",
    "docs/engineering/lessons.md", "tools/prepare_deploy.py", "tools/build_release.py",
    "tools/verify_api.py", "tests/test_accounting.py", "tests/test_aes.py",
    "tests/test_agent_atomicity.py", "tests/test_agent_bot.py", "tests/test_agent_final_regressions.py",
    "tests/test_agent_http_contract.py", "tests/test_agent_legacy_recovery.py", "tests/test_agent_normalization.py",
    "tests/test_agent_preview.py", "tests/test_agent_store_compat.py", "tests/test_bot_delivery.py",
    "tests/test_deployment.py", "tests/test_engine.py", "tests/test_foundation.py", "tests/test_ilink.py",
    "tests/test_observe.py", "tests/test_parser_boundaries.py", "tests/test_prepare_deploy.py",
    "tests/test_release_package.py", "tests/test_review.py", "tests/test_runtime_backup.py",
    "tests/test_runtime_lifecycle.py", "tests/test_schema_readiness.py", "tests/test_web.py",
    "tests/fixtures/schema-v1.sql",
)


def _read_source(root, relative):
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"发布源码不能包含符号链接：{relative}")
    if not path.is_file():
        raise ValueError(f"缺少发布文件：{relative}")
    return path.read_bytes()


def _archive_bytes(contents, prefix):
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as zipped:
        with tarfile.open(mode="w", fileobj=zipped, format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(contents.items()):
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(data)
                info.mode = 0o644
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def build_release(source=ROOT, output_dir=None, version=VERSION):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][a-zA-Z0-9._-]{0,63}", version):
        raise ValueError("发布版本不是安全的文件名")
    root = Path(source).absolute()
    if root.is_symlink():
        raise ValueError("发布源码根目录不能是符号链接")
    contents = {relative: _read_source(root, relative) for relative in FILES}
    manifest = {
        "product": "随口记经营助手", "version": version,
        "deployment": "authenticated-http-api", "ilink_enabled_by_default": False,
        "contains_user_data": False, "contains_credentials": False,
        "verification_boundary": "Offline tests only; Docker build/run and live server/iLink acceptance required.",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())},
    }
    contents["RELEASE.json"] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    contents["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items())
    ).encode()
    prefix = f"suikouji-{version}"
    data = _archive_bytes(contents, prefix)
    output = Path(output_dir) if output_dir is not None else root / "dist"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{prefix}.tar.gz"
    checksum = output / (archive.name + ".sha256")
    if os.path.lexists(archive) or os.path.lexists(checksum):
        raise ValueError("发布目标已存在；请使用新输出目录或版本，不会覆盖")
    digest = hashlib.sha256(data).hexdigest()
    # Both paths are created exclusively, including in a concurrent build.
    descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(data)
        destination.flush()
        os.fsync(destination.fileno())
    descriptor = os.open(checksum, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="ascii") as destination:
        destination.write(f"{digest}  {archive.name}\n")
    return archive, checksum


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建不含账本和密钥的随口记源码发布包")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--version", default=VERSION)
    args = parser.parse_args(argv)
    try:
        archive, checksum = build_release(output_dir=args.output_dir, version=args.version)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
