"""SQLite staging 备份、恢复和完整性检查。

备份使用 SQLite 原生 backup API，而不是只导出业务 JSON；恢复总是先写入
同目录临时文件并做完整性/版本检查，成功后才原子替换目标文件。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from .store import SCHEMA_VERSION, database_schema_ready


def _readonly(path: str):
    if path == ":memory:":
        raise ValueError("不能把内存库作为运维源文件")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    uri = "file:" + quote(path) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def inspect_database(path: str, *, expected_version: int = SCHEMA_VERSION) -> dict:
    result = {"path": os.path.abspath(path), "ok": False, "reason": "database_unavailable"}
    try:
        conn = _readonly(path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            try:
                version = int(version_row[0]) if version_row else None
            except (TypeError, ValueError):
                version = None
            schema_ok = version == expected_version and database_schema_ready(conn, expected_version=expected_version)
            result.update({
                "integrity": integrity,
                "schema_version": version,
                "schema_ok": schema_ok,
                "ok": integrity == "ok" and schema_ok,
            })
            if not result["ok"]:
                result["reason"] = "database_not_ready"
            else:
                result.pop("reason", None)
            return result
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return result


def _backup_to(source: str, destination: str) -> None:
    src = _readonly(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def backup_database(source: str, destination: str) -> dict:
    """把 source 备份到 destination；目标不存在时也不会半成品可见。"""
    source = os.path.abspath(source)
    destination = os.path.abspath(destination)
    if source == destination:
        raise ValueError("备份源和目标不能相同")
    parent = os.path.dirname(destination) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".db", dir=parent)
    os.close(fd)
    try:
        os.chmod(temporary, 0o600)
        _backup_to(source, temporary)
        check = inspect_database(temporary)
        if not check["ok"]:
            raise ValueError("备份完整性或 schema 版本检查失败")
        os.replace(temporary, destination)
        return {**check, "path": destination}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore_database(backup: str, destination: str) -> dict:
    """将备份恢复到新目标；不会覆盖源备份或已有目标。"""
    destination = os.path.abspath(destination)
    if os.path.exists(destination):
        raise ValueError("恢复目标必须是不存在的新路径，避免覆盖正在使用的数据库")
    return backup_database(backup, destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description="随口记 SQLite 运维工具")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("database")
    copy = sub.add_parser("backup")
    copy.add_argument("source")
    copy.add_argument("destination")
    restore = sub.add_parser("restore")
    restore.add_argument("source")
    restore.add_argument("destination")
    args = parser.parse_args(argv)
    if args.command == "check":
        result = inspect_database(args.database)
    elif args.command == "backup":
        result = backup_database(args.source, args.destination)
    else:
        result = restore_database(args.source, args.destination)
    print(result)


if __name__ == "__main__":
    main()
