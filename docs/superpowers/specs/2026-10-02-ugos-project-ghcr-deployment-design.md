# UGOS 项目与 GHCR 镜像发布设计

## 1. 文档状态

- 日期：2026-10-02
- 状态：已通过用户确认；Tasks 1–5 已完成本地实现与验证，Task 6 的公开 GHCR 首次发布与 NAS/UGOS 实机验收未完成
- 适用范围：`maimemo-mcp` 在 PING-NAS 上的镜像发布、UGOS Pro Docker 项目部署、数据库迁移、升级与回滚
- 依赖设计：[[2026-10-02-maimemo-learning-data-foundation-design|墨墨学习数据基础设施设计]]

## 2. 背景与目标

本设计提出时，NAS 部署模板使用本地源码和 `build:` 构建镜像，适合命令行部署，但不适合长期通过绿联云 UGOS Pro 的 Docker“项目”模块维护。用户已确认最终运维入口必须是 UGOS Pro Docker 项目，并希望使用项目页面的镜像更新能力，而不是每次通过 SSH 拉取源码和重新构建。Task 5 已将仓库交付模板改为 GHCR 拉取，公开发布与实机验收仍待 Task 6。

本设计的成功标准是：

1. UGOS Pro 中只存在一个名为 `maimemo-mcp` 的 Compose 项目，统一管理 MCP 和 Worker 两个长期运行容器。
2. 正常状态在项目列表中显示两个容器均运行，而不是额外保留一个长期退出的迁移容器。
3. GitHub 发布稳定版本后，UGOS Pro 可以发现并拉取同一镜像标签的新摘要。
4. 数据库迁移在容器启动阶段自动执行；迁移失败时 MCP 不对外就绪，Worker 不开始采集。
5. 每个发布版本都能追溯到源码提交和不可变镜像标签，并具有明确的镜像回退与数据库恢复路径。
6. Token、数据库密码和 Tunnel 凭据不进入 GitHub、镜像、Compose 正文或项目文档。

## 3. 已确认约束

- 镜像仓库使用 GitHub Container Registry，镜像名为 `ghcr.io/huang-jiping/maimemo-mcp`。
- GHCR 包设为公开，使 NAS 可以匿名拉取，不在 UGOS Pro 保存 GitHub Personal Access Token。
- `stable` 是 UGOS Pro 日常更新使用的可变标签；`vX.Y.Z` 和源码提交标签用于追溯与回退。
- PING-NAS 为 `x86_64`，第一阶段只发布 `linux/amd64` 镜像，不为未使用的平台增加多架构构建成本。
- PostgreSQL 数据库名和应用用户名均为 `maimemo`；数据库运行在现有 external `db_net` 中。
- MCP 继续只绑定宿主机 `127.0.0.1:8000`，不加入 `app_net`，不经过 NPM，也不开放公网端口。
- Docker Engine 版本或厂商修复门禁、跨局域网不可达验收、secret 文件权限和 Secure MCP Tunnel 安全要求继续有效，本设计不得弱化现有门禁。
- UGOS Pro 是长期的项目控制面；SSH 只用于准备目录、放置 secret、专项诊断和灾难恢复，不作为日常启动、停止与更新入口。

## 4. 方案选择

### 4.1 采用方案：公开 GHCR + UGOS 项目拉取

发布链路为：

```text
GitHub 版本标签 vX.Y.Z
          │
          ▼
GitHub Actions 质量检查与镜像构建
          │
          ▼
ghcr.io/huang-jiping/maimemo-mcp
  ├─ vX.Y.Z
  ├─ stable
  └─ sha-<完整提交号>
          │
          ▼
UGOS Pro Docker 项目 maimemo-mcp
  ├─ maimemo-mcp
  └─ maimemo-worker
```

选择 GHCR 的原因是源码已经托管在 GitHub，发布工作流可以使用仓库自带的 `GITHUB_TOKEN` 写入包，无需再保存 Docker Hub 凭据。公开 GHCR 包可以匿名拉取，适合 NAS 项目更新。

### 4.2 不采用的方案

- **NAS 本地构建**：仍需 Git、源码目录和 SSH 构建，不能实现以 UGOS Pro 项目为中心的维护。
- **Docker Hub**：技术上可行，但需要额外的账号、发布凭据和仓库维护，不增加当前项目的实际价值。
- **只使用不可变版本标签**：回滚清晰，但 UGOS Pro 无法通过固定标签发现新稳定版本；因此不可变版本标签与 `stable` 必须同时保留。
- **独立迁移容器**：会让 UGOS Pro 项目长期显示两个运行容器和一个已退出容器，不符合项目列表的预期状态。

## 5. 镜像发布设计

### 5.1 触发条件

- Pull Request 和普通分支推送只执行质量检查，不发布生产镜像。
- 仅符合 `v<主版本>.<次版本>.<修订版本>` 的 Git 标签触发正式发布。
- 发布作业必须建立在该标签对应提交的测试、Lint、类型检查和镜像构建均成功的基础上。
- 不允许普通 `master` 推送直接覆盖 `stable`，避免未经版本确认的代码进入 NAS 更新通道。
- 标签对应提交必须属于受保护的 `master` 历史；指向其他分支或游离提交的标签不得发布到稳定通道。
- 发布作业使用不取消运行的串行并发组，避免两个版本同时竞争更新 `stable`。

### 5.2 标签与可追溯性

一次 `v1.2.3` 发布产生同一镜像摘要的三个标签：

- `v1.2.3`：不可变发布标签，不得覆盖；
- `stable`：稳定更新通道，更新为本次发布摘要；
- `sha-<完整提交号>`：源码提交追溯标签。

不使用 `latest`，避免其含义与发布策略不一致。发布流程先写入并核验不可变版本标签和提交标签，最后才移动 `stable`；如果同名版本标签已经存在且摘要不同，发布必须失败，不能覆盖。镜像写入 OCI 源码仓库、版本和提交标签，使运行镜像可以追溯到 GitHub 提交。

### 5.3 权限与供应链

- GitHub Actions 最小权限为 `contents: read` 和 `packages: write`；需要生成来源证明时才启用对应的 `id-token`、attestations 权限。
- 第三方 Actions 必须固定到完整提交 SHA，不使用浮动主版本标签。
- 发布工作流使用 `GITHUB_TOKEN`，不新增长期 GHCR PAT。
- 首次发布后必须核验包已连接到本仓库且可见性为 Public；未完成公开可见性核验前，不在 NAS 配置匿名拉取。
- CI 不接触墨墨 Token、数据库密码、Tunnel Key 或 NAS 凭据。

## 6. UGOS Pro 项目设计

### 6.1 项目结构

UGOS Pro 项目名固定为 `maimemo-mcp`。项目存储目录使用：

```text
/volume3/docker/maimemo-mcp/
├── compose.yaml
├── .env
├── secrets/
│   ├── maimemo_token
│   └── token_fingerprint_key
├── data/
└── backup/
```

不再保留 `app/` 源码目录，也不要求 NAS 安装 Git、Python 或 `uv` 来完成日常升级。`compose.yaml`、`.env` 和相对挂载目录必须位于 UGOS Pro 选择的同一个项目目录，避免相对路径由不同工作目录解析。

删除源码目录后，不得继续在 NAS 调用仓库中的 `scripts/*`：

- OpenAPI 漂移检查整合进长期运行的 Worker，由 Worker 按既定周期执行，并只向专用状态文件写入公开规范元数据；MCP 对该文件保持只读。
- 数据库备份和恢复使用现有 PostgreSQL/pgAdmin 运维入口，备份整个 `maimemo` 数据库及 Alembic revision；不在应用项目中增加长期运行的备份容器。
- secret 所有权、模式和 ACL 审计是首次部署和权限变更后的受控安装步骤，可以通过 SSH 执行安全的元数据检查，但不得读取 secret 内容。
- 实现阶段必须同步删除或改写文档中对 NAS 源码脚本、主机 Python 和 `uv` 的依赖，不能只删除 `app/` 后留下不可执行的运维步骤。

### 6.2 Compose 行为

- 两个服务使用同一镜像：`ghcr.io/huang-jiping/maimemo-mcp:${IMAGE_TAG:-stable}`。
- 仓库 `deploy/nas/compose.yaml` 是交付源；UGOS 导入同一文件，不另维护文档内的独立 Compose。
- 两个服务均为 `pull_policy: always`、UID 1000 / GID 10，限制 1 CPU、512 MiB 内存、128 个进程；日志只用受限 Docker json-file，不挂载备份目录。
- `data/` 源目录由 UID 1000 / GID 10 所有并设为 `0700`；Worker 读写挂载，MCP 只读挂载，原子发布的公开漂移状态文件为 `0644`。
- 删除生产 Compose 中的 `build:`，确保 UGOS Pro 更新只拉取已发布镜像，不在 NAS 隐式构建。
- `maimemo-mcp` 使用 `mcp` 模式，保留健康检查和 `127.0.0.1:8000:8000`。
- `maimemo-worker` 使用 `worker` 模式，不发布端口。
- 两个服务继续加入 external `db_net`，不创建 PostgreSQL 容器。
- Worker 同时使用 Compose 的 `service_healthy` 依赖和应用级 schema 就绪检查；不能只依赖启动顺序。
- MCP 的 ready 健康检查必须验证数据库 schema 与当前镜像兼容，不能只验证数据库可以执行 `SELECT 1`。
- 健康检查的 `start_period` 必须覆盖迁移允许的最大正常时长，避免合法迁移尚未结束时被 UGOS Pro 过早判定为异常。
- 当前模板设为 `6m`，覆盖默认连接/锁/迁移语句预算；增加超时或引入多条耗时迁移时需一并复核健康宽限。
- `restart: unless-stopped`、只读根文件系统、capabilities 删除、`no-new-privileges`、日志轮转和 secret 挂载保持不变。

### 6.3 私有配置

`.env` 至少包含：

```text
IMAGE_TAG=stable
MAIMEMO_DATABASE_URL=postgresql+psycopg://maimemo:REPLACE_WITH_URL_ENCODED_PASSWORD@replace-with-db-net-dns.invalid:5432/maimemo?sslmode=disable
```

上述密码与 `.invalid` DNS 占位符必须在 NAS 本地替换；数据库用户名与库名保持 `maimemo`。真实数据库密码只存在于 NAS 私有 `.env`。墨墨 Token 和稳定的 fingerprint key 继续使用文件 secret。任何真实值都不得粘贴到 UGOS Pro Compose 编辑器、GitHub Issue、Actions 日志、文档或 Git。Tunnel 为后续独立接入，当前私有项目不需要其凭据。

## 7. 自动数据库迁移

### 7.1 启动顺序

`maimemo-mcp` 是唯一的迁移执行者，启动流程为：

```text
读取并验证配置与两份 secret
      ↓
连接 PostgreSQL
      ↓
执行 alembic upgrade head
      ↓ 成功
启动 MCP HTTP 服务并通过健康检查
      ↓
Worker 确认数据库 schema 位于当前 head
      ↓
开始定时采集
```

不得让 MCP 与 Worker 同时执行迁移，也不得依赖两个容器的偶然启动先后关系。Worker 在 schema 未就绪或版本不兼容时必须保持失败重试或退出重启，不得开始读写业务表。

配置和两份 secret 的 UTF-8、非空及可读性检查必须先于数据库迁移，避免数据库已升级但应用随后因 Token 配置错误而无法启动。该检查不得打印 secret 内容。

迁移执行者必须使用 PostgreSQL advisory lock 保证同一数据库同时只有一个迁移过程，并对数据库连接、获取锁和迁移语句设置有界超时。锁必须与迁移使用同一数据库会话持有，并在成功、失败和进程退出时释放。超时或无法取得锁均视为迁移失败，不得绕过门禁启动 HTTP 服务。

Worker 和 MCP 的 schema 门禁必须精确比较数据库 `alembic_version` 与镜像内的唯一 Alembic head，同时验证项目自己的 schema 元数据版本。旧版本、未知的新版本、多个 head、缺失版本表或元数据不一致都必须拒绝业务运行；不能把“数据库连通”当成“schema 可用”。

### 7.2 失败行为

- 数据库不可达、凭据错误、迁移失败或 schema 版本异常时，MCP 进程以非零状态退出，不监听 8000 端口。
- MCP 未健康时 Worker 不开始采集；即使 UGOS Pro 对 Compose 依赖条件支持不完整，Worker 的应用级检查也必须阻止业务运行。
- 错误日志只输出迁移阶段、异常类别和安全摘要，不输出数据库 URL、密码或 SQL 参数。
- 自动迁移不得自动执行 downgrade、删除数据库、清表或从备份恢复。
- `restart: unless-stopped` 会使可恢复的数据库短暂故障自动重试；持续配置错误或迁移错误会表现为重启循环。运维人员应在 UGOS Pro 日志确认安全错误类别后停止项目、修复配置或恢复数据库，再重新启动，不通过无限重试掩盖错误。

### 7.3 迁移兼容性

- 每个版本发布前必须验证从上一稳定版本 schema 升级到当前 head。
- 优先使用可向前兼容、可重复执行的渐进式迁移，避免同一次发布中立即删除旧版本仍可能访问的列或表。
- UGOS Pro 更新期间不假定严格的滚动更新或零停机；个人服务允许短暂停机，以数据一致性优先。
- 常规更新要求新 schema 至少与上一稳定版本的运行进程暂时兼容，以覆盖 UGOS Pro 在重建容器期间旧 Worker 仍短时运行的情况。
- 包含破坏性 schema 变更的版本不得走普通“一键更新”：必须先停止整个 UGOS Pro 项目，完成备份后进入维护升级流程。
- 若迁移对上一版本不向后兼容，发布说明必须标记“镜像不能单独回退”，并要求更新前数据库备份。

## 8. 更新与回滚

### 8.1 日常更新

1. 在 GitHub 发布新的 `vX.Y.Z` 标签。
2. CI 完成检查并将相同摘要发布为版本标签、提交标签和 `stable`。
3. 核验 GHCR 包摘要与发布提交一致。
4. 更新前生成 PostgreSQL 备份并记录当前版本、完整镜像摘要、Git SHA、Alembic revision、备份文件及最近一次恢复验证结果。
5. 在 UGOS Pro Docker 项目中执行镜像更新/重新部署。
6. 确认项目显示 `2 / 2`、MCP 健康、数据库 revision 正确、Worker 产生新的成功采集记录。
7. 从同一局域网其他主机复核 NAS 的 8000 端口仍不可达；只有后续已接入 Tunnel 时才验证 Tunnel 端 MCP。

“一键更新”只指镜像拉取和项目重新部署入口集中在 UGOS Pro，不代表跳过备份、发布说明审阅和更新后验收。

### 8.2 镜像回退

如果数据库 schema 仍兼容旧版本：

1. 停止项目；
2. 将 `IMAGE_TAG` 从 `stable` 改为上一个不可变版本，例如 `v1.2.2`；
3. 在 UGOS Pro 中重新部署；
4. 核验两个容器、健康检查与 Worker；后续已接入 Tunnel 时再核验 Tunnel。

旧镜像是否兼容当前 schema 必须由相应版本组合测试证明，不能仅根据版本号推断。实际回退记录以镜像 digest 为最终证据，版本标签只作为可读入口。

### 8.3 数据库 downgrade

只有对应 downgrade 路径已经在生产数据副本上完成无损演练时，才允许人工执行数据库 downgrade。自动启动流程永远不执行 downgrade；任何迁移脚本明确拒绝的降级都不得强行绕过。

### 8.4 数据库恢复

如果新迁移不兼容旧镜像，禁止只切换镜像标签。必须停止项目，恢复更新前数据库备份，再使用对应旧版本镜像启动。数据库恢复保持独立人工步骤，不由容器启动脚本自动执行。

## 9. 测试与验收

### 9.1 自动化测试

- 现有单元、集成测试、Ruff 和 MyPy 全部通过后才允许发布。
- 增加运行时测试：MCP 在迁移成功后启动，迁移失败时返回非零且不启动 HTTP 服务。
- 增加 Worker schema 门禁测试：旧 schema、未知新 schema 和不可达数据库均不得开始采集。
- 增加迁移并发与超时测试：并发启动只有一个迁移执行者，锁超时、连接超时和语句失败均不得启动服务。
- 增加 Compose 测试：生产模板不含 `build:`，使用 GHCR 镜像，保留 external `db_net`、loopback 端口和安全选项。
- 增加发布元数据测试：版本标签格式、OCI labels、`stable` 与版本标签指向同一摘要。
- 增加迁移路径测试：空库升级、上一稳定 revision 升级和重复启动均成功。
- 增加 secret 前置门禁测试：任一 secret 不可读、空值或编码错误时不得开始迁移。
- 增加运维断链测试：部署文档和 NAS 模板不再引用不存在的源码目录、主机脚本或本地构建命令。

### 9.2 发布验收

- GHCR 能匿名拉取 `linux/amd64` 的版本标签和 `stable`。
- 镜像运行身份仍为 UID 1000 / GID 10，能读取两份 secret 且不能写入。
- UGOS Pro 成功导入 Compose，项目列表正常显示 `maimemo-mcp`，运行状态为 `2 / 2`。
- UGOS Pro 实机确认所用 Compose 版本支持长格式 `depends_on`；即使不支持，应用级门禁仍能阻止 Worker 在 schema 未就绪时采集。
- 首次部署自动创建 schema；重复部署不会破坏已有数据。
- 更新后 `/health/ready`、`/health/status`、Alembic revision 和 Worker 采集均通过。
- Docker Engine 修复门禁与跨 LAN 不可达测试通过后，才允许连接 Secure MCP Tunnel。
- 使用一个测试发布完成从 `stable` 更新、切换到旧版本标签以及必要时的数据库恢复演练。
- 更新后确认 MCP 与 Worker 实际运行同一新 digest，`.env`、secret 和持久数据没有被重建或覆盖。

## 10. 文档与实现影响

Tasks 1–5 的本地实现涉及（不代表 Task 6 发布或 NAS 实机验收已经通过）：

- 新增 GitHub Actions 质量检查和 GHCR 发布工作流；
- 调整 NAS Compose 与环境变量示例，删除生产 `build:`；
- 增加 MCP 自动迁移和 Worker schema 门禁；
- 将 OpenAPI 漂移检查纳入 Worker，并调整 MCP/Worker 对状态目录的读写权限；
- 更新部署、升级、回滚、备份和 UGOS Pro 项目操作文档；
- 补充运行时、Compose、迁移和发布配置测试。

本设计不改变墨墨 API 业务工具、学习数据模型、MCP 对外工具契约或 Obsidian 投影范围。

## 11. 外部依据

- UGREEN 官方 Docker/Compose 指南：<https://ai.ugreen.com/blogs/knowledge/docker-docker-compose-ugreen-nas>
- GitHub 官方容器镜像发布指南：<https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images>
- GitHub Packages 权限说明：<https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages>
