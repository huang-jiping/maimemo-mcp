# 运维手册

NAS 的日常控制面是 UGOS Pro **Docker → 项目 → 创建/导入**：使用仓库
[deploy/nas/compose.yaml](../deploy/nas/compose.yaml)，项目名 `maimemo-mcp`，正常状态为
`2 / 2`。两个容器拉取同一 `ghcr.io/huang-jiping/maimemo-mcp:${IMAGE_TAG:-stable}`；
MCP 自动迁移并校验 schema，Worker 等待健康再采集。部署入口见
[DEPLOYMENT.md](../DEPLOYMENT.md)。以下 1–6 节为生产运维，7–10 节为开发工作站的独立
诊断/评测证据，不能当作 NAS 的部署依赖。首个公开镜像已发布：`v0.1.0`、提交标签和
`stable` 已通过匿名清单查询，均指向
`sha256:2fc31dcab9d514d13b1abf3da499ae1ddd64e8d7e92a72743b4f18f1bdbf0680`，镜像为
`linux/amd64` 且运行用户为 `1000:10`。NAS/UGOS 实机未验证，仍须完成以下门禁。

## 1. 部署前准备

先执行 `docker version`，记录 **Server / Engine >=28.0.0**，或 NAS **厂商明确回补**
localhost 发布漏洞的可核验证据；不能用 Client/Compose 版本替代。
[Docker 官方说明](https://docs.docker.com/engine/network/port-publishing/)指出旧版本可能
允许同一 L2 网段访问回环发布端口。**两项均不满足时禁止启动应用**，并禁止连接 Tunnel。
另一台同一 LAN 主机验证 NAS_IP:8000 不可达只作纵深检查，**不能替代版本/回补门禁**，
不覆盖旧版 localhost 发布漏洞。任何 HTTP 响应都不算网络隔离通过，失败后停止整个项目。

数据库与应用用户均为 `maimemo`，使用已有 PostgreSQL 15+ 与 external `db_net`。
通过 `docker network inspect db_net` 确认网络与成员可信、存在墨墨 API 出站路径。
用户仅在本地填写真实 Docker DNS（网络内别名）、数据库密码、Token 文件和 fingerprint key
文件；不向聊天提供秘密。`:5432` 是容器端口，不猜固定 IP 或宿主机映射。
从 NAS 环境示例建立本地 `.env`（0600），URL 中口令需标准 percent 编码，含 `$` 的完整
URL 使用单引号；配置预检只用 `docker compose config --quiet`，不打印展开环境变量。

项目目录为 `/volume3/docker/maimemo-mcp`，Compose、`.env`、`secrets/`、`data/`、
`backup/` 在一起。secret 源文件 UID 1000 / GID 10、`0400`，父目录 `0700` 且允许 UID
1000 遍历；核对 NAS ACL，去除其他非管理员主体访问，避免 GID 10 的管理员组可读。
file-source secret 的 uid/gid/mode 不会重映射源文件权限，见
[Compose 文档](https://docs.docker.com/reference/compose-file/services/#secrets)。
在 UGOS 容器终端执行 `id` 及两份 `/run/secrets/` 文件的 `test -r`、`test ! -w`，只记录
退出码，不读取内容；结果应为 UID 1000 / GID 10、均可读且只读。首次启动还验证非空 UTF-8。

`data/` 由 UID 1000 / GID 10 所有、`0700`，Worker 可写，MCP 只读；原子发布的公开状态
为 `0644`。日志只用有上限的 Docker json-file，备份目录不挂载到容器。根文件系统只读、
capabilities 全部删除、no-new-privileges，单容器限制 1 CPU、512 MiB、128 进程。
真实 secret、个人响应、dump 不进 Git、镜像、Compose 正文或日志。

## 2. UGOS 首次启动与健康检查

1. 完成 Engine/网络/权限门禁，确认 GHCR 已公开、匿名 amd64 拉取通过。
2. 在 UGOS Pro Docker → 项目 → 创建/导入，选同一项目目录与交付文件。
3. 拉取并启动项目。MCP 是唯一自动迁移执行者，使用数据库锁与有界超时；失败不监听
   8000。健康宽限为 6 分钟，改变迁移/超时预算时重新核对。
4. Worker 的 `service_healthy` 依赖和应用 schema 门禁同时生效；实机核验 UGOS 对依赖和
   资源限制的支持。MCP 未健康、旧 schema、未知新 revision 均不得采集。
5. 核对 `2 / 2`、MCP healthy、两容器日志，以及最近成功采集和连续失败数。

NAS 本机访问 `/health/ready` 与 `/health/status`，地址为 `http://127.0.0.1:8000`。
就绪要求 `alembic_version` 与镜像唯一 head 精确一致、`schema_metadata` 一致。Worker
无 HTTP 健康端口，用结构化日志、`ingestion_run` 与 MCP 数据健康工具观察。首次采集可能
尚未完成，不能把进程运行当作已得到学习数据。
记录两个容器实际 digest、版本、完整 Git SHA、数据库 revision 和验收时间；必须同摘要。
从另一台同一 LAN/L2 主机执行 `nc -vz -w 3 NAS_IP 8000`，应拒绝/超时；先确认该主机能
访问一个已知允许的 NAS 服务，多个 LAN 接口逐一检查。
日志出现凭据/个人正文时停止整个项目、轮换相关凭据并仅保留安全时间和 digest 证据。
持续重启循环先在 UGOS 停止项目并检查安全错误类别，不绕过迁移或配置门禁。

### 2.1 Worker 漂移监测与告警

Worker 启动时检查公开 OpenAPI，此后每六小时执行；无需额外主机定时任务。
状态写入 `/var/lib/maimemo/openapi-drift.json`，MCP 读取 `/health/status` 的
`drift_status`。high 优先调查，informational 安排审阅；unavailable/stale 查看 Worker
日志、ACL 和最近时间，超过 26 小时未成功发布为 stale。失败只记录安全错误类别，下一
周期重试，不终止采集；由现有 NAS 告警渠道关注连续失败。状态仅含公开 hash/时间/severity。

## 3. 后续 Tunnel 接入

当前私有部署不需要 Tunnel 凭据，也没有 Tunnel sidecar。后续使用受监督的原生客户端
连接 `http://127.0.0.1:8000/mcp`，凭据仅存 NAS 本地；按
[Tunnel 说明](tunnel-setup.md)重新核验官方版本、供应链与平台权限。Engine/回补和跨 LAN
验收必须先通过，失败禁止连接。MCP 只发布 `127.0.0.1:8000:8000`，不加入 `app_net`、
不接 NPM、不开放公网。`MAIMEMO_MCP_ALLOWED_HOSTS=maimemo-mcp` 只放行精确服务名，
不改通配。`db_net` 内容器能访问 MCP，必须限制并信任其成员。Tunnel 失联不影响 Worker。

## 4. PostgreSQL 备份

沿用用户现有 PostgreSQL/pgAdmin 工作流备份整个 `maimemo` 数据库。应用用户和数据库名
均为 `maimemo`，管理员连接信息在本地保存，不贴入命令行或记录中。不要仅备份状态目录、
某张表或 schema-only；必须包括全部表、数据、迁移元数据、失败快照与反馈撤销链。

在 pgAdmin 对数据库执行 Backup，优先 Custom 格式，选择全库且包含数据；数据库管理员
核对工具/服务器主版本兼容，保存数据库 owner/权限和应用角色 `maimemo` 的重建要求。
使用唯一文件名避免覆盖，保存到 `/volume3/docker/maimemo-mcp/backup/`，目录 0700 与受限
ACL，dump 为敏感个人数据。另行加密备份 `.env`、两份 secret（fingerprint key 保持稳定）、
Compose 与必要状态，按保留策略复制至 NAS 外部介质。
记录备份时间、镜像 digest/版本/Git SHA、`alembic_version`、`schema_metadata` 与校验结果。
备份成功不能替代恢复演练；只有实际恢复和一致性检查通过才记为可恢复。

## 5. 空库恢复演练与灾难恢复

数据库管理员通过 pgAdmin 在隔离环境创建独立新空库，使用 Restore 导入 custom dump，
不覆盖已有生产库。按本地保存的权限记录核对 owner、应用用户 `maimemo` 的 DDL/读写权限。
只读核对关键表集合、行数、非空 snapshot hash、反馈撤销链、隔离失败证据，以及
`alembic_version` 和 `schema_metadata` 的对应 revision；保存演练时间与安全结果。
迁移 head 按备份版本确认，不能把当前 `0004` 硬套在所有历史备份上。演练库不连接采集
Worker；成功后由管理员明确点名处置，失败保留安全错误与现场，不自动删除已有库。

生产恢复必须先停止整个项目并核实两个容器停止，由管理员确认精确目标与备份，再恢复
完整数据库；检查一致性和权限后启用对应镜像。恢复会改写数据，不得在项目运行时操作。
不得用清表绕过 downgrade 保护；0002/0003 对不可表示数据、0004 对非空失败证据拒绝降级。

## 6. 更新与回退

正常兼容更新：审阅发布说明，完成全库备份与恢复演练，记录当前 digest。使用本地
`IMAGE_TAG=stable`，在 UGOS 项目一键拉取/更新/重建；保留 `.env`、secret 和持久目录。
验收同一新 digest、`2 / 2`、健康、日志、revision、最近成功采集与跨 LAN 不可达。

破坏性或未证明与上一运行版本兼容的迁移：先停止整个项目，确认全库备份与恢复演练，
再按维护升级流程指定版本重建；不能在旧 Worker 仍运行时迁移。迁移失败保持停机并调查。
普通一键更新不代表零停机，也不省略备份和验收。

回退前验证旧镜像与当前精确 schema 的组合；新 revision 往往被旧镜像拒绝，不能只凭
版本号判定。停止整个项目，`IMAGE_TAG` 改为上一不可变 `vX.Y.Z`，拉取并重建，再核对旧
记录 digest 和所有关键路径。如果 schema 不兼容，镜像不能单独回退：保持停机，先恢复
更新前整个数据库，再启动对应旧镜像。自动启动永不 downgrade、清表或恢复备份。

## 7. 开发工作站的只读冒烟与协议检查

本节脚本只在保留源码和锁定开发依赖的受控工作站运行，不在 UGOS 项目目录执行，也不是
NAS 首次部署、更新或日常运维的前置条件。

真实接口冒烟必须同时满足两个门禁：用户明确提供只读 Token 文件，且命令显式带
`--confirm-readonly`。脚本只运行代码内固定的 17 项 allowlist；不要修改为从服务发现结果
自动执行工具。未知名称和墨墨写操作必须在网络请求前拒绝。

```text
uv run python scripts/smoke_readonly_api.py --confirm-readonly
uv run python scripts/smoke_readonly_api.py --confirm-readonly \
  --operation get_study_progress --operation get_today_items
```

脚本绝不打印或保存个人响应及异常正文，只输出每项的状态、毫秒耗时和记录数。
按账户 ID 查询的操作必须显式提供所有必需 ID，否则在调用前输出 `PREREQUISITE`，不发送
该工具请求。参数格式为 `--resource-id OPERATION.FIELD=VALUE`，多 ID 操作重复该参数：

```text
uv run python scripts/smoke_readonly_api.py --confirm-readonly \
  --operation get_markji_card \
  --resource-id get_markji_card.deck=ACCOUNT_DECK_ID \
  --resource-id get_markji_card.card=ACCOUNT_CARD_ID
```

支持的字段由代码固定；未知 operation/field、重复字段、空值和未选择操作的 ID 均拒绝。
值只允许 1–1000 个非空格可打印 ASCII 字符（33–126）；空格、DEL、Unicode 分隔/控制字符
及其他非 ASCII 都拒绝，错误消息不回显原值。ID 值不打印。`PREREQUISITE` 只表示缺少显式
ID，不是 PASS；一旦提供全部 ID，资源不存在、
401、429、超时、5xx、Schema 错误和未知错误均为 FAIL，绝不能按“这是 ID 查询”掩盖错误。

协议检查优先使用 MCP Inspector，但 Inspector 是交互工具，不能把空白启动或超时当成
审计通过。本次本地验收执行 `npx --yes @modelcontextprotocol/inspector@latest --help` 后
30 秒内没有得到可审计输出，因此未声称 Inspector 通过；替代证据是 MCP 官方 Python SDK
经进程内和 Streamable HTTP `/mcp` 两种传输执行工具发现与 Schema/标注测试。生产部署仍应
在可交互环境用 Inspector 复核。

## 8. 对话评测记录规则

语料在 `tests/evaluation/prompts.yaml`。确定性测试只证明语料引用的工具存在、参数符合实时
Schema、推测性反馈不指定写工具，以及不支持的墨墨写能力未注册；它不能证明模型一定会
选择该工具。动态多轮引用必须来自同一用例的更早 turn；测试沿实时注册工具的 output/input
Schema 解析路径与 `$ref`、`anyOf`、对象和数组，并校验 JSON 类型、可空性及 format。解析器
对每个可达来源分支做 universal 安全判断，目标 `anyOf` 按可接受并集处理；分支顺序不影响
结论。中间路径任一 null/缺失分支均拒绝，`$ref` sibling 按 Draft 2020-12 与引用目标合取，
循环或不存在的引用拒绝。解析器当前只允许标量字符串目标，未知关键 Schema 形态一律失败，
固定白名单中的 title/description/default/examples/deprecated/readOnly/writeOnly/$comment 等纯
annotation 可忽略；type、format、required、properties、anyOf、$ref 和可证明的字符串
minLength/maxLength 参与判断。enum、const、pattern、数值/数组/对象收窄断言、终端
additionalProperties 及未知关键字均 fail closed。`$defs`/`definitions`/`components` 只作为
引用资源容器，不当成实例断言，也不能复制工具 Schema 到语料来形成自证。解析对象路径时，
每个 conjunction 分支都必须参与：若属性不在该分支的 `properties`，则
`additionalProperties: false` 拒绝，Schema 形式与其他属性 Schema 合取并完整经过关键字
白名单，`true` 或缺省不增加约束；其结果不受 `$ref` sibling 或 `anyOf` 分支顺序影响。
Tunnel 连通后，
应逐项观察并只记录：用例 ID、工具名、参数字段名或参数类别、
结果类别、警告代码、是否要求用户确认。不要记录参数值中的个人标识符或任何结果正文。

至少执行：直接请求、间接表达、多轮标识符复用、stale/partial、明确反馈、推测性反馈、
撤销和墨墨写入拒绝。推测性反馈在用户确认前不得调用 `record_confusion_feedback`；墨墨添加
单词、创建助记或删除内容必须明确拒绝为未支持。

### 8.1 用一次性 PostgreSQL 复现 stale/partial

不得复制真实用户表或个人响应。启动项目的一次性 PostgreSQL，并只使用测试内合成数据：

```text
docker compose -f compose.test.yaml up -d postgres
uv run pytest tests/mcp/test_composite_tools.py::test_health_reports_stale_without_private_payloads -v
uv run pytest tests/mcp/test_composite_tools.py::test_weak_analysis_coverage_includes_unscored_words_before_filtering -v
```

第一个用例把 `today` 与 `records` 的最后成功调度固定在
`2030-10-01T12:00:00Z`，在 `2030-10-02T12:00:00Z` 求值；唯一预期为 `stale`，必须有
`today_stale`、`records_stale`，`data_through=2030-10-01`。第二个用例在同一截止日写入
两个可见合成词，但只给一个生成 `weakness-v1` 分数；唯一预期为 `partial`，必须有
`analysis_coverage_partial`，截止行为取已评分证据的最早 cutoff。用例自行建立/清理合成
状态；不要把一次性库连接改成生产 URL。运行后可用以下命令停止一次性数据库：

```text
docker compose -f compose.test.yaml down
```

## 9. 十项验收矩阵

以下是 2026-10-02 的本地可审计状态。“环境门禁”不是通过；完成生产验收时必须补充实际
输出时间和执行者，不能用替代检查改写为 PASS。

| # | 验收项 | 状态 | 本地证据 | 环境阻塞、替代检查与剩余风险 |
|---|---|---|---|---|
| 1 | 17 个只读操作均有 Client、原子工具和契约测试 | 本地通过 | `tests/contract/test_markji_client.py`、`test_memo_content_client.py`、`test_study_client.py`；`tests/mcp/test_atomic_tools.py` | 未跑真实账户 17 项冒烟；阻塞是本任务未获用户 Token 文件。替代为脱敏契约和真实 SDK 工具测试；仍有上游/账户数据差异风险。 |
| 2 | 5 个组合工具返回稳定结构和统一元信息 | 本地通过 | `tests/mcp/test_composite_tools.py` 覆盖 complete/partial/stale/unavailable、截止时间和来源 | 未经真实历史规模验证；剩余风险是生产数据分布。 |
| 3 | Worker 按计划采集且响应去重 | 本地通过 | `tests/integration/test_ingestion.py`、`test_worker_locking.py`、`test_worker_schema.py` | 替代使用一次性 PostgreSQL；NAS 长时间运行和调度漂移尚待观察。 |
| 4 | 返回薄弱词、原因、置信度和截止时间 | 本地通过 | `test_worker_persists_scores_visible_through_real_mcp` 通过实际 Worker→PG→评分→MCP；`test_scoring_failure_rolls_back_history_and_success_slot_then_retries` 验证评分失败回滚与重试；评分和组合工具测试 | 仅外部 HTTP 使用合成边界；初始权重是假设，仍需至少两周真实数据评估，不能静默改权重。 |
| 5 | 可记录和撤销混淆反馈 | 本地通过 | `tests/integration/test_feedback_service.py`、`tests/mcp/test_feedback_tools.py` | 未做真实 ChatGPT 确认交互；替代验证追加、幂等、方向和撤销链。 |
| 6 | MCP 未注册墨墨写操作 | 本地通过 | SDK 发现恰好 24 工具，其中 17 原子只读、5 组合只读、2 本地反馈；`tests/evaluation/test_safety_boundaries.py` | 未来新增工具仍必须更新固定审计；smoke 不会自动执行未来接口。 |
| 7 | Token 不进入镜像、日志、数据库和工具结果 | 本地通过 | 文件密钥配置、日志脱敏测试、MCP 错误边界、Docker secret 合成审计及 smoke 输出脱敏测试 | 未审计生产 NAS ACL 和真实日志；部署时必须完成第 1 节源文件 ACL 和容器只读检查并巡检日志。 |
| 8 | 数据缺失、认证失败、限流和规范漂移可观察 | 本地通过 | 数据健康 MCP 测试、传输/共享限流测试、`tests/unit/test_openapi_drift.py`、结构化日志测试 | 真实 401/429 和 Tunnel 状态未触发；替代为确定性错误注入，仍有平台告警集成风险。 |
| 9 | PostgreSQL 备份可恢复到空库并通过一致性检查 | 本地通过 | `tests/integration/test_backup_restore.py` 对 disposable PostgreSQL 执行 custom dump、恢复和撤销链检查 | NAS 的 PostgreSQL/客户端版本、存储权限未演练；上线前必须按第 4、5 节真实演练。 |
| 10 | ChatGPT 经 Secure MCP Tunnel 完成代表性对话 | 环境门禁，未通过 | 语料结构和安全边界测试；SDK/HTTP `/mcp` 协议替代检查 | 阻塞是没有运行中的 Secure MCP Tunnel 与 ChatGPT 工作区连接，也未获用户授权 Token。剩余风险包括 Tunnel 网络/认证、模型工具选择、参数生成和确认交互。 |

最终修复的补充本地证据：`test_collection_midnight.py` 覆盖锁等待、两次 HTTP、limiter/retry
跨上海午夜；`test_settings_intervals_reach_mcp_health_and_analysis` 覆盖 120/5 分钟配置；
`test_failed_response_archive.py` 和真实 MCP archive 回归覆盖缺必填留档、正常可选字段、
旧有效数据保留、BASELINE、异常/日志/MCP 边界及有损降级拒绝；
`test_drift_permissions.py` 在真实 Linux 容器检查 UID 1000 / GID 10 两次替换后可读。
这些测试不会读取真实 Token、连接用户数据库或建立 Tunnel。

## 10. 开发工作站的最终本地验证命令

从锁定依赖的干净检出执行：

```text
uv lock --check
uv run ruff check .
uv run mypy src
uv run pytest -v
docker compose config --quiet
docker build -t maimemo-mcp:test .
git status --short
```

上面本地验证在开发工作站源码目录运行，根 Compose 为通用/开发配置；NAS 使用 UGOS 项目
与交付模板，配置预检只执行 `docker compose config --quiet`。配置检查需要提供一个语法有效但不必可连接的
`MAIMEMO_DATABASE_URL`；构建和配置检查不得注入真实 Token。真实墨墨冒烟与 Secure MCP
Tunnel/ChatGPT 评测是独立环境门禁，只有观察到其实际输出后才能改为通过。
