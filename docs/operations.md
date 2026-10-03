# 运维手册

本文针对用户自有 NAS 上的私有部署。Compose 项目名为 `maimemo`，长期运行 `server`、
`mcp`、`worker`，并提供一次性 `migrate` 服务。PostgreSQL 必须是 NAS 上已有的外部
PostgreSQL 15+。基础配置不发布宿主机端口，`maimemo-app` 网络保留出站能力以访问墨墨
API 和外部 PostgreSQL；反向代理网络只通过 `compose.proxy.example.yaml` 按需附加到 Server。

三个独立镜像使用同一个发布版本：

- `ghcr.io/huang-jiping/maimemo-server`：Server 与受限迁移命令；
- `ghcr.io/huang-jiping/maimemo-mcp`：MCP 协议服务；
- `ghcr.io/huang-jiping/maimemo-worker`：后台采集与分析。

## 1. 部署前准备

1. 为应用创建独立、最小权限的 PostgreSQL 用户和数据库，不复用管理员账号。
2. 在仓库外或被 `.gitignore` 排除的 `secrets/` 目录创建 `maimemo_token` 与随机生成的
   `token_fingerprint_key`。文件只放值本身，可有一个末尾换行。应用容器固定以
   UID/GID 10001 运行，因此 Linux NAS 上应让源文件归 10001 所有且仅 owner 可读：

   ```text
   sudo chown 10001:10001 secrets/maimemo_token secrets/token_fingerprint_key
   sudo chmod 0400 secrets/maimemo_token secrets/token_fingerprint_key
   ```

   如果 NAS 使用管理界面 ACL，必须给数字用户 10001（或映射到 GID 10001 的组）仅读取
   权限并拒绝写入，同时移除其他非管理员主体的访问权限。root/平台管理员仍可管理文件，
   但不能把源文件保留为只有 root:root 0400 可读，否则容器会收到 `PermissionError`。

   Docker 官方说明 Compose 的 file-source secret 是单文件 bind mount，`uid`、`gid`、
   `mode` 对这种来源会被静默忽略；本项目因此不在 Compose 中伪装设置这些字段，访问权限
   必须在 NAS 源文件 owner/group/ACL 上落实：
   <https://docs.docker.com/reference/compose-file/services/#secrets>。
3. 创建 `var/`，供运维任务生成的 OpenAPI 漂移状态文件使用；容器以只读方式挂载。
   Linux NAS 上确保目录允许 UID10001 遍历（例如 `mkdir -p var && chmod 0755 var`）。
   状态 writer 每次原子发布均设为 `0644`，文件仅含公开规范 hash、时间和 severity；
   不要对旧 inode 单次 chmod 后依赖它跨 replace 生效，也不要将密钥放进 `var/`。
4. 从 `.env.example` 创建不提交 Git 的 `.env`，固定三个已验收的镜像 tag 或 digest，并设置
   `MAIMEMO_DATABASE_URL`。允许 SQLAlchemy 的
   `postgresql+psycopg://user:password@host:5432/database` 形式。

不要把 `.env`、数据库口令、Token、fingerprint key 或真实个人响应放入构建目录、镜像
参数、Compose YAML、工单或 Git。

## 2. 构建、迁移和启动

```text
docker compose config
docker compose pull
python scripts/check_compose_secrets.py \
  --token-file ./secrets/maimemo_token \
  --fingerprint-key-file ./secrets/token_fingerprint_key \
  --server-image ghcr.io/huang-jiping/maimemo-server:0.2.0 \
  --mcp-image ghcr.io/huang-jiping/maimemo-mcp:0.2.0 \
  --worker-image ghcr.io/huang-jiping/maimemo-worker:0.2.0
docker compose run --rm migrate upgrade head
docker compose up -d server mcp worker
docker compose ps
```

secret 审计通过真实 `docker compose run` 证明 MCP 与 Worker 以 UID 10001 只读访问两份
挂载，同时证明 Server 与 migrate 完全看不到它们。输出只包含计数和 UID，不包含 secret。
`--synthetic` 可用于不接触真实 secret 的 Docker 引擎自检，但不能替代生产源文件 ACL 检查。

本地开发镜像使用以下命令构建，NAS 正式部署优先拉取 CI 发布并记录 digest 的镜像：

```text
docker build --target server -t maimemo-server:test -f docker/Dockerfile .
docker build --target mcp -t maimemo-mcp:test -f docker/Dockerfile .
docker build --target worker -t maimemo-worker:test -f docker/Dockerfile .
```

镜像使用固定 digest 的 Python 3.12 基础镜像、锁定的 `uv.lock` 和
`uv sync --frozen --no-dev --no-editable --package ...`。运行用户为 UID/GID 10001，根文件
系统只读，移除全部 Linux capabilities。`restart: unless-stopped` 只负责进程意外退出后的
重启；Server 就绪检查同时核对 PostgreSQL 与 Alembic 头，MCP 就绪检查查询 PostgreSQL。
Worker 没有伪造的健康检查：其采集成败通过结构化日志、
`ingestion_run` 和 MCP 数据健康工具观察。

`maimemo-app` 是未发布端口的 Compose bridge。不能设为 Docker `internal: true`，否则
会阻断墨墨 API 的必要出站连接。默认只有同一 Docker 网络内的进程可访问
`http://mcp:8000/mcp`。Compose 显式把内部 DNS 名 `mcp` 加入
`MAIMEMO_MCP_ALLOWED_HOSTS`；服务端只为该精确名字及其端口形式扩展 Host allowlist，其他
Host 仍被 DNS rebinding 防护拒绝。非 Compose 部署默认不增加任何内部 Host。

健康检查：

```text
docker compose exec server /opt/venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3).read().decode())"
docker compose exec mcp /opt/venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read().decode())"
docker compose logs --since 30m server mcp worker
```

日志只应包含 allowlist 字段。若发现凭据或个人正文，立即停止服务、轮换相关凭据并保留
不含敏感值的事件时间和镜像 digest 用于调查。

### 2.1 周期漂移检查与告警

在 NAS 的任务调度器或受监督任务中每 6 小时执行一次，工作目录设为仓库的绝对路径；
使用已锁定依赖的主机环境，命令为：

```text
uv run python scripts/check_openapi_drift.py --pinned openapi/maimemo-api.yaml --remote https://open.maimemo.com/api_bundle.yaml --state-file var/openapi-drift.json
```

此任务只获取公开 OpenAPI，不需要 Token。退出码 0 表示 none/informational，1 表示 high，
2 表示获取、解析或发布失败；调度器应把 1/2 交给现有 NAS 告警渠道，并记录安全 JSON 输出。
消费者读取 `/health/status` 的 `drift_status`：high 需要优先排查；informational 安排规范
审阅；unavailable/stale 检查任务运行记录、目录权限和发布路径。超过 26 小时未更新即 stale，
因此应对连续失败或未运行报警。新解析失败不会伪造成功状态，旧文件会自然过期。
合成 Linux 容器测试验证 root 发布后 UID10001 在连续两次原子替换后仍可读；实际 NAS ACL
和定时任务是否运行仍需部署者检查。

## 3. 原生 NAS Tunnel Client 的本机访问

默认 Compose 没有 `ports`。只有在 Tunnel Client 作为 NAS 主机上的受监督原生进程运行时，
创建一个不提交到 Git 的 `compose.tunnel-local.yaml`：

```yaml
services:
  mcp:
    ports:
      - "127.0.0.1:8000:8000"
```

然后使用两份 Compose 文件启动，并让 Tunnel Client 连接
`http://127.0.0.1:8000/mcp`。不得改成 `0.0.0.0:8000:8000`。若 Tunnel Client 在同一
Docker 网络内，可直接使用 `http://mcp:8000/mcp`，但镜像与版本必须先按
`docs/tunnel-setup.md` 核验。该 DNS 名由 Compose 显式 allowlist 放行；不要通过通配符扩大
Host 范围。

## 4. PostgreSQL 备份

生产环境优先在受控 NAS 主机安装与服务器主版本兼容的 `pg_dump`、`pg_restore`、
`createdb` 和 `psql`。脚本只用参数数组、`shell=False`、`check=True` 调用工具；数据库
URL 从环境变量读取，不接收 URL 命令行参数。
原生工具的 URL 参数清除 password，只通过子进程 `PGPASSWORD` 传递。URL query 采用
fail-closed 白名单：只允许单值 `sslmode`，值域为 `disable`、`allow`、`prefer`、`require`、
`verify-ca` 或 `verify-full`。其他键、大小写变体、重复值和自由文本均被安全拒绝；尤其不要
用 query 覆盖 host、port、user、password、dbname、service 或把任何秘密写到 query 参数。
query 键和值必须使用上述 ASCII 字面量，空 query、percent 编码键值、fragment 及多余分隔符
均不接受。密码中的 `@`、`:`、`/`、`%` 必须进行标准 percent 编码；host 只接受 DNS、IPv4
或带方括号的 IPv6。当前安全子集不支持通过 `host=` query 指定 Unix socket。

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
`alembic_version` 与 `schema_metadata` 都处于当前迁移头 `0004`。检查也包括隔离失败快照表。
随后仍要用只读 SQL 核对
各表行数和抽样 hash。若 `pg_restore` 或一致性校验失败，脚本只会自动 `dropdb` 本进程刚
创建、且已通过 disposable 名称和二次确认校验的精确目标；清理失败时保留原始错误并附加
人工清理提示。成功的演练库不会自动删除，验收完成后由数据库管理员明确点名删除。脚本
不会删除或改写源库、用户库或任何已有目标。

## 6. 升级与回滚

1. 先执行新备份和空库恢复演练，且不要删除或重建现有 PostgreSQL 数据库。
2. 记录 Server、MCP、Worker 三个当前镜像 digest；不要以 `latest` 作为回滚证据。
3. 拉取同一版本的三个新镜像，先停止旧 Worker，避免新旧调度器并发采集。
4. 执行 `docker compose run --rm migrate upgrade head`，再启动 `server`、`mcp`、`worker`。
5. 核对 Server/MCP `/health/ready`、迁移版本、最近采集时间和连续失败数。
6. 应用回滚只能把三个镜像成套切回已记录 digest；数据库迁移是否可降级必须单独验证。0002/0003 对不可
   表示的数据会拒绝降级；0004 的 `failed_api_snapshot` 非空时拒绝丢失失败证据。
   不得用强制清表绕过；失败快照与学习原始快照同样按敏感个人证据保护并纳入备份。

从旧单镜像部署迁移到 0.2.0 的精确步骤见 `docs/migration/maimemo-0.2.0.md`。

Tunnel 中断不影响 Worker。数据库不可用时，就绪检查失败且采集不得以内存结果冒充已
持久化成功。

## 7. 只读冒烟与协议检查

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
| 7 | Token 不进入镜像、日志、数据库和工具结果 | 本地通过 | 文件密钥配置、日志脱敏测试、MCP 错误边界、Docker secret 合成审计及 smoke 输出脱敏测试 | 未审计生产 NAS ACL 和真实日志；部署时必须运行 `check_compose_secrets.py` 并巡检日志。 |
| 8 | 数据缺失、认证失败、限流和规范漂移可观察 | 本地通过 | 数据健康 MCP 测试、传输/共享限流测试、`tests/unit/test_openapi_drift.py`、结构化日志测试 | 真实 401/429 和 Tunnel 状态未触发；替代为确定性错误注入，仍有平台告警集成风险。 |
| 9 | PostgreSQL 备份可恢复到空库并通过一致性检查 | 本地通过 | `tests/integration/test_backup_restore.py` 对 disposable PostgreSQL 执行 custom dump、恢复和撤销链检查 | NAS 的 PostgreSQL/客户端版本、存储权限未演练；上线前必须按第 4、5 节真实演练。 |
| 10 | ChatGPT 经 Secure MCP Tunnel 完成代表性对话 | 环境门禁，未通过 | 语料结构和安全边界测试；SDK/HTTP `/mcp` 协议替代检查 | 阻塞是没有运行中的 Secure MCP Tunnel 与 ChatGPT 工作区连接，也未获用户授权 Token。剩余风险包括 Tunnel 网络/认证、模型工具选择、参数生成和确认交互。 |

最终修复的补充本地证据：`test_collection_midnight.py` 覆盖锁等待、两次 HTTP、limiter/retry
跨上海午夜；`test_settings_intervals_reach_mcp_health_and_analysis` 覆盖 120/5 分钟配置；
`test_failed_response_archive.py` 和真实 MCP archive 回归覆盖缺必填留档、正常可选字段、
旧有效数据保留、BASELINE、异常/日志/MCP 边界及有损降级拒绝；
`test_drift_permissions.py` 在真实 Linux 容器检查 UID10001 两次替换后可读。
这些测试不会读取真实 Token、连接用户数据库或建立 Tunnel。

## 10. 最终本地验证命令

从锁定依赖的干净检出执行：

```text
uv lock --check
uv run ruff check .
uv run mypy packages/maimemo/src packages/maimemo-mcp/src packages/maimemo-server/src packages/maimemo-worker/src
uv run python -m pytest -v
uv build --package maimemo
uv build --package maimemo-mcp
uv build --package maimemo-server
uv build --package maimemo-worker
docker compose config
docker build --target server -t maimemo-server:0.2.0-test -f docker/Dockerfile .
docker build --target mcp -t maimemo-mcp:0.2.0-test -f docker/Dockerfile .
docker build --target worker -t maimemo-worker:0.2.0-test -f docker/Dockerfile .
git status --short
```

`docker compose config` 需要提供一个语法有效但不必可连接的
`MAIMEMO_DATABASE_URL`；构建和配置检查不得注入真实 Token。真实墨墨冒烟与 Secure MCP
Tunnel/ChatGPT 评测是独立环境门禁，只有观察到其实际输出后才能改为通过。
