# 交给部署 bot 的执行说明

任务：在用户已明确授权的服务器上部署随口记源码发布包，启动独立、带认证的记账 API。当前交付仅是本地发布包，不代表已获服务器操作授权。若没有目标主机访问权限，先向用户确认，不能自行选择服务器。

## 交付边界

- 先阅读本文件、[Compose 操作说明](README.md#docker-compose-发布包)和[Agent 能力契约](../docs/agent-capabilities.md)。当前使用 Bearer 认证 HTTP API；本包没有 MCP 服务或 Grok 专用插件。
- 发布包不含数据库和密钥；不得寻找或上传用户本机旧账本、微信账号或任何其他项目的配置。
- 只用新的源码目录和新的仓外状态目录。目录已存在时停止核对，不覆盖、不删除、不轮换密钥，不运行清空卷命令。已有数据库不能自动迁移。
- 默认 API_ONLY、单实例、宿主机回环端口、iLink 关闭。不要自行开启公网监听、扫码、iLink 或任务调度。需要远程访问由用户另行授权私网/VPN/TLS 反向代理。
- 密钥由准备脚本在目标机生成，保存在仓外 0600 文件中。不得输出、粘贴、上传密钥或把它加入模型提示词。报告密钥文件路径即可。
- 不安装或升级目标机 Docker、不重启其他服务，除非用户授权。缺前提时报告缺项。

## 执行步骤

1. 核对独立目标目录、端口和已有服务；需要 Python 3.10+、Docker Engine 和 Docker Compose v2.20+（含 `up --wait`）。以目标机器的实际用户运行，不要用 sudo 混合切换不同 Docker 上下文。准备脚本若由 root 执行会为容器使用 UID/GID 10001，普通用户执行则使用其 UID/GID。
2. 在解压前用同目录 `.sha256` 文件核对源码包；解压到新的空目录，在 `suikouji-版本/` 根目录运行 `sha256sum -c SHA256SUMS`。不要在已有部署上覆盖解压。
3. 如 shell 已设置 `SUIKOUJI_*`，先确认并清理冲突的旧变量：Compose 的宿主环境变量优先于 `--env-file`。选择新的状态目录；下列示例的 `/opt` 父目录需有写权限，普通用户可用其拥有的绝对目录。

```bash
python3 tools/prepare_deploy.py --state-dir /opt/suikouji-state --owner-id owner-1 --workspace-id shop-a --workspace-name 我的账本 --profile restaurant --timezone Asia/Shanghai
```

4. 只校验配置，不打印配置中的真实密钥；生成的 compose.env 本身只存路径与非秘密选项。

```bash
docker compose --env-file /opt/suikouji-state/compose.env config --quiet
```

5. 构建镜像。需要下载 Python 基础镜像及 Debian 时区/证书包；没有预构建镜像，不要假定离线服务器能直接构建。

```bash
docker compose --env-file /opt/suikouji-state/compose.env build
```

6. 启动唯一实例并等待 healthcheck。入口自动创建不存在的新数据库；已有库只读核对 schema、owner、workspace、profile、timezone，配置不符直接失败。

```bash
docker compose --env-file /opt/suikouji-state/compose.env up -d --wait --wait-timeout 90
```

7. 只读验证存活、就绪、认证能力接口和工作空间；该工具不写测试账目、不输出密钥，须由有权读取准备配置和密钥的用户执行。

```bash
python3 tools/verify_api.py --config /opt/suikouji-state/compose.env
```

8. 按[备份恢复说明](README.md#compose-备份与恢复)生成唯一的新备份、校验，并恢复到另一个新文件演练。不得替换当前运行数据库。检查 Compose health 为 healthy，根路径 `/` 返回 404 属于 API_ONLY 的正常行为，不是安装失败。

## 验收报告

报告源码版本、校验结果、镜像构建结果、容器 health、回环访问地址、工作空间 ID、状态/密钥文件路径（不含内容）、备份恢复演练结果、未验收项。部署失败保留数据并说明原因；不要通过删除状态目录、重新初始化或关闭认证掩盖问题。

服务器成功运行与 Agent 已获得记账工具分别验收。调用方需在自身执行层注入密钥、先读 `GET /v1/capabilities`，再按 [OpenAPI](../docs/openapi.yaml)调用。写入编号稳定重用，HTTP 200 的待确认/未解析状态不等于已入账。公网、真实 iLink 消息及扫码未经过本包离线验证。

当前发布环境没有 Docker/Podman；本包只经过标准库离线测试和静态 YAML 校验，容器实际构建、权限挂载和网络必须在目标环境验证。禁止把此说明当作已经部署成功的证据。
