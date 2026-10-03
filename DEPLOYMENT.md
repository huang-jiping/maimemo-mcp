# NAS 私有部署：UGOS Pro 项目

UGOS Pro 的 **Docker → 项目 → 创建/导入** 是首次部署、启动、停止及日常更新的入口。
交付源为仓库 [deploy/nas/compose.yaml](deploy/nas/compose.yaml) 和
[deploy/nas/.env.example](deploy/nas/.env.example)。使用同一份 Compose，不另维护文档副本。
项目名为 `maimemo-mcp`，只运行 `maimemo-mcp` 与 `maimemo-worker` 两个容器，正常为 `2 / 2`。
两者拉取 `ghcr.io/huang-jiping/maimemo-mcp:${IMAGE_TAG:-stable}`；MCP 启动前自动迁移，
Worker 等待 MCP 健康并核对 schema 后采集。复用已有 PostgreSQL 15+，数据库和应用用户均为
`maimemo`，经 external `db_net` 连接。MCP 只发布 `127.0.0.1:8000:8000`，不加入
`app_net`、不接 NPM、不开放公网入站。Worker 不提供 API，也不发布端口。

当前仅交付本地实现：首个公开 GHCR 镜像未发布，NAS/UGOS 实机未验证。完成发布验收、确认
包为 Public 且版本标签与 `stable` 能匿名拉取后才部署；不能把模板存在视为镜像已可用。

## 目录与前置条件

```text
/volume3/docker/maimemo-mcp/
├── compose.yaml         # 仓库 deploy/nas/compose.yaml
├── .env                 # 从 NAS .env.example 复制，私有配置
├── secrets/
│   ├── maimemo_token
│   └── token_fingerprint_key
├── data/                # Worker 可写；MCP 只读的公开漂移状态
└── backup/              # 敏感数据库与配置备份，不挂载到应用容器
```

UGOS 选择的项目存储目录、Compose、`.env` 和相对挂载目录须在一起。NAS 无需应用源码或
主机开发运行时。SSH 仅用于目录权限准备、受控诊断和灾难恢复。以下 shell 命令在 NAS 执行。
部署前核对 8000 端口与固定容器名无冲突，已有 external `db_net` 的成员可信且允许应用访问
墨墨 API。数据库管理员确认网络内真实 Docker DNS 别名；不要猜 IP 或宿主机端口。

```sh
docker version
docker network inspect db_net
```

查看 **Server / Engine** 版本，不是 Client/Compose。必须 **>=28.0.0**，或 NAS
**厂商明确回补** localhost 发布漏洞且有可核验公告/版本证据。Docker 官方说明旧版本可能
被同一 L2 主机访问回环发布端口，见 [端口发布说明](https://docs.docker.com/engine/network/port-publishing/)。
记录服务端版本与回补证据：**两项均不满足时禁止启动应用**，并**禁止连接 Tunnel**。
先升级或取得回补证据；LAN 地址连接失败、防火墙或 YAML 中的回环绑定不能替代该门禁。
网络检查失败时停止准备，由数据库管理员修复；项目不创建数据库和网络。

通过受控文件管理方式把交付文件放入项目目录，再准备目录权限：

```sh
cd /volume3/docker/maimemo-mcp
mkdir -p secrets data backup
chmod 0600 .env
sudo chown 1000:10 secrets data
sudo chmod 0700 secrets data
chmod 0700 backup
```

## 配置与 secret 权限

用户只在本地填写四项：`db_net` 上 PostgreSQL 的真实 Docker DNS、数据库密码、墨墨
Token secret 文件、长期稳定的 fingerprint key 文件。数据库和应用用户均为 `maimemo`。
NAS 示例 URL 的 `replace-with-db-net-dns.invalid` 与 `REPLACE_WITH_URL_ENCODED_PASSWORD`
必须替换；`:5432` 指容器内部端口，不是宿主机映射。密码中的 `@ : / %` 按标准 percent
编码；含 `$` 的完整 URL 在本地 `.env` 使用单引号。不要把真实值放进 Compose 编辑器、
Git、文档、工单或聊天；不要输出展开后的 `docker compose config`，只用 `config --quiet`。

`IMAGE_TAG=stable` 用于正常更新；不可变 `vX.Y.Z` 用于追溯与回退。`TZ` 与
`MAIMEMO_TIMEZONE` 默认上海时区。两个容器共享配置，secret 路径固定为
`/run/secrets/maimemo_token` 与 `/run/secrets/token_fingerprint_key`。
通过安全渠道创建两份 UTF-8、非空文件，仅放值本身，可有一个末尾换行。

镜像与 Compose 固定 **UID 1000 / GID 10**，源文件权限必须在 NAS 落实：

```sh
sudo chown 1000:10 secrets/maimemo_token secrets/token_fingerprint_key
sudo chmod 0400 secrets/maimemo_token secrets/token_fingerprint_key
stat -c '%u:%g %a %n' secrets secrets/maimemo_token secrets/token_fingerprint_key data
```

核对 NAS ACL：UID 1000 可遍历父目录并读取 secret，其他非管理员主体无访问；GID 10 可能
对应 NAS 管理员组，所以不开放组读取。Compose file secret 是只读挂载，`uid/gid/mode`
不能重映射源文件权限，见 [Compose secret 文档](https://docs.docker.com/reference/compose-file/services/#secrets)。
UGOS 容器终端执行 `id`、`test -r /run/secrets/maimemo_token`、
`test ! -w /run/secrets/maimemo_token`，再对 fingerprint 文件重复检查，保存退出码；
不要读取/输出内容。首次启动的 secret 前置校验还验证 UTF-8 和非空，失败必须停项目修复。

`data/` 为 UID 1000 / GID 10、`0700`；Worker 读写挂载，MCP 只读挂载。Worker 启动时
立即检查 OpenAPI，以后每六小时检查；原子发布的 `openapi-drift.json` 为 `0644`，仅含公开
规范 hash、时间和 severity。不要向状态目录写入秘密。日志走 Docker json-file，最多
3 × 10 MB/容器，容器根文件系统只读；备份目录完全不挂载给应用。

## UGOS 首次部署与检查

1. 完成上述 Engine、网络、文件与 ACL 门禁，确认公开镜像可匿名拉取且为 `linux/amd64`。
2. 在 UGOS Pro **Docker → 项目 → 创建/导入**，名称填 `maimemo-mcp`，选择上述存储目录，
   导入交付 `compose.yaml`。确认 UGOS 使用该目录的本地 `.env`，不将口令粘贴进 YAML。
3. 若需要配置预检，在此目录执行 `docker compose config --quiet`；只保存成功/失败状态。
4. 在项目页面拉取并启动。MCP 先验证配置和 secret，再自动迁移、精确核验
   `alembic_version` 与 `schema_metadata`。Worker 通过 `service_healthy` 等待，应用级
   schema 门禁再次检查；不需在每次启动前人工迁移。
5. MCP 健康检查宽限为 6 分钟，涵盖默认连接/锁/迁移语句预算。改变超时或引入多条耗时
   迁移时需同步复核宽限。超时、数据库故障或权限错误会安全退出并重启；持续失败时停止
   整个项目，查安全错误类别并修复，不绕过门禁。
6. 项目应显示 **2 / 2**，查看两个容器日志与 MCP healthy 状态；确认 Worker 最近成功
   采集时间与连续失败数。首次采集尚未完成时不能把容器运行等同于已有学习数据。

NAS 本机只读检查：

```sh
curl --fail --silent http://127.0.0.1:8000/health/ready
curl --fail --silent http://127.0.0.1:8000/health/status
```

记录两个容器实际镜像 digest、对应版本/完整 Git SHA、数据库 revision 与验收时间；两者
必须使用同一 digest。若 UGOS 不显示 RepoDigest，可用受控 SSH 查询
`docker image inspect` 的 RepoDigests/OCI labels，禁止打印容器环境变量或私有 URL。
实机还需确认 UGOS 支持长格式 `depends_on` 与资源限制；应用门禁不能替代该项验收。

以下仅为纵深验收，**不能替代版本/回补门禁**，**不覆盖旧版 localhost 发布漏洞**。
本机健康可达后，从**另一台同一 LAN**、同一 L2 主机，把 `NAS_IP` 替换为真实 LAN 地址，
验证 **NAS_IP:8000 不可达**：

```sh
nc -vz -w 3 NAS_IP 8000
```

预期拒绝或超时。不能将 HTTP 403、404 或 Host 防护拒绝算通过，它们说明 TCP 已可达。
工具缺失、地址错误或测试机无法访问 NAS 不能算通过，先验证一个已知允许的 NAS 服务。
有多个 LAN 接口时逐一核对；记录测试主机、地址、时间和结果。失败则停止应用，
**不得连接 Tunnel** 或宣称完成私有部署。

## Tunnel 与漂移状态

当前私有部署不需要 Tunnel 凭据，也没有 Tunnel sidecar。后续接入按
[Tunnel 说明](docs/tunnel-setup.md) 重新核验官方客户端、供应链与平台权限，原生 NAS
客户端连接 `http://127.0.0.1:8000/mcp`，凭据仅在 NAS 本地保存。Engine 和 LAN 门禁未
通过时禁止连接。Tunnel 失联不影响 Worker；不新增 NPM、公网转发或通配 Host。

`/health/status` 中的 `drift_status` 为 high 时优先调查，informational 时安排审阅；
unavailable/stale 时看 Worker 日志、目录 ACL 与最近检查时间，超过 26 小时即 stale。
漂移检查失败只输出安全类别，下一周期重试，不停止学习采集。由现有 NAS 告警渠道处理。

## 升级、回滚与备份

正常兼容更新：先审阅发布说明，在现有 PostgreSQL/pgAdmin 工作流备份整个 `maimemo`
数据库并完成恢复演练，记录当前版本、digest、Git SHA、`alembic_version`、
`schema_metadata`、备份路径和演练结果。随后在 UGOS 项目页面一键拉取/更新/重建，
保留本地 `.env`、secret 和持久目录。两个服务配置 `pull_policy: always`，还须核对它们
实际使用同一新 digest、`2 / 2`、健康、日志与新采集，并重验 LAN 隔离。一键操作不省略
备份和验收，不保证零停机。

破坏性或与上一运行版本不兼容的迁移走维护升级：**先停止整个项目**，确认两个容器都
停止，完成并验证数据库备份，再在 UGOS 修改版本并重建。不能让旧 Worker 在新 schema
迁移期间继续运行。若发布说明未证明兼容性，按维护升级处理。

镜像回退只适用于已经验证的版本/schema 组合；精确 schema 门禁会拒绝未知新 revision，
因此不能仅凭版本号推断。停止整个项目，将本地 `.env` 的 `IMAGE_TAG` 改为上一不可变
`vX.Y.Z`，在 UGOS 拉取并重建，核对记录的旧 digest 与所有检查。如果 schema 不兼容，
**镜像不能单独回退**：保持项目停止，用 PostgreSQL/pgAdmin 恢复更新前完整数据库，
核对 revision 和数据，再启用对应旧镜像。自动启动永不 downgrade、清表或恢复备份。

使用已有 PostgreSQL/pgAdmin 的 Backup/Restore，选择整个 `maimemo` 数据库（不是仅
选某张表、`data/` 或 schema-only），优先 custom dump，由数据库管理员保留所有者/权限
信息并确认用户 `maimemo` 的 DDL 与读写权限。备份文件用唯一名称，避免覆盖已有备份，
置于 `backup/`（0700/受限 ACL）并加密复制至 NAS 外部介质。恢复演练在独立新空库中
进行，不覆盖生产库；检查关键表、行数、抽样 hash、反馈撤销链、隔离失败证据和两份
revision 元数据。详细步骤见 [运维手册](docs/operations.md#4-postgresql-备份)。

生产灾难恢复需明确确认精确目标后由数据库管理员处理；禁止与运行中的项目并行恢复。
另行加密备份 `.env`、两份 secret（fingerprint key 必须稳定）、Compose 和必要状态文件。
这些文件及 dump 含秘密/个人数据，不进 Git、镜像或聊天。
