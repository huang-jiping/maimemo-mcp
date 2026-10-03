# Maimemo 项目命名与独立包实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有单包 `maimemo-mcp` 仓库迁移为名为 `maimemo` 的 uv 多包工作区，使核心包、MCP、Server 和 Worker 可独立构建、发布和部署，同时保持现有数据库结构及 MCP/Worker 行为。

**Architecture:** 仓库根项目作为不发布的虚拟工作区，四个 Python 包位于 `packages/`。`maimemo-mcp`、`maimemo-server` 和 `maimemo-worker` 只依赖核心包 `maimemo`，彼此不得直接依赖；三个运行模块分别构建镜像，由同一个 Compose 项目编排。

**Tech Stack:** Python 3.12、uv workspace、Hatchling、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 15、Starlette/Uvicorn、MCP Python SDK 2、Docker Compose、GitHub Actions、GHCR。

**Spec:** `docs/superpowers/specs/2026-10-03-maimemo-project-naming-and-packaging-design.md`

## Global Constraints

- 本计划只实施设计中的阶段 A；阶段 B 的 OAuth、Refresh Token 和公网反向代理必须先有独立设计。
- GitHub 仓库最终为 `huang-jiping/maimemo`，Compose 项目名固定为 `maimemo`。
- 四个 Python 包初始版本统一为 `0.2.0`，三个镜像使用同一语义版本标签。
- 根项目名使用不发布的 `maimemo-workspace`，并设置 `tool.uv.package = false`；可发布核心包名仍为 `maimemo`。
- Python 版本保持 `>=3.12,<3.13`，继续使用 Hatchling 和单一 `uv.lock`。
- 不新增、删除或修改 Alembic 版本；数据库迁移头保持 `0004`。
- 不改变现有 MCP 工具名称、Schema、只读边界、Worker 调度语义或安全日志策略。
- Server 首期只提供主页、健康检查和返回 `503 oauth_not_configured` 的 OAuth 路由。
- MCP 与 Worker 不发布宿主机端口；应用 Bridge 网络必须允许访问墨墨 API 和外部 PostgreSQL。
- 不把真实 Token、数据库密码、OAuth secret、个人响应或 NAS 地址写入仓库、镜像、日志和测试。
- Windows 命令使用 PowerShell 7；每个外部命令后检查 `$LASTEXITCODE`。
- 当前只读检查确认 Git 2.54.0 和 Docker Compose v5.1.4 可用，Docker Engine 29.5.3 正在运行。
- 当前会话 PATH 中 `uv` 与 `gh` 不可用，`WindowsApps` 的 Python 是无效占位程序；执行前必须先定位已安装工具或取得用户授权后解决环境，不能自动安装。

## Review Focus

- 只配置 Server 时不得要求墨墨 Token；由 Task 2、3、5 的配置隔离测试固定。
- Worker 不得导入 `maimemo_mcp`，MCP 不得导入 Server 或 Worker；由 Task 3、4 的 AST 依赖边界测试固定。
- `maimemo-migrate` 只能读取数据库 URL，且不得产生新的 Alembic 版本；由 Task 5 的迁移测试固定。
- Compose 必须允许出站访问，但不得为 MCP 与 Worker发布端口；由 Task 6 的配置测试固定。
- 三个镜像必须使用相同语义版本并能按提交哈希回滚；由 Task 7 的发布元数据测试固定。

---

## 执行前门禁（不产生提交）

- [ ] **步骤 1：确认工作区干净并记录基线**

Run:

```powershell
git status --short
git branch --show-current
git log -1 --oneline
```

Expected：工作区为空，分支为实施用 `codex/maimemo-packaging` 工作树分支，不直接在 `master` 上开发。

- [ ] **步骤 2：确认 PowerShell、Git、Docker、uv 和 gh 的真实路径**

Run:

```powershell
$PSVersionTable | Select-Object PSVersion, PSEdition
Get-Command pwsh, git, docker, uv, gh
docker compose version
docker info --format '{{.ServerVersion}}'
uv --version
gh --version
gh auth status
```

Expected：全部命令退出码为 0。当前已知 `uv` 和 `gh` 在 PATH 中缺失，因此未解决前不得开始 Task 1；不得使用 `WindowsApps` Python 占位程序。

- [ ] **步骤 3：建立隔离工作树**

使用 `superpowers:using-git-worktrees` 创建 `codex/maimemo-packaging` 工作树，并在新目录重新执行步骤 1、2。

### Task 1: 建立可渐进迁移的 uv 工作区骨架

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `packages/maimemo/pyproject.toml`
- Create: `packages/maimemo/src/maimemo/__init__.py`
- Create: `packages/maimemo-server/pyproject.toml`
- Create: `packages/maimemo-server/src/maimemo_server/__init__.py`
- Create: `packages/maimemo-worker/pyproject.toml`
- Create: `packages/maimemo-worker/src/maimemo_worker/__init__.py`
- Create: `tests/architecture/test_workspace_metadata.py`

**Interfaces:**
- Consumes: 当前根项目 `maimemo-mcp==0.1.0` 及其锁文件。
- Produces: 单锁文件 workspace；成员 `maimemo`、`maimemo-server`、`maimemo-worker`；根项目在本任务中暂时保留现有 MCP 包，Task 4 再转为虚拟根。

- [ ] **步骤 1：编写失败的工作区元数据测试**

在 `tests/architecture/test_workspace_metadata.py` 添加：

```python
def test_workspace_declares_expected_members() -> None:
    assert workspace_members() == {
        'packages/maimemo',
        'packages/maimemo-server',
        'packages/maimemo-worker',
    }

def test_initial_package_versions_are_aligned() -> None:
    assert package_versions() == {'0.2.0'}
```

辅助函数只读取 TOML，不导入产品代码；同时断言三个成员均存在 `[build-system]` 和 `src` 包映射。

- [ ] **步骤 2：运行测试并确认失败**

Run: `uv run pytest tests/architecture/test_workspace_metadata.py -v`

Expected：FAIL，原因是 workspace 和成员元数据尚不存在。

- [ ] **步骤 3：添加最小 workspace 和三个成员包**

根 `pyproject.toml` 暂时保留现有项目构建配置，新增：

```toml
[tool.uv.workspace]
members = ['packages/*']

[tool.uv.sources]
maimemo = { workspace = true }
maimemo-server = { workspace = true }
maimemo-worker = { workspace = true }
```

三个成员都声明 Python 3.12 和 Hatchling；本任务只创建最小 `__init__.py`，不复制业务代码。

- [ ] **步骤 4：重新生成锁文件并验证成员构建**

Run:

```powershell
uv lock
uv build --package maimemo
uv build --package maimemo-server
uv build --package maimemo-worker
uv run pytest tests/architecture/test_workspace_metadata.py -v
```

Expected：三个包构建成功，测试 PASS；构建产物不包含真实配置或 secret。

- [ ] **步骤 5：运行现有快速测试确认骨架未改变行为**

Run: `uv run pytest tests/unit tests/contract -v`

Expected：PASS。

- [ ] **步骤 6：提交工作区骨架**

```powershell
git add pyproject.toml uv.lock packages tests/architecture/test_workspace_metadata.py
git commit -m 'build(workspace): 建立多包项目骨架'
```

### Task 2: 提取核心包并拆除模块专用配置耦合

**Files:**
- Move: `src/maimemo_mcp/analysis/` → `packages/maimemo/src/maimemo/analysis/`
- Move: `src/maimemo_mcp/feedback/` → `packages/maimemo/src/maimemo/feedback/`
- Move: `src/maimemo_mcp/storage/` → `packages/maimemo/src/maimemo/storage/`
- Move: `src/maimemo_mcp/maimemo_client/` → `packages/maimemo/src/maimemo/api_client/`
- Move: `src/maimemo_mcp/database_url.py` → `packages/maimemo/src/maimemo/database_url.py`
- Move: `src/maimemo_mcp/logging.py` → `packages/maimemo/src/maimemo/logging.py`
- Move: `src/maimemo_mcp/time.py` → `packages/maimemo/src/maimemo/time.py`
- Move: `src/maimemo_mcp/ingestion/hashing.py` → `packages/maimemo/src/maimemo/ingestion/hashing.py`
- Move: `src/maimemo_mcp/ingestion/normalizers.py` → `packages/maimemo/src/maimemo/ingestion/normalizers.py`
- Move: `src/maimemo_mcp/ingestion/service.py` → `packages/maimemo/src/maimemo/ingestion/service.py`
- Create: `packages/maimemo/src/maimemo/config.py`
- Create: `packages/maimemo/src/maimemo/application/resources.py`
- Modify: `src/maimemo_mcp/config.py`
- Modify: all affected imports under `src/`, `scripts/`, `migrations/`, and `tests/`
- Create: `tests/architecture/test_import_boundaries.py`
- Split/modify: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `CoreSettings`, `DatabaseSettings`, `UpstreamCredentialSettings`, `AnalysisIntervals`。
- Produces: `create_async_engine(database_url: str) -> AsyncEngine` and `create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]`。
- Produces: `open_database(database: DatabaseSettings) -> AsyncIterator[DatabaseResources]` and `open_upstream(credentials: UpstreamCredentialSettings, sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[UpstreamResources]`。
- Produces dependency ownership in `packages/maimemo/pyproject.toml`: `pydantic>=2,<3`、`sqlalchemy[asyncio]>=2,<3`、`psycopg[binary]>=3,<4`、`httpx>=0.28,<1`、`pyyaml>=6,<7`、`tzdata>=2025.2`。
- Consumes: Task 1 的 `maimemo` workspace 成员。

- [ ] **步骤 1：编写失败的核心配置和导入边界测试**

新增断言：

```python
def test_core_settings_do_not_define_module_ports_or_intervals() -> None:
    assert set(CoreSettings.model_fields) == {'database', 'timezone', 'log_level'}

def test_upstream_credentials_are_loaded_only_when_requested() -> None:
    settings = CoreSettings.load(minimal_database_environment())
    assert settings.database.database_url.startswith('postgresql+psycopg://')

def test_core_never_imports_runtime_packages() -> None:
    assert forbidden_imports('packages/maimemo/src', {'maimemo_mcp', 'maimemo_server', 'maimemo_worker'}) == []
```

测试还应证明数据库 URL 错误不会泄露密码，迁移仅构造 `DatabaseSettings` 时不要求 Token。

- [ ] **步骤 2：运行新测试并确认失败**

Run: `uv run pytest tests/architecture/test_import_boundaries.py tests/unit/test_config.py -v`

Expected：FAIL，原因是核心包和配置模型尚未提取。

- [ ] **步骤 3：移动核心代码并更新内部导入**

使用 `git mv` 保留历史。将 `maimemo_client` 更名为 `api_client`，把所有共享模块导入改为 `maimemo.*`；不得留下复制后的第二份实现。

- [ ] **步骤 4：实现细粒度核心配置与资源构造接口**

`CoreSettings` 只组合数据库和非 secret 运行设置；`UpstreamCredentialSettings` 单独读取 `MAIMEMO_TOKEN_FILE` 与 `MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE`。引擎构造函数接收数据库 URL，不接收任何运行模块 Settings。

- [ ] **步骤 5：更新现有测试和脚本导入**

契约、存储、分析、反馈、采集和备份恢复测试改为导入 `maimemo.*`。MCP 专用测试仍导入当前根 `maimemo_mcp`，但其业务依赖改为核心包。

- [ ] **步骤 6：运行核心测试集**

Run:

```powershell
uv run pytest tests/unit tests/contract tests/integration -v
uv run mypy packages/maimemo/src
uv run ruff check packages/maimemo src scripts migrations tests
```

Expected：PASS；`rg -n 'maimemo_mcp\.(analysis|feedback|storage|maimemo_client|database_url|logging|time)' .` 无代码命中。

- [ ] **步骤 7：构建核心包并检查内容**

Run:

```powershell
uv build --package maimemo
uv run python -c "import maimemo; import maimemo.api_client; import maimemo.storage"
```

Expected：构建和导入成功，wheel 不包含 MCP、Server 或 Worker 包。

- [ ] **步骤 8：提交核心包提取**

```powershell
git add packages/maimemo src scripts migrations tests pyproject.toml uv.lock
git commit -m 'refactor(core): 提取共享业务核心包'
```

### Task 3: 提取独立 Worker 包和运行入口

**Files:**
- Move: `src/maimemo_mcp/ingestion/scheduler.py` → `packages/maimemo-worker/src/maimemo_worker/scheduler.py`
- Move: `src/maimemo_mcp/ingestion/worker.py` → `packages/maimemo-worker/src/maimemo_worker/worker.py`
- Create: `packages/maimemo-worker/src/maimemo_worker/config.py`
- Create: `packages/maimemo-worker/src/maimemo_worker/dependencies.py`
- Create: `packages/maimemo-worker/src/maimemo_worker/runtime.py`
- Modify: `packages/maimemo-worker/pyproject.toml`
- Modify: `src/maimemo_mcp/runtime.py`
- Move/modify: Worker-related tests under `tests/unit/` and `tests/integration/`
- Modify: `tests/architecture/test_import_boundaries.py`

**Interfaces:**
- Produces: `WorkerSettings(core: CoreSettings, upstream: UpstreamCredentialSettings, intervals: AnalysisIntervals)`。
- Produces: `WorkerDependencies(service: StudyIngestionService, schedule: Schedule)`。
- Produces: `open_worker_dependencies(settings: WorkerSettings) -> AsyncIterator[WorkerDependencies]`。
- Produces: console script `maimemo-worker = maimemo_worker.runtime:main`。
- Produces dependency ownership in `packages/maimemo-worker/pyproject.toml`: only `maimemo>=0.2.0,<0.3` as a workspace dependency。
- Consumes: Task 2 的核心资源构造函数和采集服务。

- [ ] **步骤 1：编写失败的 Worker 配置和依赖边界测试**

```python
def test_worker_settings_do_not_require_mcp_environment(environ: dict[str, str]) -> None:
    settings = WorkerSettings.load(environ)
    assert settings.intervals.today_interval_minutes == 30

def test_worker_package_never_imports_mcp() -> None:
    assert forbidden_imports('packages/maimemo-worker/src', {'maimemo_mcp'}) == []
```

增加入口测试，断言缺失 Worker 所需 secret 时返回受控的 `configuration_error`，且输出不包含输入 URL 或 secret。

- [ ] **步骤 2：运行测试并确认失败**

Run: `uv run pytest tests/architecture/test_import_boundaries.py tests/unit/test_runtime.py tests/unit/test_scheduler.py -v`

Expected：FAIL，原因是 Worker 仍位于旧主包并复用 MCP 依赖容器。

- [ ] **步骤 3：移动 Worker 调度和循环代码**

将 `Schedule` 改为接收 `AnalysisIntervals`，不得接收 MCP Settings。业务采集继续由 `maimemo.ingestion.service` 提供。

- [ ] **步骤 4：实现 Worker 独立配置和依赖组装**

`open_worker_dependencies` 使用 Task 2 的数据库和上游资源构造函数，创建 `WeaknessService`、`StudyIngestionService` 与 `Schedule`；不得导入 `maimemo_mcp`。

- [ ] **步骤 5：实现独立入口并移除旧 Worker 模式**

`maimemo_worker.runtime.main(argv: Sequence[str] | None = None) -> int` 只启动 Worker。旧 `maimemo_mcp.runtime` 暂时仅保留 MCP 模式，Task 4 将其替换。

- [ ] **步骤 6：迁移并运行 Worker 测试**

Run:

```powershell
uv run pytest tests/unit/test_scheduler.py tests/unit/test_runtime.py tests/integration/test_worker_locking.py tests/integration/test_worker_schema.py tests/integration/test_collection_midnight.py -v
uv run mypy packages/maimemo-worker/src
uv run ruff check packages/maimemo-worker tests
```

Expected：PASS；Worker 包没有 `maimemo_mcp` 导入。

- [ ] **步骤 7：构建 Worker 包并验证入口**

Run:

```powershell
uv build --package maimemo-worker
uv run --package maimemo-worker maimemo-worker --help
```

Expected：构建成功，帮助命令退出码为 0 且不读取数据库或 secret。

- [ ] **步骤 8：提交 Worker 拆分**

```powershell
git add packages/maimemo-worker src tests pyproject.toml uv.lock
git commit -m 'refactor(worker): 拆分独立任务运行包'
```

### Task 4: 将 MCP 迁移为独立 workspace 包并虚拟化仓库根项目

**Files:**
- Create: `packages/maimemo-mcp/pyproject.toml`
- Move: `src/maimemo_mcp/mcp_server/app.py` → `packages/maimemo-mcp/src/maimemo_mcp/server.py`
- Move: `src/maimemo_mcp/mcp_server/dependencies.py` → `packages/maimemo-mcp/src/maimemo_mcp/dependencies.py`
- Move: `src/maimemo_mcp/mcp_server/envelopes.py` → `packages/maimemo-mcp/src/maimemo_mcp/envelopes.py`
- Move: `src/maimemo_mcp/mcp_server/health.py` → `packages/maimemo-mcp/src/maimemo_mcp/health.py`
- Move: `src/maimemo_mcp/mcp_server/tools/` → `packages/maimemo-mcp/src/maimemo_mcp/tools/`
- Create: `packages/maimemo-mcp/src/maimemo_mcp/config.py`
- Modify: `packages/maimemo-mcp/src/maimemo_mcp/__init__.py`
- Remove: `src/maimemo_mcp/` after all code is relocated
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: all files under `tests/mcp/` and `tests/evaluation/`
- Modify: `tests/architecture/test_workspace_metadata.py`
- Modify: `tests/architecture/test_import_boundaries.py`

**Interfaces:**
- Produces: `MCPSettings(core: CoreSettings, upstream: UpstreamCredentialSettings, host: str, port: int, allowed_hosts: tuple[str, ...], drift_state_file: Path)`。
- Produces: `MCPDependencies` and `open_mcp_dependencies(settings: MCPSettings) -> AsyncIterator[MCPDependencies]`。
- Produces: `create_mcp_app(settings: MCPSettings, *, clock: Clock = utc_now) -> MCPServer`。
- Produces: console script `maimemo-mcp = maimemo_mcp.server:main`。
- Produces dependency ownership in `packages/maimemo-mcp/pyproject.toml`: `maimemo>=0.2.0,<0.3`、`mcp>=2,<3`、`anyio>=4,<5`、`starlette>=1,<2`、`uvicorn>=0.54,<1`。
- Converts: root project to virtual `maimemo-workspace` with `tool.uv.package = false`。

- [ ] **步骤 1：扩展失败的 workspace 和导入边界测试**

断言最终 workspace 成员恰好为四个包，根项目不可构建，MCP 包不得导入 `maimemo_server` 或 `maimemo_worker`，且 `maimemo_mcp` 只包含 MCP 协议层。

```python
def test_runtime_packages_depend_only_on_core() -> None:
    assert direct_workspace_dependencies('maimemo-mcp') == {'maimemo'}
    assert direct_workspace_dependencies('maimemo-server') == {'maimemo'}
    assert direct_workspace_dependencies('maimemo-worker') == {'maimemo'}
```

- [ ] **步骤 2：运行边界测试并确认失败**

Run: `uv run pytest tests/architecture -v`

Expected：FAIL，原因是 MCP 仍是根项目且 workspace 尚缺 `maimemo-mcp` 成员。

- [ ] **步骤 3：移动 MCP 代码并更新导入**

保留导入包名 `maimemo_mcp`，但删除 `mcp_server` 中间层；所有共享业务导入改为 `maimemo.*`。工具名称、Schema、注解和服务名 `maimemo-mcp` 保持不变。

- [ ] **步骤 4：实现 MCP 独立配置和资源组装**

从旧 `Settings` 中只迁移 MCP 字段；使用核心资源构造函数创建 MCP 需要的数据库、官方 API 客户端、反馈和薄弱项服务。`main(argv=None) -> int` 只启动 MCP。

- [ ] **步骤 5：把根项目转换为虚拟 workspace**

根 `pyproject.toml` 使用 `name = 'maimemo-workspace'`、`version = '0.2.0'` 和 `tool.uv.package = false`，开发依赖保留在根项目；根项目依赖四个 workspace 成员，使 `uv run pytest` 能导入全部包。根依赖不得重复声明产品库依赖版本。

- [ ] **步骤 6：更新 MCP、评估和安全边界测试**

更新 monkeypatch 路径与构造方式；服务版本断言从 `0.1.0` 改为 `0.2.0`。不得弱化固定工具清单、secret 脱敏或 Host allowlist 测试。

- [ ] **步骤 7：运行 MCP 与架构测试**

Run:

```powershell
uv lock --check
uv run pytest tests/architecture tests/mcp tests/evaluation -v
uv run mypy packages/maimemo-mcp/src
uv run ruff check packages/maimemo-mcp tests
```

Expected：PASS；`rg -n 'src/maimemo_mcp|maimemo_mcp\.mcp_server' .` 只允许在历史设计或计划文档中出现。

- [ ] **步骤 8：分别构建三个已实现包**

Run:

```powershell
uv build --package maimemo
uv build --package maimemo-mcp
uv build --package maimemo-worker
uv run --package maimemo-mcp maimemo-mcp --help
```

Expected：构建和帮助命令全部成功。

- [ ] **步骤 9：提交 MCP 独立化**

```powershell
git add pyproject.toml uv.lock packages src tests
git commit -m 'refactor(mcp): 拆分独立协议服务包'
```

### Task 5: 实现最小 Server 和独立数据库迁移入口

**Files:**
- Create: `packages/maimemo-server/src/maimemo_server/config.py`
- Create: `packages/maimemo-server/src/maimemo_server/app.py`
- Create: `packages/maimemo-server/src/maimemo_server/migrate.py`
- Create: `packages/maimemo-server/src/maimemo_server/runtime.py`
- Modify: `packages/maimemo-server/pyproject.toml`
- Modify: `migrations/env.py`
- Modify: `alembic.ini` only if entrypoint path resolution requires it
- Create: `tests/server/test_config.py`
- Create: `tests/server/test_app.py`
- Create: `tests/server/test_migrate.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/architecture/test_import_boundaries.py`

**Interfaces:**
- Produces: `ServerSettings(database: DatabaseSettings, host: str = '0.0.0.0', port: int = 8080, external_base_url: AnyHttpUrl | None = None)`。
- Produces: `create_app(settings: ServerSettings) -> Starlette`。
- Produces: console scripts `maimemo-server = maimemo_server.runtime:main` and `maimemo-migrate = maimemo_server.migrate:main`。
- Produces dependency ownership in `packages/maimemo-server/pyproject.toml`: `maimemo>=0.2.0,<0.3`、`alembic>=1,<2`、`starlette>=1,<2`、`uvicorn>=0.54,<1`。
- `maimemo-migrate` accepts only `upgrade head` and `current`; unsupported commands fail closed。

- [ ] **步骤 1：编写失败的 Server 配置测试**

```python
def test_server_loads_without_upstream_secrets(environ: dict[str, str]) -> None:
    settings = ServerSettings.load(environ)
    assert settings.port == 8080

def test_server_rejects_non_https_external_base_url() -> None:
    with pytest.raises(ValidationError):
        ServerSettings(
            database=DatabaseSettings(
                database_url='postgresql+psycopg://u:p@db.invalid:5432/maimemo'
            ),
            external_base_url='http://maimemo.example',
        )
```

测试环境只提供数据库 URL，不提供 `MAIMEMO_TOKEN_FILE` 或 OAuth secret。

- [ ] **步骤 2：编写失败的最小路由测试**

使用 Starlette TestClient 断言：

```python
assert client.get('/').status_code == 200
assert client.get('/health/live').json() == {'status': 'live'}
assert client.get('/oauth/start').status_code == 503
assert client.get('/oauth/callback').json() == {'error': 'oauth_not_configured'}
```

响应不得包含环境变量、异常正文、Token 或数据库 URL。

- [ ] **步骤 3：编写失败的迁移入口测试**

断言 `maimemo-migrate current` 和 `upgrade head` 只读取 `MAIMEMO_DATABASE_URL`；缺少 URL 时返回受控错误；传入 `downgrade`、任意 revision 或附加参数时拒绝执行。

- [ ] **步骤 4：运行 Server 测试并确认失败**

Run: `uv run pytest tests/server tests/integration/test_migrations.py -v`

Expected：FAIL，原因是 Server 与迁移入口尚未实现。

- [ ] **步骤 5：实现最小 Server**

使用已在技术栈中的 Starlette/Uvicorn。`/health/ready` 执行最小数据库检查并验证当前 Alembic head；OAuth 两个路由始终返回固定 503，不接受、存储或回显请求中的授权参数。

- [ ] **步骤 6：实现受限迁移命令并更新 Alembic 导入**

`migrations/env.py` 改为导入 `maimemo.storage`。`maimemo_server.migrate.main(argv)` 通过 Alembic Python API执行唯一允许的命令，不创建迁移文件，也不读取任何 API 或 OAuth secret。

- [ ] **步骤 7：验证没有产生数据库结构差异**

Run:

```powershell
uv run pytest tests/server tests/integration/test_migrations.py tests/integration/test_schema_constraints.py -v
uv run alembic heads
git diff --exit-code -- migrations/versions
```

Expected：测试 PASS；唯一 head 为 `0004`；`git diff --exit-code` 返回 0。

- [ ] **步骤 8：构建 Server 包并验证入口**

Run:

```powershell
uv build --package maimemo-server
uv run --package maimemo-server maimemo-server --help
uv run --package maimemo-server maimemo-migrate --help
```

Expected：全部成功且帮助命令不连接数据库。

- [ ] **步骤 9：提交最小 Server 和迁移入口**

```powershell
git add packages/maimemo-server migrations alembic.ini tests pyproject.toml uv.lock
git commit -m 'feat(server): 添加最小后端和迁移入口'
```

### Task 6: 构建三个独立镜像并重写 Compose

**Files:**
- Move/replace: `Dockerfile` → `docker/Dockerfile`
- Modify: `compose.yaml`
- Modify: `compose.test.yaml`
- Create: `compose.proxy.example.yaml`
- Modify: `.env.example`
- Modify: `.gitignore` if new local override files need exclusion
- Modify: `scripts/check_compose_secrets.py`
- Create: `tests/docker/test_compose_contract.py`
- Create: `tests/docker/test_image_contract.py`

**Interfaces:**
- Produces Docker targets: `server`、`mcp`、`worker`。
- Produces images: `ghcr.io/huang-jiping/maimemo-server`、`ghcr.io/huang-jiping/maimemo-mcp`、`ghcr.io/huang-jiping/maimemo-worker`。
- Produces Compose services: `migrate`、`server`、`mcp`、`worker`。
- Base Compose has one outbound-capable `maimemo-app` Bridge network; proxy attachment is an explicit override。

- [ ] **步骤 1：编写失败的 Compose 契约测试**

测试解析 `docker compose config --format json` 并断言：

```python
assert project_name == 'maimemo'
assert set(services) == {'migrate', 'server', 'mcp', 'worker'}
assert services['mcp'].get('ports') is None
assert services['worker'].get('ports') is None
assert networks['maimemo-app'].get('internal') is not True
```

同时断言 `migrate` 和 `server` 未挂载墨墨 Token；`mcp`、`worker` 各自使用正确独立镜像；服务键不重复 `maimemo-` 前缀。

- [ ] **步骤 2：编写失败的镜像契约测试**

测试三个目标均以 UID/GID 10001、只读根文件系统兼容方式运行，且：

- Server 镜像只包含 Server、核心包、Alembic 配置和迁移文件；
- MCP 镜像不包含 Server 或 Worker 包；
- Worker 镜像不包含 MCP 或 Server 包；
- 三个镜像标签均来自同一个 `MAIMEMO_VERSION=0.2.0`。

- [ ] **步骤 3：运行 Docker 契约测试并确认失败**

Run: `uv run pytest tests/docker -v`

Expected：FAIL，原因是仍为单镜像、双服务 Compose。

- [ ] **步骤 4：实现多阶段 Dockerfile**

基础阶段固定 Python 3.12 digest 和 uv 版本。三个目标分别执行 `uv sync --frozen --no-dev --no-editable --package maimemo-server`、`--package maimemo-mcp` 和 `--package maimemo-worker`；最终阶段只复制目标包及核心包所需的非 editable 环境，不得复制 `.env`、`secrets/`、测试 fixture 或 Git 元数据。

- [ ] **步骤 5：重写基础 Compose**

`name: maimemo`。`migrate` 使用 Server 镜像，设置 `entrypoint: ['maimemo-migrate']` 和 `restart: 'no'`；长期服务不依赖一个永久运行的迁移容器，部署命令显式先运行 `docker compose run --rm migrate upgrade head`，再启动三个服务。

Server 只 `expose: 8080`；MCP 只 `expose: 8000`；Worker 不暴露端口。基础文件不要求尚未创建的 NPM 外部网络；`compose.proxy.example.yaml` 仅说明后续如何把 Server 加入已确认名称的代理网络。

- [ ] **步骤 6：更新 secret 审计脚本**

脚本分别验证 MCP 和 Worker 可以只读两份上游 secret，Server 与 migrate 容器看不到这些挂载。输出仍只包含计数和 UID。

- [ ] **步骤 7：验证 Compose 配置和三个镜像**

Run:

```powershell
$env:MAIMEMO_DATABASE_URL = 'postgresql+psycopg://u:p@db.invalid:5432/maimemo'
docker compose config
docker build --target server -t maimemo-server:test -f docker/Dockerfile .
docker build --target mcp -t maimemo-mcp:test -f docker/Dockerfile .
docker build --target worker -t maimemo-worker:test -f docker/Dockerfile .
uv run pytest tests/docker -v
```

Expected：所有命令成功；配置输出不含真实 secret；MCP 和 Worker 无宿主机端口。

- [ ] **步骤 8：运行容器级健康检查**

启动一次性 PostgreSQL，依次运行 `migrate`、Server、MCP、Worker；验证 Server `/health/ready`、MCP `/health/ready`，并确认 Worker 日志没有 secret 或重复锁错误。使用合成 secret，不连接真实墨墨账户。

- [ ] **步骤 9：提交镜像与 Compose 拆分**

```powershell
git add docker compose.yaml compose.test.yaml compose.proxy.example.yaml .env.example .gitignore scripts tests/docker
git commit -m 'build(docker): 拆分服务镜像和部署编排'
```

### Task 7: 添加 CI 和三镜像发布工作流

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/publish-images.yml`
- Create: `tests/ci/test_workflows.py`
- Modify: `README.md` only for build status and release policy link

**Interfaces:**
- CI triggers: pull requests and pushes to `master`。
- Publish triggers: pushes to `master`、`v*` tags and manual workflow dispatch。
- Publish matrix: `server`、`mcp`、`worker`。
- Required workflow permissions: CI `contents: read`; publish `contents: read` and `packages: write`。

- [ ] **步骤 1：编写失败的工作流契约测试**

测试用 PyYAML 解析工作流并断言：

- CI 执行锁文件检查、Ruff、mypy、完整 pytest、四包构建和三镜像构建；
- 发布工作流只在 `master`、tag 或手动触发时写 GHCR；
- 发布矩阵恰好生成三个设计规定的镜像名；
- 语义版本标签来自同一个 Git tag，另有 `${{ github.sha }}` 标签；
- PR 工作流没有 `packages: write` 权限；
- 第三方 Actions 不引用 `main`、`master` 或 `latest`。

- [ ] **步骤 2：运行工作流测试并确认失败**

Run: `uv run pytest tests/ci/test_workflows.py -v`

Expected：FAIL，原因是 `.github/workflows` 尚不存在。

- [ ] **步骤 3：核验并固定第三方 Action 版本**

从官方仓库核验当前稳定版本，将 Action 固定到完整 commit SHA，并在注释写明版本号。至少包括 `actions/checkout`、`astral-sh/setup-uv`、`docker/setup-buildx-action`、`docker/login-action`、`docker/build-push-action`；不得只写可移动分支。

- [ ] **步骤 4：实现 CI 工作流**

CI 使用 PostgreSQL 15 service，Python 3.12 和锁定 uv；先执行确定性测试，再构建四个 wheel 和三个 Docker target。不得使用真实 secret，数据库和 Token 均使用测试合成值。

- [ ] **步骤 5：实现发布工作流**

构建矩阵分别发布：

```text
ghcr.io/huang-jiping/maimemo-server
ghcr.io/huang-jiping/maimemo-mcp
ghcr.io/huang-jiping/maimemo-worker
```

`master` 构建生成 `sha-${{ github.sha }}` 和 `latest`；`v0.2.0` tag 构建生成相同提交的 `sha-${{ github.sha }}` 与 `0.2.0`。`stable` 不在普通构建中自动更新，必须在 NAS 验收后将三个已验证 digest 成套提升。

- [ ] **步骤 6：验证工作流语法和权限**

Run:

```powershell
uv run pytest tests/ci/test_workflows.py -v
uv run python -c "import yaml, pathlib; [yaml.safe_load(p.read_text(encoding='utf-8')) for p in pathlib.Path('.github/workflows').glob('*.yml')]"
```

Expected：PASS；解析无异常；测试确认 PR 没有写包权限。

- [ ] **步骤 7：提交 CI 与发布工作流**

```powershell
git add .github tests/ci README.md
git commit -m 'ci(release): 添加多包验证和镜像发布'
```

### Task 8: 更新运维文档并执行完整本地验收

**Files:**
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/tunnel-setup.md`
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-10-02-maimemo-learning-data-foundation-design.md`
- Modify: `docs/superpowers/plans/2026-10-02-maimemo-learning-data-foundation.md`
- Create: `docs/migration/maimemo-0.2.0.md`

**Interfaces:**
- Produces: 新包结构、构建命令、三镜像 Compose、迁移服务、NAS 切换和回滚说明。
- Historical documents receive a short migration notice only; their original commands remain historical evidence。

- [ ] **步骤 1：编写文档一致性检查**

扩展架构测试或新增文档测试，断言当前 README、operations、tunnel setup 和 `.env.example` 不再把 `maimemo-mcp` 当作总项目、单一镜像或 Compose 服务键；允许独立模块名和历史文件正文出现旧名称。

- [ ] **步骤 2：运行文档检查并确认失败**

Run: `uv run pytest tests/architecture -v`

Expected：FAIL，原因是当前运维文档仍描述旧单镜像部署。

- [ ] **步骤 3：更新当前文档和迁移说明**

`docs/migration/maimemo-0.2.0.md` 必须包含：

- 旧名到新名映射；
- `migrate → server → mcp → worker` 启动顺序；
- 停旧 Worker 后再启新版；
- GHCR 三个镜像和统一版本；
- 不删除 PostgreSQL 数据；
- 回滚到三个已记录 digest；
- Server 域名和 OAuth 属于阶段 B；
- 当前活跃会话结束后才重命名本地目录。

- [ ] **步骤 4：给历史设计与计划增加迁移提示**

只在文件顶部增加指向新设计和迁移文档的说明，不机械替换历史命令、路径和验收证据。

- [ ] **步骤 5：运行完整 Python 验证**

Run:

```powershell
uv lock --check
uv run ruff check .
uv run mypy packages/maimemo/src packages/maimemo-mcp/src packages/maimemo-server/src packages/maimemo-worker/src
uv run pytest -v
uv build --package maimemo
uv build --package maimemo-mcp
uv build --package maimemo-server
uv build --package maimemo-worker
```

Expected：全部退出码 0，无跳过的必需测试。

- [ ] **步骤 6：运行完整 Docker 验证**

Run:

```powershell
$env:MAIMEMO_DATABASE_URL = 'postgresql+psycopg://u:p@db.invalid:5432/maimemo'
docker compose config
docker build --target server -t maimemo-server:0.2.0-test -f docker/Dockerfile .
docker build --target mcp -t maimemo-mcp:0.2.0-test -f docker/Dockerfile .
docker build --target worker -t maimemo-worker:0.2.0-test -f docker/Dockerfile .
```

Expected：全部退出码 0；记录三个本地镜像 digest。

- [ ] **步骤 7：执行旧名称和敏感信息扫描**

Run:

```powershell
rg -n --hidden --glob '!.git/**' --glob '!docs/superpowers/**' 'maimemo-learning-foundation|image:\s*maimemo-mcp:local|^\s{2}maimemo-(mcp|worker):' .
rg -n --hidden --glob '!.git/**' 'MAIMEMO_TOKEN=|client_secret\s*=|refresh_token\s*=' .
git diff --check
git status --short
```

Expected：无未解释的旧总项目名称，无真实 secret；只存在准备提交的文档改动。

- [ ] **步骤 8：提交文档和迁移说明**

```powershell
git add README.md docs .env.example tests/architecture
git commit -m 'docs(deploy): 更新多服务迁移与运维说明'
```

### Task 9: 合并后重命名仓库、发布镜像并切换 NAS

**Files/External State:**
- Rename: GitHub repository `huang-jiping/maimemo-mcp` → `huang-jiping/maimemo`
- Modify: local Git remote `origin`
- Publish: tag `v0.2.0` and three GHCR images
- Deploy: NAS Compose project `maimemo`
- Later rename: local directory `P:\maimemo-mcp` → `P:\maimemo`

**Interfaces:**
- Consumes: merged `master` with successful CI and Task 8 evidence。
- Produces: renamed repository, three immutable image digests, verified NAS deployment。

- [ ] **步骤 1：合并前执行最终分支检查**

Run:

```powershell
git status --short
git log --oneline --decorate -10
git diff --check master...HEAD
```

Expected：工作区干净，所有实施提交在当前分支，CI 全部通过。

- [ ] **步骤 2：取得外部操作确认**

在执行仓库重命名、推送 tag、发布镜像或切换 NAS 前，向用户列出精确目标并获得当次明确确认。未确认不得执行任何外部变更。

- [ ] **步骤 3：重命名 GitHub 仓库并验证**

Run:

```powershell
gh repo rename -R huang-jiping/maimemo-mcp maimemo --yes
gh repo view huang-jiping/maimemo --json nameWithOwner,url,defaultBranchRef
git remote set-url origin git@github.com:huang-jiping/maimemo.git
git remote -v
git ls-remote origin HEAD
```

Expected：仓库为 `huang-jiping/maimemo`，默认分支仍为 `master`，新 SSH remote 可访问。

- [ ] **步骤 4：创建并推送统一版本 tag**

Run:

```powershell
git tag -a v0.2.0 -m 'maimemo 0.2.0'
git push origin v0.2.0
```

Expected：发布工作流开始运行；若 tag 已存在则停止，不覆盖。

- [ ] **步骤 5：等待发布工作流并核验三个 digest**

使用 `gh run watch` 等待对应工作流结束，再读取 GHCR 元数据。只有三个镜像的 `0.2.0` 和提交哈希标签都存在且 digest 已记录时才能继续。

- [ ] **步骤 6：生成 NAS 最终 Compose 配置**

将三个镜像固定为验收得到的 digest，不使用 `latest` 作为部署或回滚依据。填入现有 PostgreSQL URL 和 secret 文件路径，但不得把实际值提交或粘贴到公开记录。

- [ ] **步骤 7：切换 NAS 项目**

执行顺序：

1. 备份 PostgreSQL，并完成已有空库恢复演练；
2. 拉取三个新镜像；
3. 停止旧 Worker；
4. `docker compose run --rm migrate upgrade head`；
5. 启动 Server、MCP、Worker；
6. 验证 Server、MCP、数据库迁移头和 Worker 最近一次任务；
7. 观察日志确认无 secret、重复任务或连续失败；
8. 验证通过后再移除旧 Compose 项目。

任一步失败都停止推进；恢复旧 Worker 和已记录的旧镜像 digest，不删除或重建数据库。

- [ ] **步骤 8：提升 stable 标签**

只有 NAS 验收完成后，才把三个已验证 digest 提升为 `stable`；三者必须成套更新。

- [ ] **步骤 9：在独立维护窗口重命名本地目录**

关闭当前 Codex 工作区和占用目录的进程，在父目录确认源与目标绝对路径后，将 `P:\maimemo-mcp` 移动为 `P:\maimemo`，再重新登记项目。不得在本实施会话中直接移动活动工作区。

## Plan Self-Review

- Spec coverage：阶段 A 的命名、四包边界、配置拆分、依赖拆分、最小 Server、迁移服务、三镜像、Compose、仓库重命名和 NAS 回滚均有对应任务。
- Scope boundary：阶段 B 只作为 Task 9 后续门禁出现，本计划不实现 OAuth 或 Refresh Token。
- Type consistency：核心资源、四类 Settings、三个运行入口和迁移入口都只定义一次，后续任务引用相同名称。
- Review Focus：五项风险分别由 Task 2 至 Task 7 的明确测试覆盖。
- Proportion：计划描述接口、测试和验收，不预写产品函数体；大范围文件移动集中在可独立评审的核心、Worker、MCP 三个任务。
- Environment risk：当前 `uv`、`gh` 和可用宿主 Python 均未在 PATH 中验证成功，已列为执行前硬门禁；Docker 与 Git 已验证。
