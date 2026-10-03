# maimemo 的 UGOS Pro 部署

本文是 NAS 管理界面的交付入口。项目总名固定为 `maimemo`，使用
`deploy/nas/compose.yaml` 管理 `migrate`、`server`、`mcp`、`worker` 四个服务；前三个长期
服务分别拉取 Server、MCP、Worker 镜像，迁移服务复用 Server 镜像。

## 目录与前置条件

1. 执行 `docker version`，确认 Server/Engine >=28.0.0，或取得 NAS 厂商明确回补 localhost
   端口发布漏洞的证据。两项均不满足时禁止启动应用和连接 Tunnel。官方风险说明：
   <https://docs.docker.com/engine/network/port-publishing/>。
2. PostgreSQL 15+ 已在 external `db_net` 中提供真实 Docker DNS；数据库与应用用户均为
   `maimemo`。先完成全库备份和空库恢复演练。
3. 项目目录包含 `compose.yaml`、本地 `.env`、`secrets/`、`var/` 和 `backup/`。不要把源码、
   Token、数据库口令、个人响应或 dump 放进公开记录。
4. 三个镜像必须来自同一版本和提交，并固定到发布后记录的 `sha256:` digest；首次切换不用
   `latest` 或 `stable` 猜测内容。

secret 源文件是 `secrets/maimemo_token` 和 `secrets/token_fingerprint_key`。容器使用 UID/GID
10001；两文件应由 10001 所有、权限 `0400`，父目录和 `var/` 为 `0700`，并核对 NAS ACL。
Compose file-source secret 不会替你重映射源文件 uid/gid/mode。

## UGOS 首次部署与检查

1. 在 UGOS Pro **Docker → 项目 → 创建/导入**，选择 `deploy/nas/compose.yaml`，项目名填写
   `maimemo`。
2. 复制 `.env.example` 为本地 `.env`，填写 PostgreSQL URL 和三个不可变镜像 digest。
3. 拉取镜像，先停止旧 Worker，再单独执行 `migrate upgrade head`。迁移失败立即停止，不得
   清表、删除或重建数据库。
4. 按 `server → mcp → worker` 启动。Server/MCP 就绪均要求 `alembic_version` 和
   `schema_metadata` 精确等于镜像唯一迁移头；Worker 等待当前结构，不自行迁移。
5. 核对 Server/MCP 健康、Worker 最近任务、连续失败数、三个镜像 digest 和日志。日志不得
   出现 secret、数据库 URL 或个人正文。
6. 从另一台同一 LAN 主机执行 `nc -vz -w 3 NAS_IP 8000`；必须拒绝或超时。HTTP 403 也表示
   端口可达，不能算通过；此检查不能替代 Engine/厂商回补门禁。

Worker 启动时并每六小时检查公开 OpenAPI，向 `var/openapi-drift.json` 原子写入公开
hash/时间/severity；MCP 只读该文件。MCP 端口只绑定 `127.0.0.1:8000`，不接公网反代。
当前私有部署不需要 Tunnel 凭据；未来原生 Tunnel Client 只连接
`http://127.0.0.1:8000/mcp`。

## 更新与回滚

更新前记录旧的三个 digest、全库备份和恢复演练结果。拉取同一新版本的三个镜像后，仍按
“停旧 Worker → migrate → server → mcp → worker”切换。任何一步失败都停止推进，恢复旧
Worker 和三个旧 digest；不要自动 downgrade、清表或删除数据库。

只有三镜像在 NAS 上完成验收后，才允许把该组三个 digest 一起提升为 `stable`。版本回退也
必须三者成套；若旧应用与当前 schema 不兼容，保持停机并恢复更新前整库备份，而不是强行
启动旧镜像。详细备份、恢复和验证命令见 `docs/operations.md`。
