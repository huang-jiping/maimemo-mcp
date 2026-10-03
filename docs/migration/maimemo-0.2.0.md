# maimemo 0.2.0 迁移说明

本文用于把 0.1.x 的单镜像部署迁移到 0.2.0 多包、多镜像部署。迁移保留现有 PostgreSQL
数据库和学习数据，不创建替代数据库，也不删除旧数据。

## 命名映射

| 旧名称 | 0.2.0 名称 | 说明 |
|---|---|---|
| 总项目 `maimemo-mcp` | 总项目 `maimemo` | 仓库和 Compose 项目统一使用产品名 |
| 主 Python 包 `maimemo_mcp` | 核心包 `maimemo` | 共享配置、API Client、业务与存储 |
| 旧 MCP 入口 | `maimemo-mcp` / 服务 `mcp` | 独立协议适配包与镜像 |
| 旧 Worker 入口 | `maimemo-worker` / 服务 `worker` | 独立后台包与镜像 |
| 无独立后端 | `maimemo-server` / 服务 `server` | 最小 HTTP 后端；OAuth 属于阶段 B |
| Alembic 模块命令 | 服务 `migrate` | 复用 Server 镜像的一次性受限迁移命令 |

三个运行镜像使用同一版本：

- `ghcr.io/huang-jiping/maimemo-server:0.2.0`
- `ghcr.io/huang-jiping/maimemo-mcp:0.2.0`
- `ghcr.io/huang-jiping/maimemo-worker:0.2.0`

生产部署和回滚应记录并使用三个实际 digest，不以 `latest` 作为证据。

## 切换前门禁

1. 备份现有 PostgreSQL，并按 `docs/operations.md` 恢复到新的空演练库完成校验。
2. 记录旧镜像 digest、旧 Compose 配置和旧 Worker 最近一次成功任务时间。
3. 拉取 0.2.0 的三个镜像，核对它们来自同一提交和版本。
4. 准备 `.env` 与两份只读 secret 文件；Server 和 migrate 不应挂载上游 secret。
5. 运行 `docker compose config` 和 `scripts/check_compose_secrets.py`。

任一门禁失败都停止切换。不要删除、重建、清空或强制降级现有数据库。

## 切换顺序

先停止旧 Worker，避免两个调度器同时采集，然后严格按以下顺序执行：

```text
# 先在旧项目的管理界面或旧 Compose 文件中停止旧 Worker，并确认它已退出。
docker compose pull
docker compose run --rm migrate upgrade head
docker compose up -d server
docker compose up -d mcp
docker compose up -d worker
docker compose ps
```

即 `migrate → server → mcp → worker`。长期服务不依赖一个常驻迁移容器；`migrate` 成功退出后
再启动三个运行服务。切换后核对：

- Server 和 MCP 的 `/health/ready` 均成功；
- `alembic_version` 与 `schema_metadata` 为迁移头 `0004`；
- Worker 只有一个实例获得任务锁，最近任务没有连续失败；
- 日志不含 Token、数据库 URL、个人响应正文或重复锁错误。

## 回滚

应用回滚时停止新版 Worker，把 Server、MCP、Worker 三个镜像成套切回切换前记录的 digest，
再恢复旧服务。不要只回滚其中一个镜像，也不要使用 `latest` 猜测旧版本。

数据库回滚是独立决策：0002/0003 遇到旧结构无法表达的数据会拒绝降级；0004 在
`failed_api_snapshot` 非空时会拒绝丢失证据。若无法证明降级无损，保留当前数据库结构，仅
回滚应用并调查；禁止通过清表或删除数据库绕过保护。

## 阶段 B 与本地目录

Server 的公网域名 `maimemo.huangjiping.com`、反向代理和真实 OAuth 授权/refresh token 属于
阶段 B。0.2.0 基础部署可以在不配置域名和 OAuth 的情况下先运行；OAuth 路由此时固定返回
`503 oauth_not_configured`。确认代理网络后再叠加 `compose.proxy.example.yaml`。

Git 仓库将重命名为 `huang-jiping/maimemo`。当前活跃 Codex 会话和工作树仍依赖旧本地路径，
只有在会话结束、分支合并且状态干净后才重命名本地目录；不要在实施过程中移动活动工作树。
