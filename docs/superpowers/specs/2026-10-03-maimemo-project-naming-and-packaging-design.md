# Maimemo 项目命名与独立包设计

## 1. 背景与目标

当前仓库、Python 发行包、Python 根包和主要运行服务均使用 `maimemo-mcp` 或
`maimemo_mcp` 作为总项目名称。随着项目增加 OAuth 后端、后台任务、前端和 Obsidian
同步能力，MCP 已不再适合作为整个项目的名称或根包边界。

本次设计将总项目统一命名为 `maimemo`，并将 MCP、后端和后台任务拆分为可独立构建、
发布、部署及升级的模块。迁移不得改变现有 PostgreSQL 数据结构，也不得改变现有 MCP
与 Worker 的外部行为；新增的最小 Server 路由除外。

成功标准：

- GitHub 仓库名称为 `huang-jiping/maimemo`。
- Docker Compose 项目名称为 `maimemo`。
- MCP 是独立 Python 包，不作为项目主包或共享业务代码容器。
- Server、MCP 和 Worker 分别拥有独立的 Python 包、启动入口和 Docker 镜像。
- 共享业务能力集中在 `maimemo` 核心包中，依赖方向保持单向。
- 公网只暴露 Server；MCP、Worker 和 PostgreSQL 不直接暴露公网。
- 首期 Server 仅提供主页、健康检查和 OAuth 路由边界，完整 OAuth 与自动刷新另行实施。

## 2. 统一命名

| 对象 | 名称 |
| --- | --- |
| 总项目 | `maimemo` |
| GitHub 仓库 | `huang-jiping/maimemo` |
| Compose 项目 | `maimemo` |
| 核心 Python 发行包 | `maimemo` |
| 核心 Python 导入包 | `maimemo` |
| MCP Python 发行包 | `maimemo-mcp` |
| MCP Python 导入包 | `maimemo_mcp` |
| Server Python 发行包 | `maimemo-server` |
| Server Python 导入包 | `maimemo_server` |
| Worker Python 发行包 | `maimemo-worker` |
| Worker Python 导入包 | `maimemo_worker` |
| 未来前端 | `maimemo-client` |
| 公网域名 | `maimemo.huangjiping.com` |
| PostgreSQL 数据库及用户 | `maimemo` |

`MAIMEMO_MCP_*` 环境变量继续表示 MCP 模块专用配置，不视为旧项目名称残留。通用配置
继续使用 `MAIMEMO_*` 前缀。

## 3. 仓库和包结构

仓库使用单仓库、多包工作区：

```text
maimemo/
├── packages/
│   ├── maimemo/
│   │   ├── pyproject.toml
│   │   └── src/maimemo/
│   │       ├── api_client/
│   │       ├── analysis/
│   │       ├── feedback/
│   │       ├── ingestion/
│   │       ├── storage/
│   │       ├── application/
│   │       ├── config.py
│   │       ├── logging.py
│   │       └── time.py
│   ├── maimemo-mcp/
│   │   ├── pyproject.toml
│   │   └── src/maimemo_mcp/
│   │       ├── server.py
│   │       ├── dependencies.py
│   │       ├── config.py
│   │       ├── envelopes.py
│   │       ├── health.py
│   │       └── tools/
│   ├── maimemo-server/
│   │   ├── pyproject.toml
│   │   └── src/maimemo_server/
│   │       ├── app.py
│   │       ├── config.py
│   │       ├── migrate.py
│   │       └── oauth.py
│   └── maimemo-worker/
│       ├── pyproject.toml
│       └── src/maimemo_worker/
│           ├── config.py
│           ├── dependencies.py
│           └── runtime.py
├── client/
├── docker/
│   └── Dockerfile
├── migrations/
├── openapi/
├── scripts/
├── tests/
├── alembic.ini
├── compose.yaml
├── pyproject.toml
└── uv.lock
```

根 `pyproject.toml` 只管理 uv workspace、测试和开发工具，不发布为 Python 包。

核心包中的 `api_client` 专指墨墨官方 API 客户端。`client` 名称保留给未来网页前端，
避免两个不同概念共用同一个名称。

`migrations/`、`openapi/` 和运维脚本继续保留在仓库根目录；它们是整个项目的资产，不
归属于 MCP 包。

## 4. 模块职责与依赖方向

### 4.1 `maimemo`

核心包负责：

- 墨墨官方 API 客户端和数据模型；
- PostgreSQL 模型、仓储和迁移所需元数据；
- 学习数据采集、标准化、分析与反馈业务；
- 通用配置、日志和时间处理；
- 数据库引擎、会话、官方 API 客户端和业务服务的细粒度构造函数。

核心包不得依赖 MCP、Server、Worker 或前端。

核心包不得提供同时包含所有模块依赖的全局容器。每个运行模块必须在自己的
`dependencies.py` 中组装所需依赖，Worker 不得复用 MCP 的依赖容器。

### 4.2 `maimemo-mcp`

独立 MCP 包负责：

- MCP 协议服务和工具注册；
- 将核心业务能力转换为智能体可调用工具；
- MCP 专用请求封装、依赖组装和健康检查；
- MCP Host 校验及传输配置。

该包只通过公开接口依赖 `maimemo`，不得承载通用业务逻辑。

### 4.3 `maimemo-server`

独立 Server 包负责：

- 应用主页和健康检查；
- 预留 `/oauth/start` 和 `/oauth/callback` 路由边界；
- 未配置 OAuth 客户端时，OAuth 路由返回不包含秘密信息的
  `503 oauth_not_configured`；
- 为后续 OAuth 和前端 API 提供独立运行边界。

公网域名 `maimemo.huangjiping.com` 只反向代理至该服务。

OIDC Authorization Code、PKCE、授权状态校验、Access Token、Refresh Token 及自动
刷新不在本次命名迁移中实现，必须由后续 OAuth 设计覆盖。

### 4.4 `maimemo-worker`

独立 Worker 包负责：

- 定时同步学习进度和学习记录；
- 执行遗忘、易混词和薄弱项分析；
- 使用 PostgreSQL 锁避免重复任务；
- 记录任务状态和失败信息。

### 4.5 依赖约束

```text
maimemo-server ─┐
maimemo-worker ─┼──> maimemo
maimemo-mcp ────┘
```

Server、MCP 和 Worker 之间不得直接依赖。跨模块共享能力必须先提炼到 `maimemo` 核心
包的公开接口中。

### 4.6 配置边界

配置按运行模块拆分：

- `CoreSettings`：数据库 URL、时区、日志以及确实由多个模块共享的官方 API 凭据路径；
- `MCPSettings`：Host、Port、Allowed Hosts 和 MCP 专用传输配置；
- `ServerSettings`：Host、Port、外部基础 URL，以及后续加入的 OIDC 配置；
- `WorkerSettings`：同步周期、任务锁和 Worker 专用配置。

模块设置通过组合使用 `CoreSettings`，不得把所有字段重新集中到一个全局 `Settings`
对象。每个进程只读取自己需要的环境变量和 secret；数据库迁移只读取数据库 URL。

## 5. 启动入口和版本

每个运行模块提供独立命令：

```text
maimemo-server
maimemo-mcp
maimemo-worker
```

初始迁移版本统一为 `0.2.0`：

| 包 | 版本 |
| --- | --- |
| `maimemo` | `0.2.0` |
| `maimemo-mcp` | `0.2.0` |
| `maimemo-server` | `0.2.0` |
| `maimemo-worker` | `0.2.0` |

各包和镜像独立构建、独立发布，但在当前开发阶段使用统一版本号。核心包变化后，三个
依赖它的运行镜像必须重新构建和验证，避免同一版本组合出现不可追踪的差异。只有在建立
明确的兼容范围、依赖锁定和兼容矩阵后，才能改为独立版本。

项目仍处于开发阶段，不提供旧 `maimemo_mcp` 主包结构的兼容层。独立 MCP 包继续使用
`maimemo_mcp` 作为导入名，但其中仅保留 MCP 适配层代码。

## 6. Docker 镜像和 Compose

使用一个多阶段 Dockerfile 共享基础层和构建缓存，但输出三个独立镜像：

```text
ghcr.io/huang-jiping/maimemo-server
ghcr.io/huang-jiping/maimemo-mcp
ghcr.io/huang-jiping/maimemo-worker
```

每个镜像支持以下标签：

```text
latest
stable
<语义版本>
<提交哈希>
```

`latest` 表示默认分支最近一次成功构建，`stable` 表示最近一次通过 NAS 验收并明确提升
的版本。三个镜像的语义版本标签必须成套发布，提交哈希标签用于精确回滚。

Compose 项目和服务采用：

```yaml
name: maimemo

services:
  server:
  mcp:
  worker:
```

不设置固定 `container_name`，由 Compose 生成 `maimemo-server-1`、`maimemo-mcp-1` 和
`maimemo-worker-1`，保留 Compose 的升级、扩容和故障恢复能力。

网络规则：

- 项目应用网络使用普通 Bridge 网络，必须允许访问墨墨官方 API 和外部 PostgreSQL；
  不得配置为 Docker 的 `internal: true`。
- Server 连接项目应用网络和反向代理外部网络，只由反向代理发布公网入口。
- MCP 连接项目应用网络及按需配置的专用隧道网络，不发布宿主机端口。
- Worker 只连接项目应用网络，不监听服务端口。
- PostgreSQL 使用现有外部数据库，不随本次命名迁移重建。
- 反向代理不得访问 MCP、Worker 或数据库。

无宿主机端口不等于禁止出站访问；网络隔离依靠不发布端口、明确的网络成员和反向代理
路由实现。

### 6.1 数据库迁移服务

- SQLAlchemy `Base.metadata` 由 `maimemo.storage` 提供。
- `migrations/` 和 `alembic.ini` 保留在仓库根目录，导入路径迁移为 `maimemo.*`。
- `maimemo-server` 包提供独立的 `maimemo-migrate` 管理命令。
- Server 镜像包含 Alembic 配置和迁移文件。
- Compose 增加一次性 `migrate` 服务，使用 Server 镜像执行升级，不长期运行。
- `migrate` 只接收数据库 URL，不挂载墨墨 Token、OAuth secret 或指纹密钥。
- MCP 和 Worker 镜像不得作为数据库升级入口。

## 7. 迁移顺序

### 7.1 阶段 A：命名和包边界迁移

1. 建立根 uv workspace 和四个独立 Python 包。
2. 拆分 `CoreSettings`、`MCPSettings`、`ServerSettings` 和 `WorkerSettings`。
3. 将共享 API 客户端、存储、分析、反馈和采集逻辑迁移到 `maimemo`。
4. 将通用依赖构造函数移入核心包，并分别建立 MCP 与 Worker 的依赖组装入口。
5. 将 MCP 协议层和工具迁移到独立 `maimemo-mcp`。
6. 将后台任务入口迁移到 `maimemo-worker`。
7. 建立最小 `maimemo-server`，提供主页、健康检查及未配置状态的 OAuth 路由。
8. 迁移 Alembic 导入路径，并建立 `maimemo-migrate` 和一次性 Compose 服务。
9. 更新导入路径、测试、脚本和当前运维文档。
10. 分别构建四个 Python 包和三个 Docker 镜像。
11. 完整验证通过后，将 GitHub 仓库改名为 `huang-jiping/maimemo`。
12. 更新本地 Git 远程地址和发布工作流。
13. 发布三个使用同一版本号的新 GHCR 镜像。
14. 在 NAS 中创建 Compose 项目 `maimemo`。
15. 先停止旧 Worker，按 `migrate`、Server、MCP、Worker 的顺序启动新项目。
16. 验证数据库、Worker、MCP 和 Server 后移除旧 Compose 项目。
17. 当前开发会话和工具绑定结束后，将本地目录从 `P:\maimemo-mcp` 改为
    `P:\maimemo`，并重新登记本地项目路径；不得在活跃会话中直接改名。

### 7.2 阶段 B：公网和 OAuth

1. 为 Server 配置 `maimemo.huangjiping.com`、反向代理和 HTTPS。
2. 验证主页、健康检查和 OAuth 回调路径可以从公网访问。
3. 创建并审核墨墨后端应用。
4. 依据独立 OAuth 设计实现 Authorization Code、PKCE、Refresh Token 存储和自动刷新。
5. 完成真实授权，确认令牌接口返回 `refresh_token` 后再启用自动刷新。

旧设计和实施计划作为历史记录保留，并在文件顶部注明名称已迁移；不机械改写历史命令
和当时的架构描述。

## 8. 验证标准

### 8.1 Python 和工作区

- uv workspace 能够完整解析并锁定依赖。
- 四个 Python 包可分别构建。
- 单元、契约、MCP、集成和评估测试全部通过。
- Ruff 和 mypy 检查通过。
- 核心包不存在对三个运行模块的反向依赖。
- MCP、Server 和 Worker 之间不存在直接依赖。
- 配置测试证明每个进程不要求其他模块的环境变量或 secret。
- 依赖边界测试证明 Worker 不导入 `maimemo_mcp`，MCP 不导入 Server 或 Worker。

### 8.2 Docker 和 Compose

- 三个镜像可分别构建并以非 root 用户启动。
- MCP 健康检查和协议握手成功。
- Worker 能执行最小同步任务且不会重复获取任务锁。
- Server 健康检查和应用主页可访问；未配置 OAuth 时相关路由返回预期的安全错误。
- `maimemo-migrate` 能在不读取 API 或 OAuth secret 的情况下检查当前迁移头。
- `docker compose config` 显示项目名 `maimemo` 及服务 `migrate`、`server`、`mcp`、
  `worker`。
- MCP 与 Worker 没有公网端口。
- Compose 应用网络允许出站访问，但不为 MCP 与 Worker 发布宿主机端口。

### 8.3 数据和部署

- Alembic 迁移版本及现有表结构保持一致。
- 现有 PostgreSQL 数据无需导出、重建或重写。
- GHCR 三个镜像可由 NAS 拉取。
- 三个镜像具有相同语义版本标签，并记录各自提交哈希标签。
- NAS 客户端只显示一个 `maimemo` 项目。
- 公网只能访问 Server 的预期路由。

## 9. 失败处理与回滚

- 任一测试或镜像构建失败时，不重命名 GitHub 仓库，不切换 NAS。
- 任一新镜像发布失败时，保留现有部署。
- NAS 切换前先停止旧 Worker；新 Worker 验证失败时停止新版并恢复旧版。
- `migrate` 检查失败时不得启动三个长期运行服务。
- Server 或 MCP 验证失败不回滚 PostgreSQL，因为阶段 A 不新增数据库迁移版本。
- 旧 Compose 项目只有在新项目关键路径全部通过后才移除。
- GitHub 仓库改名后立即更新本地远程地址和文档，避免继续向旧地址发布。

## 10. 本次范围之外

- 不在阶段 A 实现 Authorization Code、PKCE、Token 持久化或自动刷新逻辑。
- 不创建未来网页前端。
- 不改变学习分析算法、MCP 工具语义或墨墨 API 调用行为。
- 不拆分为多个 GitHub 仓库。
- 不迁移或重建 PostgreSQL 数据库。

阶段 B 的 OAuth、Refresh Token 自动刷新和公网反向代理必须遵守本设计确定的 Server
边界和网络规则，并在实施前补充独立 OAuth 设计。
