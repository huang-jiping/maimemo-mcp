# 运维手册

本文针对用户自有 NAS 上的私有部署。Compose 只运行同一只读应用镜像的 `mcp` 与
`worker` 两个命令；PostgreSQL 必须是 NAS 上已有的外部 PostgreSQL 15+。默认不发布
MCP 端口，网络仍保留出站能力以访问墨墨 API。

## 1. 部署前准备

1. 为应用创建独立、最小权限的 PostgreSQL 用户和数据库，不复用管理员账号。
2. 在仓库外或被 `.gitignore` 排除的 `secrets/` 目录创建两个仅管理员可读的文件：
   `maimemo_token` 与随机生成的 `token_fingerprint_key`。文件只放值本身，可有一个末尾换行。
3. 创建 `var/`，供运维任务生成的 OpenAPI 漂移状态文件使用；容器以只读方式挂载。
4. 设置 `MAIMEMO_DATABASE_URL`。允许 SQLAlchemy 的
   `postgresql+psycopg://user:password@host:5432/database` 形式。

不要把 `.env`、数据库口令、Token、fingerprint key 或真实个人响应放入构建目录、镜像
参数、Compose YAML、工单或 Git。

## 2. 构建、迁移和启动

```text
docker compose config
docker compose build --pull
docker compose run --rm --entrypoint /opt/venv/bin/python maimemo-mcp -m alembic upgrade head
docker compose up -d
docker compose ps
```

镜像使用固定 digest 的 Python 3.12 基础镜像、锁定的 `uv.lock` 和
`uv sync --frozen --no-dev`。运行用户为 UID/GID 10001，根文件系统只读，移除全部 Linux
capabilities。`restart: unless-stopped` 只负责进程意外退出后的重启；MCP 的就绪检查真实
查询 PostgreSQL。Worker 没有伪造的健康检查：其采集成败通过结构化日志、
`ingestion_run` 和 MCP 数据健康工具观察。

`maimemo-private` 是未发布端口的 Compose bridge。不能设为 Docker `internal: true`，否则
会阻断墨墨 API 的必要出站连接。默认只有同一 Docker 网络内的进程可访问
`http://maimemo-mcp:8000/mcp`。

健康检查：

```text
docker compose exec maimemo-mcp /opt/venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read().decode())"
docker compose logs --since 30m maimemo-mcp maimemo-worker
```

日志只应包含 allowlist 字段。若发现凭据或个人正文，立即停止服务、轮换相关凭据并保留
不含敏感值的事件时间和镜像 digest 用于调查。

## 3. 原生 NAS Tunnel Client 的本机访问

默认 Compose 没有 `ports`。只有在 Tunnel Client 作为 NAS 主机上的受监督原生进程运行时，
创建一个不提交到 Git 的 `compose.tunnel-local.yaml`：

```yaml
services:
  maimemo-mcp:
    ports:
      - "127.0.0.1:8000:8000"
```

然后使用两份 Compose 文件启动，并让 Tunnel Client 连接
`http://127.0.0.1:8000/mcp`。不得改成 `0.0.0.0:8000:8000`。若 Tunnel Client 在同一
Docker 网络内，可直接使用 `http://maimemo-mcp:8000/mcp`，但镜像与版本必须先按
`docs/tunnel-setup.md` 核验。

## 4. PostgreSQL 备份

生产环境优先在受控 NAS 主机安装与服务器主版本兼容的 `pg_dump`、`pg_restore`、
`createdb` 和 `psql`。脚本只用参数数组、`shell=False`、`check=True` 调用工具；数据库
URL 从环境变量读取，不接收 URL 命令行参数。

```text
export MAIMEMO_BACKUP_DATABASE_URL='postgresql://app_user:...@db.internal:5432/maimemo'
python scripts/backup_postgres.py --output /backups/maimemo-2026-10-02.dump
```

Windows PowerShell 使用：

```powershell
$env:MAIMEMO_BACKUP_DATABASE_URL = 'postgresql://app_user:...@db.internal:5432/maimemo'
python.exe scripts/backup_postgres.py --output 'D:\backups\maimemo-2026-10-02.dump'
```

输出目录必须已存在，扩展名必须是 `.dump`，目标文件必须不存在。失败时只清理由该次
命令新建的部分文件；绝不覆盖旧备份。`--docker-container` 仅供仓库的一次性集成测试，
生产不要使用。

## 5. 空库恢复演练

恢复只允许新数据库，且名称必须匹配 `maimemo_restore_[a-z0-9_]{8,47}`（总长不超过
PostgreSQL 标识符上限 63 字节）。确认值必须和目标
名称完全相同；目标已存在时始终拒绝，不提供覆盖开关。

```text
export MAIMEMO_RESTORE_ADMIN_URL='postgresql://restore_admin:...@db.internal:5432/postgres'
python scripts/restore_postgres.py \
  --backup /backups/maimemo-2026-10-02.dump \
  --target-database maimemo_restore_drill_20261002 \
  --confirm-disposable-target maimemo_restore_drill_20261002
```

脚本在恢复后验证关键表集合、非空 snapshot hash、反馈撤销链接，以及
`alembic_version` 与 `schema_metadata` 都处于当前迁移头 `0003`。随后仍要用只读 SQL 核对各表行数和
抽样 hash；演练验收完成后，由数据库管理员明确点名删除该 disposable 数据库。脚本不会
自动删除或改写源库、用户库或已有目标。

## 6. 升级与回滚

1. 先执行新备份和空库恢复演练。
2. 构建带唯一版本标签的镜像并记录 digest；不要以 `latest` 作为回滚证据。
3. 执行迁移，再逐个重建 MCP 与 Worker。
4. 核对 `/health/ready`、迁移版本、最近采集时间和连续失败数。
5. 应用回滚只能切回已记录 digest；数据库迁移是否可降级必须单独验证。0002/0003 对不可
   表示的数据会拒绝降级，不得用强制清表绕过。

Tunnel 中断不影响 Worker。数据库不可用时，就绪检查失败且采集不得以内存结果冒充已
持久化成功。
