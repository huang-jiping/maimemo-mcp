# NAS 私有部署

部署只运行 `maimemo-mcp`（Streamable HTTP MCP）和 `maimemo-worker`（定时采集进程），
复用 NAS 已有 PostgreSQL 15+。Worker 不提供后端 API。本方案不包含前端、普通 REST API、
Redis 或数据库容器。ChatGPT/Codex 入口继续使用 Secure MCP Tunnel；客户端原生运行于 NAS。
MCP 配置为发布 `127.0.0.1:8000:8000`，须通过下面的 Docker Engine 修复门禁和网络验收
才可认定主机回环隔离有效；不接 NPM、不加入 `app_net`、不开放公网入站。

## 目录与前置条件

```text
/volume3/docker/maimemo-mcp/
├── app/                 # 仓库源码；不移动仓库内部结构
├── compose.yaml         # 从 app/deploy/nas/compose.yaml 复制
├── .env.example         # 从 app/deploy/nas/.env.example 复制
├── .env                 # 私有配置，不提交
├── secrets/
│   ├── maimemo_token
│   └── token_fingerprint_key
├── data/                # OpenAPI 漂移状态，容器只读
└── backup/              # 敏感备份，限制访问
```

以下命令在 NAS 的 POSIX shell 执行。需要 Docker Compose v2、Git；NAS 原生运维脚本
需要 Python 3.12 和锁定依赖（`uv sync --frozen`）。数据库管理员必须提前提供已存在的
external `db_net`、该网络上的 PostgreSQL DNS 名，以及独立最小权限用户和数据库。
应用需要出站访问墨墨 API，不能把 `db_net` 配成阻断出站的网络。

部署前在 NAS 上执行：

```sh
docker version
```

查看输出的 **Server / Engine** 版本，不是 Client 或 Compose 版本。Docker Engine
**>=28.0.0**，或 NAS **厂商明确回补** localhost 发布端口修复且有可核对的公告/版本证据，
才可依赖回环绑定。记录服务端版本与回补证据。Docker 官方文档说明低于 28.0.0 时，
同一 L2 网段的其他主机可能访问发布到 localhost 的端口，见
[端口发布说明](https://docs.docker.com/engine/network/port-publishing/)。

上述版本或明确回补证据是必须通过的生产门禁：**两项均不满足时禁止启动应用**，
并**禁止连接 Tunnel**。先升级到受支持版本或取得可核对的厂商回补证据，再继续部署。
普通 LAN 地址连接失败、主机防火墙规则或仅写了 `127.0.0.1` 都不能替代此门禁。
项目不提供自定义路由/防火墙绕过方案，也不自动判断厂商版本或修改 NAS 防火墙。

```sh
docker network inspect db_net
mkdir -p /volume3/docker/maimemo-mcp
cd /volume3/docker/maimemo-mcp
git clone git@github.com:huang-jiping/maimemo-mcp.git app
cp app/deploy/nas/compose.yaml compose.yaml
cp app/deploy/nas/.env.example .env.example
cp .env.example .env
mkdir -p secrets data backup
chmod 0700 secrets backup
chmod 0755 data
chmod 0600 .env
```

`docker network inspect` 失败时停止，先联系数据库管理员检查网络。项目不会创建 `db_net`。
仓库根 `compose.yaml` 保留给通用/开发用途，NAS 使用外层模板；不要在 `app/` 内启动根配置。
固定 container_name 要求同一 NAS 只运行这一套实例，先排查同名容器和 8000 端口占用。

## 配置与 secret 权限

通过受控编辑器编辑外层 `.env`。`MAIMEMO_DATABASE_URL` 必须使用
`postgresql+psycopg`，主机名替换为 `db_net` 上真实 PostgreSQL DNS 名，不能使用
`localhost` 或固定容器 IP。示例仅给无凭据的 `postgresql+psycopg://postgresql/maimemo`；
账户/密码只填写在私有 `.env`，密码中的 `@ : / %` 需要标准 percent 编码。
Compose `.env` 支持插值；含 `$` 的完整 URL 应用单引号包裹。不要输出完整
`docker compose config`，它会展开数据库凭据，使用 `config --quiet`。

`IMAGE_TAG` 是本地镜像标签，默认 `dev`；正式升级选唯一版本号。`TZ` 为容器时区，
`MAIMEMO_TIMEZONE` 为应用调度时区。采集间隔、日志级别按模板已有变量设置。
secret 容器路径固定为 `/run/secrets/maimemo_token` 和
`/run/secrets/token_fingerprint_key`，漂移文件固定为
`/var/lib/maimemo/openapi-drift.json`；不要改成宿主机路径。

通过安全渠道创建两份文件，只放 Token/随机 fingerprint key 本身，不在 YAML、镜像构建
参数、文档或 Git 写入真实值。fingerprint key 需要长期稳定并单独安全备份。
应用镜像运行 UID 1000 / GID 10；基础镜像已有 GID 10，构建只创建 UID 1000 用户。

```sh
sudo chown 1000:10 secrets/maimemo_token secrets/token_fingerprint_key
sudo chmod 0400 secrets/maimemo_token secrets/token_fingerprint_key
sudo chown 1000:10 secrets
sudo chmod 0700 secrets
```

NAS ACL 必须允许 UID 1000 读取 secret、遍历父目录，移除其他非管理员主体的访问权限。
GID 10 可能对应 NAS 管理员组，避免组可读：保持文件 `0400` 并核对扩展 ACL。
Compose 文件来源 secret 的 `uid/gid/mode` 不会重映射宿主机权限，必须检查真实源文件。
root 和平台管理员仍能管理文件。审计脚本不会输出 secret 内容。

## 构建、迁移、启动和检查

所有 Compose 命令在 `/volume3/docker/maimemo-mcp` 执行：

```sh
docker compose config --quiet
docker compose build --pull
```

配置本机脚本环境后，审计**外层** Compose 和两份生产文件：

```sh
cd /volume3/docker/maimemo-mcp/app
uv sync --frozen
uv run python scripts/check_compose_secrets.py \
  --compose-file /volume3/docker/maimemo-mcp/compose.yaml \
  --token-file /volume3/docker/maimemo-mcp/secrets/maimemo_token \
  --fingerprint-key-file /volume3/docker/maimemo-mcp/secrets/token_fingerprint_key \
  --image local/maimemo-mcp:dev
cd /volume3/docker/maimemo-mcp
```

将审计的镜像标签与 `.env` 的 `IMAGE_TAG` 保持一致。结果必须为 UID 1000 / GID 10
读取两份文件成功、写入均失败。`--synthetic` 只检查合成文件，不能替代生产 ACL 检查。
确认版本/回补门禁已通过后，才可对外部数据库迁移并启动：

```sh
docker compose run --rm --no-deps --entrypoint /opt/venv/bin/python maimemo-mcp -m alembic upgrade head
docker compose up -d
docker compose ps
curl --fail --silent http://127.0.0.1:8000/health/ready
curl --fail --silent http://127.0.0.1:8000/health/status
docker compose logs --since 30m maimemo-mcp maimemo-worker
```

以下检查仅为已通过版本/回补门禁后的纵深验收，**不能替代版本/回补门禁**，
**不覆盖旧版 localhost 发布漏洞**。普通 NAS LAN 地址连接失败不能证明旧版的 L2 绕过
路径已修复，也不能作为启动未修复版本的许可。
确认 NAS 本机 `/health/ready` 可达后，从**另一台同一 LAN**、同一 L2 网段的主机，
把下面 `NAS_IP` 替换为 NAS 的真实 LAN 地址，验证 **NAS_IP:8000 不可达**：

```sh
nc -vz -w 3 NAS_IP 8000
```

预期 TCP 连接拒绝或超时；连接成功即验收失败。不能将 HTTP 403、404 或 Host 防护拒绝
当成网络隔离通过：这些响应说明端口已可达。工具缺失、地址错误或测试机本身无法访问
NAS 也不能算通过；先确认测试机能访问 NAS 的其他已知允许服务。NAS 有多个 LAN 接口时
逐一核对。记录 Engine 版本/回补证据、测试主机、目标地址、时间和实际结果；
失败时停止应用并检查隔离规则，**不得连接 Tunnel** 或宣称完成私有部署。

MCP 就绪检查真实查询 PostgreSQL。Worker 通过结构化日志、`ingestion_run` 和 MCP 数据健康
工具观察采集结果，没有 HTTP 健康端口。检查最近采集时间、连续失败数和迁移版本。
首次采集可能尚未完成；不要把进程运行或健康就绪当成学习数据已采集完成。

## Tunnel 与漂移状态

原生 NAS Tunnel Client 的 MCP 地址为 `http://127.0.0.1:8000/mcp`；按
[Tunnel 接入说明](docs/tunnel-setup.md) 检查官方客户端版本、配置和平台权限。
不要新增反向代理、公网端口转发或扩大 Host allowlist。Tunnel 失联不影响 Worker。

NAS 调度器每 6 小时以源码目录为工作目录运行（主机写入，应用只读）：

```sh
cd /volume3/docker/maimemo-mcp/app
uv run python scripts/check_openapi_drift.py --pinned openapi/maimemo-api.yaml --remote https://open.maimemo.com/api_bundle.yaml --state-file /volume3/docker/maimemo-mcp/data/openapi-drift.json
```

退出码 1/2 分别表示高危漂移/检查失败，应由现有 NAS 告警处理。确保 UID 1000 可遍历
`data/` 且能读取每次原子替换后的文件（writer 发布为 `0644`）。状态仅包含公开规范元信息，
不能把 secret 放入 `data/`。超过 26 小时未更新会显示 stale。

## 升级、回滚与备份

升级前保存当前源码提交、外层模板、镜像 ID/标签并做数据库备份与空库恢复演练。
将 `app/` 更新到已审阅的具体提交；对比新模板后再更新外层配置，保留私有 `.env` 和 secret。
给 `.env` 设置新 `IMAGE_TAG`，执行 `config --quiet`、build、secret 审计和数据库迁移，
再 `docker compose up -d` 并核对健康、日志、采集时间。Docker Engine、NAS 网络或防火墙
变更后重新检查修复门禁与跨 LAN 不可达验收。不要先删旧镜像。

应用回滚：把 `IMAGE_TAG` 改为保留的上一版本标签，核对其记录的镜像 ID，执行
`docker compose up -d --no-build`，然后检查健康和采集。镜像构建上下文仍是 `app/`，
回滚时禁止重新构建旧标签。数据库迁移回退必须独立审查，不得用清表绕过降级保护；
若新代码依赖新 schema，单独回退镜像不一定可行。

数据库由外部 PostgreSQL 的既有备份机制负责，备份整个应用数据库及迁移版本，不能只备份
`data/`。项目原生备份/恢复脚本、版本要求与空库保护见
[详细运维手册](docs/operations.md#4-postgresql-备份)。主机不能解析 Docker DNS 时，
备份连接使用数据库管理员提供的主机可达私有地址；不要照搬应用容器 DNS 到主机脚本。
备份 URL 由受控环境变量注入，不能放在命令行或打印到日志。备份输出放
`/volume3/docker/maimemo-mcp/backup/`，脚本拒绝覆盖已有 `.dump`。

另行加密备份 `.env`、两份 secret、外层 Compose 和必要的 `data/`，保持访问限制，按保留
策略复制到 NAS 外部介质。数据库 dump 含个人数据；备份不能加入 Git 或镜像。
定期恢复到独立空库并验证后再认定备份可用。
