"""在源码外准备全新的部署目录；不会覆盖或轮换已有密钥。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.profile import get_profile


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")


def _env_quote(value):
    value = str(value)
    if (not value or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in ("$", "'", "\\"))):
        raise ValueError("配置不能含控制字符、首尾空白、单引号、反斜杠或 $ 字符")
    return "'" + value + "'"


def prepare(state_dir, *, owner_id="owner-1", principal_id="agent-api", workspace_id="shop-a",
            workspace_name="我的账本", profile="restaurant", timezone="Asia/Shanghai", port=8765):
    requested = Path(state_dir)
    if not requested.is_absolute():
        raise ValueError("state-dir 必须使用源码目录外的绝对路径")
    if os.path.lexists(requested):
        raise ValueError("部署目录已存在；不会覆盖目录、账本或轮换密钥")
    state = requested.resolve()
    if state.is_relative_to(ROOT):
        raise ValueError("部署目录必须位于源码目录外")
    if not state.parent.is_dir():
        raise ValueError("部署目录的父目录必须已存在")
    for label, value in (("owner", owner_id), ("principal", principal_id), ("workspace", workspace_id)):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"{label} 必须为 1 到 100 个字母、数字或 _.:- 字符")
    get_profile(profile)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("必须使用有效 IANA 时区") from exc
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口范围为 1 到 65535")
    uid, gid = (10001, 10001) if os.getuid() == 0 else (os.getuid(), os.getgid())
    values = {
        "SUIKOUJI_DATA_DIR": str(state / "data"),
        "SUIKOUJI_BACKUP_DIR": str(state / "backups"),
        "SUIKOUJI_API_KEY_PATH": str(state / "secrets" / "api_key"),
        "SUIKOUJI_UID": str(uid), "SUIKOUJI_GID": str(gid),
        "SUIKOUJI_HOST_PORT": str(port), "SUIKOUJI_OWNER_ID": owner_id,
        "SUIKOUJI_PRINCIPAL_ID": principal_id, "SUIKOUJI_WORKSPACE_ID": workspace_id,
        "SUIKOUJI_WORKSPACE_NAME": workspace_name, "SUIKOUJI_PROFILE_ID": profile,
        "SUIKOUJI_TIMEZONE": timezone,
    }
    contents = "# Docker Compose --env-file input; do not shell-source this file.\n"
    contents += "".join(f"{key}={_env_quote(value)}\n" for key, value in values.items())
    # Validate everything before creating the state directory. An interrupted
    # preparation keeps its new directory for manual inspection; never retry over it.
    state.mkdir(mode=0o700)
    for name in ("data", "backups", "secrets"):
        directory = state / name
        directory.mkdir(mode=0o700)
        if os.getuid() == 0:
            os.chown(directory, uid, gid)
    key_path = state / "secrets" / "api_key"
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(secrets.token_urlsafe(32) + "\n")
        output.flush()
        os.fsync(output.fileno())
        if os.getuid() == 0:
            os.fchown(output.fileno(), uid, gid)
    config = state / "compose.env"
    descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(contents)
        output.flush()
        os.fsync(output.fileno())
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description="为随口记准备新的仓外数据与密钥目录")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--owner-id", default="owner-1")
    parser.add_argument("--principal-id", default="agent-api")
    parser.add_argument("--workspace-id", default="shop-a")
    parser.add_argument("--workspace-name", default="我的账本")
    parser.add_argument("--profile", default="restaurant")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        config = prepare(args.state_dir, owner_id=args.owner_id, principal_id=args.principal_id,
                         workspace_id=args.workspace_id, workspace_name=args.workspace_name,
                         profile=args.profile, timezone=args.timezone, port=args.port)
    except (OSError, ValueError):
        print("准备失败：需要全新仓外目录、合法配置和写入权限；不要覆盖或删除已有部署目录", file=sys.stderr)
        return 1
    print(f"配置已创建：{config}")
    quoted = shlex.quote(str(config))
    print(f"docker compose --env-file {quoted} build")
    print(f"docker compose --env-file {quoted} up -d --wait")
    print("密钥保存在仓外文件中，不显示密钥。请在项目根目录执行命令。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
