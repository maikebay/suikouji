"""容器入口：显式创建新库，重启只读校验，密钥从挂载文件加载。"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import runtime
from .backup import backup_database, inspect_database
from .profile import get_profile
from .store import SCHEMA_VERSION, SqliteStore, database_schema_ready, migrate_v5_to_v6


ROOT = Path(__file__).resolve().parent.parent


def _value(env, name, default=""):
    value = env.get(name, default)
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"必须配置有效的 {name}")
    return value


def _database_path(env):
    path = Path(_value(env, "SUIKOUJI_DB"))
    if not path.is_absolute() or path.resolve().is_relative_to(ROOT):
        raise ValueError("部署数据库必须使用源码目录外的绝对路径")
    if path.is_symlink():
        raise ValueError("部署数据库不能是符号链接")
    return path


def _workspace_config(env):
    owner = _value(env, "SUIKOUJI_OWNER_ID")
    workspace = _value(env, "SUIKOUJI_WORKSPACE_ID")
    name = _value(env, "SUIKOUJI_WORKSPACE_NAME", "我的账本")
    profile = _value(env, "SUIKOUJI_PROFILE_ID", "restaurant")
    timezone = _value(env, "SUIKOUJI_TIMEZONE", "Asia/Shanghai")
    get_profile(profile)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("时区必须是已安装的 IANA 时区名称") from exc
    return owner, workspace, name, profile, timezone


def initialize_database(env, *, allow_existing=False):
    """发布入口仅初始化不存在的文件；已有库不构造会迁移的 Store。"""
    path = _database_path(env)
    owner, workspace, name, profile, timezone = _workspace_config(env)
    if path.exists():
        if not allow_existing:
            raise ValueError("数据库已存在，初始化不会覆盖或迁移")
        if not runtime.probe_database(str(path), owner, workspace)["ok"]:
            raise ValueError("已有数据库未就绪，必须先备份并显式处理，不能自动迁移")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            actual = conn.execute(
                "SELECT profile_id, timezone FROM workspace_meta WHERE shop_id=? AND owner_id=?",
                (workspace, owner),
            ).fetchone()
        if actual != (profile, timezone):
            raise ValueError("已有工作空间的行业或时区与部署配置不一致，请核对配置")
        return {"status": "existing", "workspace_id": workspace}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".init-", suffix=".db", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(SqliteStore(str(temporary))) as store:
            store.ensure_shop(workspace, name, owner_id=owner, profile_id=profile, timezone=timezone)
        if not runtime.probe_database(str(temporary), owner, workspace)["ok"]:
            raise ValueError("新数据库初始化校验失败")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # A hard link publishes the fully initialized file without replacing an
        # existing destination, even if another initializer won the race.
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "created", "workspace_id": workspace}


def migrate_database(source: str, destination: str) -> dict:
    """将完整 v5 库备份到新路径后迁移副本；永不修改或覆盖源库。"""
    source_path, destination_path = Path(source).absolute(), Path(destination).absolute()
    if source == ":memory:" or not source_path.is_file() or source_path.is_symlink():
        raise ValueError("迁移源必须是现有数据库文件")
    if os.path.lexists(destination_path):
        raise ValueError("迁移目标必须是不存在的新路径")
    destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".migrate-", suffix=".db",
                                                dir=destination_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        # backup includes committed WAL pages, unlike a filesystem copy. Source
        # is opened read-only and never passed through a migrating constructor.
        with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source_conn, \
                closing(sqlite3.connect(temporary)) as target_conn:
            if not database_schema_ready(source_conn, expected_version=5):
                raise ValueError("迁移源必须是完整的 v5 数据库")
            source_conn.backup(target_conn)
            target_conn.execute("PRAGMA foreign_keys=ON")
            migrate_v5_to_v6(target_conn)
            # Publish a self-contained main file, including when source uses WAL.
            target_conn.execute("PRAGMA journal_mode=DELETE")
        check = inspect_database(str(temporary))
        if not check["ok"] or not runtime.probe_database(str(temporary))["ok"]:
            raise ValueError("迁移副本校验失败")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, destination_path)
        directory_fd = os.open(destination_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {**check, "path": str(destination_path), "status": "migrated",
                "source_schema_version": 5, "schema_version": SCHEMA_VERSION}
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def load_api_key(env):
    secret_path = env.get("SUIKOUJI_API_KEY_FILE", "")
    direct = env.get("SUIKOUJI_API_KEY", "")
    if secret_path and direct:
        raise ValueError("API key 文件与环境变量不能同时配置")
    if secret_path:
        path = Path(secret_path)
        if not path.is_absolute():
            raise ValueError("API key 文件必须使用绝对路径")
        try:
            with path.open("rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("API key 文件必须是普通文件")
                raw = source.read(513)
            if len(raw) > 512:
                raise ValueError("API key 文件过长")
            # The preparation tool writes one terminating newline, never spaces.
            direct = raw.decode("ascii").removesuffix("\n")
        except (OSError, UnicodeError) as exc:
            raise ValueError("无法读取 API key 文件") from exc
    if (not isinstance(direct, str) or not 16 <= len(direct) <= 512
            or not direct.isascii() or any(char.isspace() for char in direct)
            or any(ord(char) < 33 or ord(char) > 126 for char in direct)):
        raise ValueError("API key 必须为至少 16 字符的无空白 ASCII 密钥")
    return direct


def serve(env=None):
    config_env = dict(os.environ if env is None else env)
    config_env["SUIKOUJI_API_KEY"] = load_api_key(config_env)
    config = runtime.RuntimeConfig.from_env(config_env)
    config.validate()
    if not config.api_only:
        raise ValueError("部署入口要求 SUIKOUJI_API_ONLY=1")
    _, _, _, _, timezone = _workspace_config(config_env)
    os.environ["TZ"] = timezone
    if hasattr(time, "tzset"):
        time.tzset()
    initialize_database(config_env, allow_existing=True)
    os.umask(0o077)
    runtime.run(config)


def healthcheck(env=None):
    config_env = os.environ if env is None else env
    port = int(config_env.get("SUIKOUJI_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("端口范围不正确")
    # Ignore ambient proxy settings for the container's own readiness probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/readyz", timeout=3) as response:
        data = json.loads(response.read(4096))
        if response.status != 200 or data.get("status") != "ready":
            raise ValueError("服务未就绪")


def main(argv=None):
    parser = argparse.ArgumentParser(description="随口记容器部署与运维入口")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("init")
    commands.add_parser("healthcheck")
    check = commands.add_parser("check")
    check.add_argument("database")
    for command in ("backup", "restore", "migrate"):
        child = commands.add_parser(command)
        child.add_argument("source")
        child.add_argument("destination")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve()
            return 0
        if args.command == "init":
            result = initialize_database(os.environ)
        elif args.command == "healthcheck":
            healthcheck()
            return 0
        elif args.command == "check":
            result = inspect_database(args.database)
        elif args.command == "migrate":
            result = migrate_database(args.source, args.destination)
        else:
            # Publish operations to a new path without a check/replace race.
            destination = Path(args.destination).absolute()
            if os.path.lexists(destination):
                raise ValueError("运维目标必须是不存在的新路径")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".deploy-copy-", suffix=".db",
                                                           dir=destination.parent)
            os.close(descriptor)
            try:
                result = backup_database(args.source, temporary_name)
                os.link(temporary_name, destination)
                result["path"] = str(destination)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1
    except (OSError, ValueError, sqlite3.Error, urllib.error.URLError):
        # A traceback may contain configuration paths or credential material.
        # Individual commands deliberately fail closed without printing secrets.
        print("部署操作失败：请核对配置、文件权限、数据库就绪及目标是否已存在", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
