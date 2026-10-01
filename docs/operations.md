# 运维手册

本文针对用户自有 NAS 上的私有部署。Compose 只运行同一只读应用镜像的 `mcp` 与
`worker` 两个命令；PostgreSQL 必须是 NAS 上已有的外部 PostgreSQL 15+。默认不发布
MCP 端口，网络仍保留出站能力以访问墨墨 API。

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
4. 设置 `MAIMEMO_DATABASE_URL`。允许 SQLAlchemy 的
   `postgresql+psycopg://user:password@host:5432/database` 形式。

不要把 `.env`、数据库口令、Token、fingerprint key 或真实个人响应放入构建目录、镜像
参数、Compose YAML、工单或 Git。

## 2. 构建、迁移和启动

```text
docker compose config
docker compose build --pull
python scripts/check_compose_secrets.py \
  --token-file ./secrets/maimemo_token \
  --fingerprint-key-file ./secrets/token_fingerprint_key \
  --image maimemo-mcp:local
docker compose run --rm --entrypoint /opt/venv/bin/python maimemo-mcp -m alembic upgrade head
docker compose up -d
docker compose ps
```

secret 审计通过真实 `docker compose run` 以 UID 10001 读取两份挂载，并逐一确认写入失败；
它只输出计数和 UID，不输出 secret 内容。`--synthetic --image maimemo-mcp:test` 可用于不接触
真实 secret 的 Docker 引擎自检，但不能替代生产源文件 owner/ACL 检查。

镜像使用固定 digest 的 Python 3.12 基础镜像、锁定的 `uv.lock` 和
`uv sync --frozen --no-dev`。运行用户为 UID/GID 10001，根文件系统只读，移除全部 Linux
capabilities。`restart: unless-stopped` 只负责进程意外退出后的重启；MCP 的就绪检查真实
查询 PostgreSQL。Worker 没有伪造的健康检查：其采集成败通过结构化日志、
`ingestion_run` 和 MCP 数据健康工具观察。

`maimemo-private` 是未发布端口的 Compose bridge。不能设为 Docker `internal: true`，否则
会阻断墨墨 API 的必要出站连接。默认只有同一 Docker 网络内的进程可访问
`http://maimemo-mcp:8000/mcp`。Compose 显式把唯一内部 DNS 名 `maimemo-mcp` 加入
`MAIMEMO_MCP_ALLOWED_HOSTS`；服务端只为该精确名字及其端口形式扩展 Host allowlist，其他
Host 仍被 DNS rebinding 防护拒绝。非 Compose 部署默认不增加任何内部 Host。

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
`docs/tunnel-setup.md` 核验。该 DNS 名由 Compose 显式 allowlist 放行；不要通过通配符扩大
Host 范围。

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
`alembic_version` 与 `schema_metadata` 都处于当前迁移头 `0003`。随后仍要用只读 SQL 核对
各表行数和抽样 hash。若 `pg_restore` 或一致性校验失败，脚本只会自动 `dropdb` 本进程刚
创建、且已通过 disposable 名称和二次确认校验的精确目标；清理失败时保留原始错误并附加
人工清理提示。成功的演练库不会自动删除，验收完成后由数据库管理员明确点名删除。脚本
不会删除或改写源库、用户库或任何已有目标。

## 6. 升级与回滚

1. 先执行新备份和空库恢复演练。
2. 构建带唯一版本标签的镜像并记录 digest；不要以 `latest` 作为回滚证据。
3. 执行迁移，再逐个重建 MCP 与 Worker。
4. 核对 `/health/ready`、迁移版本、最近采集时间和连续失败数。
5. 应用回滚只能切回已记录 digest；数据库迁移是否可降级必须单独验证。0002/0003 对不可
   表示的数据会拒绝降级，不得用强制清表绕过。

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
当前只允许标量字符串目标，未知 Schema 形态一律失败，不能复制工具 Schema 到语料来形成
自证。Tunnel 连通后，应逐项观察并只记录：用例 ID、工具名、参数字段名或参数类别、
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
| 4 | 返回薄弱词、原因、置信度和截止时间 | 本地通过 | `tests/unit/test_weakness_scoring.py`、`tests/integration/test_weakness_service.py`、组合工具测试 | 初始权重是假设；仍需至少两周真实数据评估，不能静默改权重。 |
| 5 | 可记录和撤销混淆反馈 | 本地通过 | `tests/integration/test_feedback_service.py`、`tests/mcp/test_feedback_tools.py` | 未做真实 ChatGPT 确认交互；替代验证追加、幂等、方向和撤销链。 |
| 6 | MCP 未注册墨墨写操作 | 本地通过 | SDK 发现恰好 24 工具，其中 17 原子只读、5 组合只读、2 本地反馈；`tests/evaluation/test_safety_boundaries.py` | 未来新增工具仍必须更新固定审计；smoke 不会自动执行未来接口。 |
| 7 | Token 不进入镜像、日志、数据库和工具结果 | 本地通过 | 文件密钥配置、日志脱敏测试、MCP 错误边界、Docker secret 合成审计及 smoke 输出脱敏测试 | 未审计生产 NAS ACL 和真实日志；部署时必须运行 `check_compose_secrets.py` 并巡检日志。 |
| 8 | 数据缺失、认证失败、限流和规范漂移可观察 | 本地通过 | 数据健康 MCP 测试、传输/共享限流测试、`tests/unit/test_openapi_drift.py`、结构化日志测试 | 真实 401/429 和 Tunnel 状态未触发；替代为确定性错误注入，仍有平台告警集成风险。 |
| 9 | PostgreSQL 备份可恢复到空库并通过一致性检查 | 本地通过 | `tests/integration/test_backup_restore.py` 对 disposable PostgreSQL 执行 custom dump、恢复和撤销链检查 | NAS 的 PostgreSQL/客户端版本、存储权限未演练；上线前必须按第 4、5 节真实演练。 |
| 10 | ChatGPT 经 Secure MCP Tunnel 完成代表性对话 | 环境门禁，未通过 | 语料结构和安全边界测试；SDK/HTTP `/mcp` 协议替代检查 | 阻塞是没有运行中的 Secure MCP Tunnel 与 ChatGPT 工作区连接，也未获用户授权 Token。剩余风险包括 Tunnel 网络/认证、模型工具选择、参数生成和确认交互。 |

## 10. 最终本地验证命令

从锁定依赖的干净检出执行：

```text
uv lock --check
uv run ruff check .
uv run mypy src
uv run pytest -v
docker compose config
docker build -t maimemo-mcp:test .
git status --short
```

`docker compose config` 需要提供一个语法有效但不必可连接的
`MAIMEMO_DATABASE_URL`；构建和配置检查不得注入真实 Token。真实墨墨冒烟与 Secure MCP
Tunnel/ChatGPT 评测是独立环境门禁，只有观察到其实际输出后才能改为通过。
