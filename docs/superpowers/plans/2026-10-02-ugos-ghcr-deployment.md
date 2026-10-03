# UGOS Project and GHCR Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `maimemo-mcp` 发布为可追溯的公开 GHCR 镜像，并提供可由绿联云 UGOS Pro Docker“项目”模块完成首次部署、日常更新和版本回退的生产配置。

**Architecture:** GitHub Actions 对 `master` 历史上的 SemVer 标签执行完整质量检查，发布 `linux/amd64` 的不可变版本标签、完整提交标签和最后更新的 `stable` 标签。NAS 上只保留 UGOS 项目 Compose、私有环境文件、secret、状态和备份，不再本地构建源码；MCP 启动前以单执行者锁自动迁移，Worker 和健康检查共同执行精确 schema 门禁，OpenAPI 漂移检查由 Worker 内部调度。

**Tech Stack:** Python 3.12、Alembic、SQLAlchemy/psycopg、PostgreSQL 17、Docker Compose、GitHub Actions、GHCR、UGOS Pro

**Spec:** `docs/superpowers/specs/2026-10-02-ugos-project-ghcr-deployment-design.md`

## Global Constraints

- 生产镜像固定为 `ghcr.io/huang-jiping/maimemo-mcp`，第一阶段只发布 `linux/amd64`。
- UGOS 项目名、MCP 容器名和 Worker 容器名分别固定为 `maimemo-mcp`、`maimemo-mcp`、`maimemo-worker`。
- PostgreSQL 数据库和应用用户均为 `maimemo`，通过 external `db_net` 的 Docker DNS 访问，不暴露数据库宿主机端口。
- MCP 只发布 `127.0.0.1:8000:8000`，不加入 `app_net`，不接 NPM，不开放公网入站。
- Docker Engine Server 必须 `>=28.0.0`，或存在厂商可核验的 localhost 端口修复回补；否则禁止启动应用和连接 Tunnel。
- 真实 Token、数据库密码、fingerprint key 和 Tunnel 凭据不得进入 Git、GitHub Actions、镜像、Compose 正文、文档或聊天记录。
- `stable` 只由已验证且属于 `master` 历史的 `vX.Y.Z` 发布更新；不可变版本标签不得覆盖。
- 破坏性数据库迁移必须走停止整个项目的维护升级，不得作为普通 UGOS 一键更新。
- 所有实现使用 TDD；每个任务只提交本任务文件，并使用 Conventional Commits 中文提交信息。

## Review Focus

- 空、旧、未知新、多个 head 或 `schema_metadata` 不一致的数据库必须拒绝就绪和采集；Task 1 的集成测试覆盖全部状态。
- 数据库不可达、DDL 权限不足、迁移锁超时或迁移异常不得让 MCP 监听 8000；Task 2 的运行时测试覆盖这些失败。
- secret 不可读、空值或非法 UTF-8 时必须在迁移前失败且不泄漏内容；Task 2 的单元测试固定顺序和脱敏输出。
- 两个版本并发发布时不得竞态覆盖 `stable`，非 `master` 历史标签和已存在版本标签必须失败；Task 4 的发布策略测试覆盖。
- 删除 NAS 源码目录后，部署和运维文档不得继续依赖 `app/`、主机 Python、`uv` 或 `scripts/*`；Task 5 的文档契约测试覆盖。

---

### Task 1: 精确 schema 兼容性门禁

**Files:**
- Create: `src/maimemo_mcp/storage/schema.py`
- Modify: `src/maimemo_mcp/mcp_server/health.py`
- Create: `tests/unit/test_schema_state.py`
- Modify: `tests/integration/test_migrations.py`
- Modify: `tests/mcp/test_server_foundation.py`

**Interfaces:**
- Produces: `expected_schema_revision(config_path: Path = Path("alembic.ini")) -> str`
- Produces: `inspect_schema(engine: AsyncEngine, expected: str) -> SchemaState`
- Produces: `require_current_schema(engine: AsyncEngine, expected: str) -> None`
- Produces: `SchemaDefinitionError` and `SchemaNotReadyError` with fixed safe messages.
- Produces: immutable `SchemaState(alembic_revisions: tuple[str, ...], metadata_revisions: tuple[str, ...], expected_revision: str, status: SchemaStatus)`; Task 2 runtime and health checks consume it.

- [ ] **Step 1: Write failing unit tests for the migration head resolver**

  Assert that the shipped Alembic directory has exactly one head (`0004` at the current revision) and that zero or multiple heads raise `SchemaDefinitionError` without embedding filesystem or credential data.

- [ ] **Step 2: Write failing integration tests for database state classification**

  Cover current head, empty schema, missing `schema_metadata`, old revision, unknown newer revision, multiple `alembic_version` rows and mismatched metadata; assert only the exact single-head/current-metadata state passes.

- [ ] **Step 3: Run focused tests and confirm RED**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_schema_state.py tests/integration/test_migrations.py -q`

  Expected: FAIL because `storage.schema` and its interfaces do not exist.

- [ ] **Step 4: Implement `storage/schema.py` and reuse it in health readiness**

  Resolve the expected head from the packaged `alembic.ini`/migration directory, query both `alembic_version` and `schema_metadata`, map failures to fixed safe reason codes, and change `/health/ready` to return `503 schema_incompatible` unless the exact state is current. Keep `/health/status` informational fields but derive compatibility from the same module.

- [ ] **Step 5: Run schema and health tests and confirm GREEN**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_schema_state.py tests/mcp/test_server_foundation.py tests/integration/test_migrations.py -q`

  Expected: PASS.

- [ ] **Step 6: Commit**

  Commit: `feat(database): 添加精确数据库版本门禁`

### Task 2: 自动迁移与 Worker 启动屏障

**Files:**
- Create: `src/maimemo_mcp/migration_runner.py`
- Modify: `src/maimemo_mcp/config.py`
- Modify: `src/maimemo_mcp/storage/database.py`
- Modify: `src/maimemo_mcp/runtime.py`
- Modify: `migrations/env.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_runtime.py`
- Create: `tests/integration/test_startup_migrations.py`

**Interfaces:**
- Consumes: Task 1 `expected_schema_revision()` and `require_current_schema()`.
- Produces: `run_upgrade(settings: Settings, config_path: Path = Path("alembic.ini")) -> None`, injecting the already locked SQLAlchemy connection through `AlembicConfig.attributes["connection"]`.
- Produces: configuration fields `database_connect_timeout_seconds=10`, `migration_lock_timeout_seconds=30`, `migration_statement_timeout_seconds=300`, `schema_wait_timeout_seconds=60`.
- Produces: MCP startup sequence “validate config/secrets → migrate → verify schema → serve” and Worker sequence “bounded wait → verify schema → collect”.

- [ ] **Step 1: Write failing configuration and runtime order tests**

  Assert timeout defaults and validation bounds; assert invalid/empty secret files fail before `run_upgrade`; assert MCP never calls `uvicorn.run` when migration fails; assert Worker never enters `run_forever` before schema verification succeeds.

- [ ] **Step 2: Write failing PostgreSQL integration tests**

  Cover empty-database first start, repeat start at head, upgrade from previous revision, concurrent migration attempts, lock timeout, DDL permission failure and database-unavailable failure. Assert failure exits nonzero, logs only fixed categories and never starts the HTTP server.

- [ ] **Step 3: Run focused tests and confirm RED**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_runtime.py tests/integration/test_startup_migrations.py -q`

  Expected: FAIL because the migration runner and startup gates do not exist.

- [ ] **Step 4: Implement bounded database connection and migration locking**

  Pass psycopg `connect_timeout=10` through engine creation. Acquire fixed advisory key `21745489128041807` on one connection with bounded retry up to `migration_lock_timeout_seconds`, inject that same connection into Alembic, apply PostgreSQL `statement_timeout`, run the transactional migration, and release the session lock in `finally`. Update `migrations/env.py` to use an injected connection when present and preserve its existing standalone CLI path otherwise.

- [ ] **Step 5: Implement runtime startup gates**

  Load and validate both secret files before migration. Run synchronous Alembic work outside the event loop, verify the exact schema after upgrade, then start MCP. Worker waits at most `schema_wait_timeout_seconds`, verifies the exact schema and exits nonzero on timeout or incompatibility so Compose can restart it.

- [ ] **Step 6: Run focused tests and confirm GREEN**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config.py tests/unit/test_runtime.py tests/integration/test_startup_migrations.py tests/integration/test_worker_schema.py -q`

  Expected: PASS.

- [ ] **Step 7: Commit**

  Commit: `feat(runtime): 启动前自动迁移并阻止旧结构运行`

### Task 3: 将 OpenAPI 漂移检查纳入 Worker

**Files:**
- Create: `src/maimemo_mcp/openapi_drift.py`
- Create: `src/maimemo_mcp/ingestion/drift_monitor.py`
- Modify: `src/maimemo_mcp/runtime.py`
- Modify: `src/maimemo_mcp/config.py`
- Modify: `scripts/check_openapi_drift.py`
- Modify: `tests/unit/test_openapi_drift.py`
- Create: `tests/unit/test_drift_monitor.py`
- Modify: `tests/integration/test_drift_permissions.py`

**Interfaces:**
- Produces: reusable drift parsing/comparison/state-write functions in `maimemo_mcp.openapi_drift`; the existing script becomes a thin local wrapper.
- Produces: `OpenApiDriftMonitor.run_forever() -> None`, scheduled every six hours with an immediate startup check.
- Consumes: existing `MAIMEMO_OPENAPI_DRIFT_STATE_FILE`; Worker owns write access, MCP retains read-only access.

- [ ] **Step 1: Move existing drift behavior behind failing package-level tests**

  Update tests to import the application module, preserving existing severity, hash, size, atomic-write and safe-error behavior; add monitor tests for immediate run, six-hour cadence, recoverable upstream failure and cancellation.

- [ ] **Step 2: Run focused tests and confirm RED**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_openapi_drift.py tests/unit/test_drift_monitor.py tests/integration/test_drift_permissions.py -q`

  Expected: FAIL because the package module and monitor do not exist.

- [ ] **Step 3: Extract drift logic and run it beside the ingestion Worker**

  Preserve the local script as a wrapper for manual diagnostics. Start ingestion and drift monitor in one structured async lifetime; drift fetch/parse failures update safe state/logging and retry next interval without stopping learning-data collection.

- [ ] **Step 4: Run focused tests and confirm GREEN**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_openapi_drift.py tests/unit/test_drift_monitor.py tests/integration/test_drift_permissions.py tests/unit/test_runtime.py -q`

  Expected: PASS.

- [ ] **Step 5: Commit**

  Commit: `feat(worker): 内置 OpenAPI 漂移监测任务`

### Task 4: 建立 CI 与 GHCR 稳定发布流水线

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/publish-image.yml`
- Create: `scripts/validate_release.py`
- Create: `tests/unit/test_release_workflow.py`
- Modify: `Dockerfile`

**Interfaces:**
- Produces: PR/branch CI gate and `vX.Y.Z` tag release workflow.
- Produces: image tags `vX.Y.Z`, `sha-<完整40位Git SHA>` and `stable` with one manifest digest.
- Consumes: repository `GITHUB_TOKEN` with `contents: read` and `packages: write`; no application secrets.

- [ ] **Step 1: Write failing workflow contract tests**

  Parse both workflow YAML files with a YAML mode that does not reinterpret the `on` key and assert Python 3.12, locked dependencies, full pytest/Ruff/MyPy checks, `linux/amd64`, exact GHCR name, minimal permissions, full-SHA-pinned Actions, SemVer-only release trigger, protected-`master` ancestry validation, serial release concurrency and `stable` promotion after immutable tag publication.

- [ ] **Step 2: Write failing release validation tests**

  Test `validate_release.py` against valid `v0.1.0`, malformed tags, non-`master` commits and an already-existing immutable GHCR tag. An existing tag with the same digest is an idempotent retry; a different digest must fail. The validator must use safe fixed messages.

- [ ] **Step 3: Run tests and confirm RED**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_release_workflow.py -q`

  Expected: FAIL because workflows and validator do not exist.

- [ ] **Step 4: Implement CI and publication**

  CI starts the existing test PostgreSQL, then runs full pytest, Ruff, MyPy and Docker build. Publication repeats required quality gates, verifies tag ancestry and immutability, builds/pushes version and full-SHA tags, records the manifest digest, verifies both immutable tags resolve to it, then promotes that digest to `stable` as the final step. Add OCI source, revision and version labels; pin every external Action to a verified full commit SHA.

- [ ] **Step 5: Run workflow contract tests and local image build**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_release_workflow.py -q`

  Run: `docker build -t maimemo-mcp:ugos-candidate .`

  Expected: tests PASS and image build exits 0.

- [ ] **Step 6: Commit**

  Commit: `ci(release): 添加 GHCR 稳定镜像发布流程`

### Task 5: 改造 UGOS Compose 与运维文档

**Files:**
- Modify: `deploy/nas/compose.yaml`
- Modify: `deploy/nas/.env.example`
- Modify: `tests/unit/test_nas_deployment.py`
- Modify: `DEPLOYMENT.md`
- Modify: `docs/operations.md`
- Modify: `docs/tunnel-setup.md`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-10-02-ugos-project-ghcr-deployment-design.md`

**Interfaces:**
- Consumes: Task 2 startup migration and schema gates, Task 3 Worker drift writer, Task 4 GHCR tags.
- Produces: 可直接粘贴或导入 UGOS Pro 的 `deploy/nas/compose.yaml` 以及不含秘密值的 `.env.example`。

- [ ] **Step 1: Rewrite NAS deployment tests for the desired UGOS project**

  Assert exactly two services, same `ghcr.io/huang-jiping/maimemo-mcp:${IMAGE_TAG:-stable}` image, `pull_policy: always`, no `build:`, MCP loopback port, Worker `service_healthy` dependency, Worker read-write drift state mount, MCP read-only mount, external `db_net`, existing hardening, 6-minute migration-aware health start period and relative secret files.

- [ ] **Step 2: Add failing documentation contract tests**

  Assert UGOS Project/Create/import/update/rollback instructions exist; reject production references to cloning `app/`, local image builds, host `uv`, scheduled `scripts/check_openapi_drift.py` or manual pre-start Alembic migration. Preserve Docker Engine and cross-LAN isolation gates.

- [ ] **Step 3: Run NAS tests and confirm RED**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_nas_deployment.py -q`

  Expected: FAIL on the old local-build Compose and CLI deployment guide.

- [ ] **Step 4: Implement the UGOS project template**

  Set `IMAGE_TAG=stable`, database URL example to user/database `maimemo` with a Docker-DNS placeholder, split data mounts by service permissions, add Worker health dependency and remove local build context. Keep real credentials only in `.env` and the two secret files.

- [ ] **Step 5: Rewrite deployment, update, backup and rollback guidance**

  Make UGOS Pro the normal control plane and mark the approved design status accordingly. Use existing PostgreSQL/pgAdmin for full-database backup/restore, document normal versus maintenance upgrades, image digest recording, immutable-version rollback, secret ACL verification and the unchanged Tunnel/localhost security gate.

- [ ] **Step 6: Run NAS tests and confirm GREEN**

  Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_nas_deployment.py -q`

  Expected: PASS.

- [ ] **Step 7: Commit**

  Commit: `feat(deploy): 提供 UGOS 项目部署模板`

### Task 6: 全量验收、远端集成与首个镜像发布

**Files:**
- Modify only if verification exposes an in-scope defect; otherwise no source changes.
- Generated external artifacts: GitHub pull request, proposed first release tag `v0.1.0`, public GHCR package.

**Interfaces:**
- Consumes: Tasks 1-5 complete branch.
- Produces: merged `master`, public `ghcr.io/huang-jiping/maimemo-mcp:v0.1.0` and `:stable`, and final user-facing UGOS Compose/configuration checklist. Approval of this plan also confirms the initial version `v0.1.0`, matching `pyproject.toml`.

- [ ] **Step 1: Run full local quality gates**

  Run: `.venv\Scripts\python.exe -m pytest -q`

  Run: `.venv\Scripts\ruff.exe check .`

  Run: `.venv\Scripts\mypy.exe src`

  Run: `docker build -t maimemo-mcp:ugos-release-candidate .`

  Expected: all commands exit 0; record exact counts and image ID.

- [ ] **Step 2: Run security and configuration checks**

  Run staged/branch diff checks, secret-pattern scan, container UID/GID and read-only filesystem smoke tests, Compose parse, database migration smoke test and loopback binding inspection. Do not print expanded Compose containing the database URL.

- [ ] **Step 3: Request independent whole-branch review**

  Block release on any Critical or Important finding; fix in-scope issues with focused tests and repeat all affected gates.

- [ ] **Step 4: Push the feature branch and create a pull request**

  Attach the PR to this task, wait for GitHub CI, and merge only after required checks pass. Do not force-push or bypass failed checks.

- [ ] **Step 5: Tag and publish `v0.1.0`**

  Create an annotated `v0.1.0` tag from the merged `master` commit, push it, wait for the publication workflow, and verify version/full-SHA/`stable` all resolve to the same manifest digest. Confirm the GHCR package is Public and anonymous `linux/amd64` pull succeeds; if GitHub does not inherit public visibility from the public repository, change package visibility through the authenticated GitHub package settings before the anonymous-pull check.

- [ ] **Step 6: Deliver the NAS deployment package**

  Provide the final `compose.yaml`, `.env` field checklist and secret file paths. Explicitly identify values the user must fill locally: PostgreSQL Docker DNS, database password,墨墨 Token and Tunnel credentials. Do not request secret values in chat.

- [ ] **Step 7: Record remaining live-NAS gates**

  Mark Docker Engine/backport evidence, actual `db_net` alias, port 8000 availability, UGOS `2 / 2`, automatic migration, database backup/restore, cross-LAN isolation and Secure MCP Tunnel as unverified until executed on PING-NAS.
