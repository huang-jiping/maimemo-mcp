# Maimemo 项目命名与独立包设计

## 1. 背景与目标

当前仓库、Python 发行包、Python 根包和主要运行服务均使用 `maimemo-mcp` 或
`maimemo_mcp` 作为总项目名称。随着项目增加 OAuth 后端、后台任务、前端和 Obsidian
同步能力，MCP 已不再适合作为整个项目的名称或根包边界。

本次设计将总项目统一命名为 `maimemo`，并将 MCP、后端和后台任务拆分为可独立构建、
发布、部署及升级的模块。迁移不得改变现有 PostgreSQL 数据结构和业务行为。

成功标准：

- GitHub 仓库名称为 `huang-jiping/maimemo`。
- Docker Compose 项目名称为 `maimemo`。
- MCP 是独立 Python 包，不作为项目主包或共享业务代码容器。
- Server、MCP 和 Worker 分别拥有独立的 Python 包、启动入口和 Docker 镜像。
- 共享业务能力集中在 `maimemo` 核心包中，依赖方向保持单向。
- 公网只暴露 Server；MCP、Worker 和 PostgreSQL 不直接暴露公网。

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
│   │       ├── config.py
│   │       ├── logging.py
│   │       └── time.py
│   ├── maimemo-mcp/
│   │   ├── pyproject.toml
│   │   └── src/maimemo_mcp/
│   │       ├── server.py
│   │       ├── dependencies.py
│   │       ├── envelopes.py
│   │       ├── health.py
│   │       └── tools/
│   ├── maimemo-server/
│   │   ├── pyproject.toml
│   │   └── src/maimemo_server/
│   └── maimemo-worker/
│       ├── pyproject.toml
│       └── src/maimemo_worker/
├── client/
├── docker/
│   └── Dockerfile
├── tests/
├── compose.yaml
├── pyproject.toml
└── uv.lock
```

根 `pyproject.toml` 只管理 uv workspace、测试和开发工具，不发布为 Python 包。

核心包中的 `api_client` 专指墨墨官方 API 客户端。`client` 名称保留给未来网页前端，
避免两个不同概念共用同一个名称。

## 4. 模块职责与依赖方向

### 4.1 `maimemo`

核心包负责：

- 墨墨官方 API 客户端和数据模型；
- PostgreSQL 模型、仓储和迁移所需元数据；
- 学习数据采集、标准化、分析与反馈业务；
- 通用配置、日志和时间处理。

核心包不得依赖 MCP、Server、Worker 或前端。

### 4.2 `maimemo-mcp`

独立 MCP 包负责：

- MCP 协议服务和工具注册；
- 将核心业务能力转换为智能体可调用工具；
- MCP 专用请求封装、依赖组装和健康检查；
- MCP Host 校验及传输配置。

该包只通过公开接口依赖 `maimemo`，不得承载通用业务逻辑。

### 4.3 `maimemo-server`

独立 Server 包负责：

- 应用主页和普通后端 API；
- OIDC Authorization Code 授权发起与回调；
- Access Token 和 Refresh Token 生命周期管理；
- 为未来前端提供受控接口。

公网域名 `maimemo.huangjiping.com` 只反向代理至该服务。

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

## 5. 启动入口和版本

每个运行模块提供独立命令：

```text
maimemo-server
maimemo-mcp
maimemo-worker
```

初始迁移版本：

| 包 | 版本 |
| --- | --- |
| `maimemo` | `0.2.0` |
| `maimemo-mcp` | `0.2.0` |
| `maimemo-server` | `0.1.0` |
| `maimemo-worker` | `0.1.0` |

各包独立管理版本。核心包变化后，只有依赖范围或实际行为受到影响的运行模块需要重新
发布。

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

- Server 可连接反向代理网络和项目内部网络。
- MCP 只连接项目内部网络或明确配置的专用隧道网络。
- Worker 只连接项目内部网络，不监听服务端口。
- PostgreSQL 使用现有外部数据库，不随本次命名迁移重建。
- 反向代理不得访问 MCP、Worker 或数据库。

## 7. 迁移顺序

1. 建立根 uv workspace 和四个独立 Python 包。
2. 将共享 API 客户端、存储、分析、反馈和采集逻辑迁移到 `maimemo`。
3. 将 MCP 协议层和工具迁移到独立 `maimemo-mcp`。
4. 将后台任务入口迁移到 `maimemo-worker`。
5. 建立 `maimemo-server` 的独立入口，为 OAuth 功能提供边界。
6. 更新导入路径、测试、迁移配置、脚本和当前运维文档。
7. 分别构建四个 Python 包和三个 Docker 镜像。
8. 完整验证通过后，将 GitHub 仓库改名为 `huang-jiping/maimemo`。
9. 更新本地 Git 远程地址和发布工作流。
10. 发布三个新的 GHCR 镜像。
11. 在 NAS 中创建 Compose 项目 `maimemo`。
12. 先停止旧 Worker，再启动新项目，避免新旧任务并行运行。
13. 验证数据库、Worker、MCP 和 Server 后移除旧 Compose 项目。
14. Server 部署成功后配置 `maimemo.huangjiping.com` 和 HTTPS。

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

### 8.2 Docker 和 Compose

- 三个镜像可分别构建并以非 root 用户启动。
- MCP 健康检查和协议握手成功。
- Worker 能执行最小同步任务且不会重复获取任务锁。
- Server 健康检查和应用主页可访问。
- `docker compose config` 显示项目名 `maimemo` 及服务 `server`、`mcp`、`worker`。
- MCP 与 Worker 没有公网端口。

### 8.3 数据和部署

- Alembic 迁移版本及现有表结构保持一致。
- 现有 PostgreSQL 数据无需导出、重建或重写。
- GHCR 三个镜像可由 NAS 拉取。
- NAS 客户端只显示一个 `maimemo` 项目。
- 公网只能访问 Server 的预期路由。

## 9. 失败处理与回滚

- 任一测试或镜像构建失败时，不重命名 GitHub 仓库，不切换 NAS。
- 任一新镜像发布失败时，保留现有部署。
- NAS 切换前先停止旧 Worker；新 Worker 验证失败时停止新版并恢复旧版。
- Server 或 MCP 验证失败不回滚 PostgreSQL，因为本次迁移不改变数据库结构。
- 旧 Compose 项目只有在新项目关键路径全部通过后才移除。
- GitHub 仓库改名后立即更新本地远程地址和文档，避免继续向旧地址发布。

## 10. 本次范围之外

- 不在命名迁移中实现完整 OAuth 自动刷新逻辑。
- 不创建未来网页前端。
- 不改变学习分析算法、MCP 工具语义或墨墨 API 调用行为。
- 不拆分为多个 GitHub 仓库。
- 不迁移或重建 PostgreSQL 数据库。

OAuth、Refresh Token 自动刷新和公网反向代理将在独立实施阶段完成，但必须遵守本设计
确定的 Server 边界和网络规则。
