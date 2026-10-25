"""用明确提供的部署配置验证本机 API；不输出密钥，不写入账目。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.request


def verify(config_path):
    values = {}
    for line in Path(config_path).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or len(value) < 2 or value[0] != "'" or value[-1] != "'":
            raise ValueError("只接受 prepare_deploy.py 生成的 Compose 配置")
        values[key] = value[1:-1]
    port = int(values["SUIKOUJI_HOST_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("端口范围不正确")
    token = Path(values["SUIKOUJI_API_KEY_PATH"]).read_text(encoding="ascii").removesuffix("\n")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("/healthz", "/readyz", "/v1/capabilities", "/v1/workspaces"):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if path.startswith("/v1/"):
            request.add_header("Authorization", "Bearer " + token)
        with opener.open(request, timeout=5) as response:
            body = json.loads(response.read(256000))
            if response.status != 200:
                raise ValueError("接口未成功")
        if path == "/healthz" and body.get("status") != "ok":
            raise ValueError("存活探针未成功")
        if path == "/readyz" and body.get("status") != "ready":
            raise ValueError("就绪探针未成功")
        if path == "/v1/capabilities" and body.get("contract_version") != "agent-1":
            raise ValueError("能力契约不符合预期")
        if path == "/v1/workspaces":
            if [row["id"] for row in body.get("workspaces", [])] != [values["SUIKOUJI_WORKSPACE_ID"]]:
                raise ValueError("工作空间范围不符合配置")
        print(f"OK {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读验证本机部署 API，不显示密钥")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        verify(args.config)
    except (OSError, ValueError, KeyError):
        print("API 验证失败，请检查配置、密钥权限、服务状态与端口", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
