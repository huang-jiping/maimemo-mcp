# 墨墨学习数据基础设施实施计划

> 历史文档：本文保留 0.1.x 单镜像实施过程与验收证据，不应用于当前部署。当前设计见
> `docs/superpowers/specs/2026-10-03-maimemo-project-naming-and-packaging-design.md`，迁移步骤见
> `docs/migration/maimemo-0.2.0.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个运行在 NAS 上的 Python MCP 服务，完整接入墨墨当前 17 个只读 API，持续保存个人学习历史，提供可解释薄弱词分析和可撤销的本地混淆反馈。

**Architecture:** 一个 Python 代码库构建一个镜像，以 MCP 进程和 Worker 进程两种入口运行；MCP 原子工具按需访问墨墨只读 API，Worker 负责正式历史采集，二者通过 PostgreSQL 共享限流状态和业务数据。Secure MCP Tunnel 只连接内部 Streamable HTTP `/mcp` 端点，Tunnel 中断不影响 Worker。

**Tech Stack:** Python 3.12、MCP Python SDK v2、Pydantic 2、SQLAlchemy 2.x Async、Psycopg 3、Alembic、HTTPX、PostgreSQL 15+、pytest、pytest-asyncio、Ruff、mypy、uv、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-10-02-maimemo-learning-data-foundation-design.md`

## Global Constraints

- 单用户；墨墨 Token 只从 Docker Secret 或只读文件加载，禁止写入数据库、日志、异常或 MCP 返回。
- 墨墨侧第一阶段严格只读；不得注册添加单词、提前复习、创建、更新或删除内容的工具。
- 当前 17 个语义只读操作必须全部有 Client 方法、原子 MCP 工具和契约测试。
- MCP 对话触发的实时调用不得写入正式学习历史；只有 Worker 生成历史快照。
- 本地仅允许追加混淆反馈和追加撤销事件，不物理删除原事件。
- 所有持久化时间使用 UTC `timestamptz`；学习日和用户展示使用 `Asia/Shanghai`。
- 响应模型允许未知可选字段；缺失必填字段必须失败，且不得覆盖最近有效数据。
- PostgreSQL 最低版本为 15；不引入 Redis、消息队列、Neo4j 或向量数据库。
- 使用 `uv.lock` 固定完整依赖树；开发、CI 和镜像构建均以锁文件为准。
- 每个任务严格按测试先行执行；任务末尾只提交该任务涉及的文件。
- 下文所有涉及网络或数据库 I/O 的方法均为 `async def`；纯计算、配置解析和时间换算保持同步函数。

## Review Focus

- 墨墨返回有效空集、同步未初始化或部分字段缺失时，系统必须区分空数据与不完整数据；Task 6、7、13 覆盖。
- `Asia/Shanghai` 午夜前后、Worker 重启和重复调度时，同一学习日不得被拆错或重复计入；Task 7、8 覆盖。
- MCP 与 Worker 并发请求时，10 秒、60 秒和 5 小时三个滑动窗口均不得超限；Task 3 覆盖。
- OpenAPI 新增可选字段应兼容，删除必填字段或修改路径必须产生失败或高优先级漂移；Task 4、14 覆盖。
- 混淆反馈的方向、证据类型、幂等和撤销链必须准确，系统候选不得变成用户确认事实；Task 10、13 覆盖。

---

## File Structure

```text
.
├─ .dockerignore
├─ .env.example
├─ .gitignore
├─ .python-version
├─ Dockerfile
├─ README.md
├─ compose.yaml
├─ compose.test.yaml
├─ pyproject.toml
├─ uv.lock
├─ alembic.ini
├─ openapi/
│  ├─ maimemo-api.yaml
│  └─ maimemo-api.sha256
├─ migrations/
│  ├─ env.py
│  └─ versions/
│     └─ 0001_initial_schema.py
├─ scripts/
│  ├─ check_openapi_drift.py
│  ├─ smoke_readonly_api.py
│  ├─ backup_postgres.py
│  └─ restore_postgres.py
├─ src/maimemo_mcp/
│  ├─ __init__.py
│  ├─ config.py
│  ├─ logging.py
│  ├─ time.py
│  ├─ maimemo_client/
│  │  ├─ __init__.py
│  │  ├─ errors.py
│  │  ├─ models.py
│  │  ├─ rate_limit.py
│  │  ├─ transport.py
│  │  ├─ markji.py
│  │  ├─ memo_content.py
│  │  └─ study.py
│  ├─ storage/
│  │  ├─ __init__.py
│  │  ├─ base.py
│  │  ├─ database.py
│  │  ├─ repositories.py
│  │  └─ models/
│  │     ├─ ingestion.py
│  │     ├─ learning.py
│  │     ├─ analysis.py
│  │     ├─ feedback.py
│  │     └─ rate_limit.py
│  ├─ ingestion/
│  │  ├─ hashing.py
│  │  ├─ normalizers.py
│  │  ├─ service.py
│  │  ├─ scheduler.py
│  │  └─ worker.py
│  ├─ analysis/
│  │  ├─ models.py
│  │  ├─ scoring.py
│  │  └─ service.py
│  ├─ feedback/
│  │  ├─ models.py
│  │  └─ service.py
│  └─ mcp_server/
│     ├─ app.py
│     ├─ dependencies.py
│     ├─ envelopes.py
│     ├─ health.py
│     └─ tools/
│        ├─ markji.py
│        ├─ memo_content.py
│        ├─ study.py
│        ├─ composite.py
│        └─ feedback.py
├─ tests/
│  ├─ conftest.py
│  ├─ fixtures/maimemo/
│  │  ├─ markji/
│  │  ├─ memo_content/
│  │  └─ study/
│  ├─ unit/
│  ├─ contract/
│  ├─ integration/
│  ├─ mcp/
│  └─ evaluation/
└─ docs/
   ├─ operations.md
   ├─ tunnel-setup.md
   └─ superpowers/
```

文件按职责拆分：`maimemo_client` 只处理外部 API，`ingestion` 只建立正式历史，`analysis` 只计算和解释评分，`feedback` 只维护事件语义，`mcp_server` 只做工具协议适配。

### Task 1: 项目骨架、配置与固定 OpenAPI 基线

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `openapi/maimemo-api.yaml`
- Create: `openapi/maimemo-api.sha256`
- Create: `src/maimemo_mcp/__init__.py`
- Create: `src/maimemo_mcp/config.py`
- Create: `src/maimemo_mcp/time.py`
- Create: `tests/unit/test_config.py`
- Create: `tests/unit/test_time.py`

**Interfaces:**
- Produces: `Settings.load(environ: Mapping[str, str] | None = None) -> Settings`
- Produces: `Settings.read_maimemo_token() -> SecretStr`
- Produces: `Settings.read_token_fingerprint_key() -> SecretStr`
- Produces: `learning_date(at: datetime, zone: ZoneInfo) -> date`
- Produces: Python 3.12 project with locked runtime and development dependencies.

- [ ] **Step 1: Write failing configuration and timezone tests**

```python
def test_settings_require_token_file_not_plain_token(): ...
def test_read_maimemo_token_strips_trailing_newline(): ...
def test_secret_is_absent_from_settings_repr(): ...
def test_learning_date_uses_asia_shanghai_at_utc_boundary(): ...
```

- [ ] **Step 2: Run the focused tests and confirm the package is missing**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_time.py -v`

Expected: FAIL because `maimemo_mcp.config` and `maimemo_mcp.time` do not exist.

- [ ] **Step 3: Create the Python project and configuration interfaces**

Use Python `3.12`, `src` layout, MCP SDK `>=2,<3`, Pydantic `>=2,<3`, SQLAlchemy `>=2,<3`, Psycopg 3, Alembic, HTTPX, pytest, pytest-asyncio, Ruff and mypy. Define configuration fields for database URL, Token file, Token fingerprint-key file, timezone, MCP bind address, MCP port, collection intervals and log level. Do not add a plain-text Token environment option.

- [ ] **Step 4: Pin the current official OpenAPI document**

Download `https://open.maimemo.com/api_bundle.yaml` into `openapi/maimemo-api.yaml`, calculate SHA-256 into `openapi/maimemo-api.sha256`, and verify the snapshot contains 38 operations and the 17 approved read-only operations. Do not embed credentials.

- [ ] **Step 5: Lock dependencies and run static checks**

Run: `uv lock`

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_time.py -v`

Run: `uv run ruff check src tests`

Run: `uv run mypy src`

Expected: lockfile created; all tests and checks PASS.

- [ ] **Step 6: Commit**

```text
git add .python-version .gitignore .env.example README.md pyproject.toml uv.lock openapi src/maimemo_mcp/__init__.py src/maimemo_mcp/config.py src/maimemo_mcp/time.py tests/unit/test_config.py tests/unit/test_time.py
git commit -m "chore: bootstrap maimemo mcp project"
```

### Task 2: PostgreSQL 模型、会话和初始迁移

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_initial_schema.py`
- Create: `src/maimemo_mcp/storage/__init__.py`
- Create: `src/maimemo_mcp/storage/base.py`
- Create: `src/maimemo_mcp/storage/database.py`
- Create: `src/maimemo_mcp/storage/models/ingestion.py`
- Create: `src/maimemo_mcp/storage/models/learning.py`
- Create: `src/maimemo_mcp/storage/models/analysis.py`
- Create: `src/maimemo_mcp/storage/models/feedback.py`
- Create: `src/maimemo_mcp/storage/models/rate_limit.py`
- Create: `tests/integration/test_migrations.py`
- Create: `tests/integration/test_schema_constraints.py`
- Create: `compose.test.yaml`

**Interfaces:**
- Produces: `create_async_engine_from_settings(settings: Settings) -> AsyncEngine`
- Produces: `create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]`
- Produces: ORM models for every table named in spec section 11 plus `schema_metadata`.
- Requires: PostgreSQL 15+ and `postgresql+psycopg` async dialect.

- [ ] **Step 1: Write migration and constraint tests**

```python
async def test_upgrade_creates_expected_tables(postgres_url): ...
async def test_snapshot_content_hash_is_unique_per_endpoint_and_request(postgres_url): ...
async def test_feedback_retraction_references_existing_event(postgres_url): ...
async def test_timestamps_are_timezone_aware(postgres_url): ...
```

- [ ] **Step 2: Start the disposable test database and verify failure**

Run: `docker compose -f compose.test.yaml up -d postgres`

Run: `uv run pytest tests/integration/test_migrations.py tests/integration/test_schema_constraints.py -v`

Expected: FAIL because migrations and models do not exist.

- [ ] **Step 3: Implement focused ORM models and database lifecycle**

Use PostgreSQL `JSONB`, UUID primary keys, explicit indexes and `timestamptz`. Keep ingestion, learning, analysis, feedback and rate-limit models in separate files. Store response status and enum-like values as constrained strings so new upstream values can be retained without a database enum migration.

- [ ] **Step 4: Add the initial Alembic migration**

The migration must create `vocabulary`, `ingestion_run`, `api_snapshot`, `api_rate_limit_window`, `daily_progress`, `daily_word_observation`, `study_record_snapshot`, `weakness_score`, `learning_feedback_event` and `schema_metadata`, including uniqueness, foreign-key and lookup indexes used by later tasks.

- [ ] **Step 5: Verify upgrade, downgrade and clean re-upgrade**

Run: `uv run alembic upgrade head`

Run: `uv run pytest tests/integration/test_migrations.py tests/integration/test_schema_constraints.py -v`

Run: `uv run alembic downgrade base`

Run: `uv run alembic upgrade head`

Expected: all operations succeed and tests PASS.

- [ ] **Step 6: Commit**

```text
git add alembic.ini migrations compose.test.yaml src/maimemo_mcp/storage tests/integration/test_migrations.py tests/integration/test_schema_constraints.py
git commit -m "feat: add postgres persistence schema"
```

### Task 3: 跨进程滑动窗口限流和 HTTP 传输层

**Files:**
- Create: `src/maimemo_mcp/maimemo_client/__init__.py`
- Create: `src/maimemo_mcp/maimemo_client/errors.py`
- Create: `src/maimemo_mcp/maimemo_client/rate_limit.py`
- Create: `src/maimemo_mcp/maimemo_client/transport.py`
- Create: `tests/unit/test_transport.py`
- Create: `tests/integration/test_rate_limit.py`

**Interfaces:**
- Produces: `RateLimitDecision(allowed: bool, retry_at: datetime | None)`
- Produces: `SharedRateLimiter.reserve(token_fingerprint: str, now: datetime) -> RateLimitDecision`
- Produces: `SharedRateLimiter.acquire(token_fingerprint: str) -> None`
- Produces: `MaimemoTransport.request(method: str, path: str, *, params: Mapping[str, Any] | None, json: Mapping[str, Any] | None, response_type: type[T]) -> T`
- Produces: typed exceptions `AuthenticationError`, `RateLimitError`, `UpstreamUnavailableError`, `UpstreamSchemaError`, `InvalidRequestError`.

- [ ] **Step 1: Write deterministic sliding-window tests**

```python
async def test_twenty_first_request_inside_ten_seconds_is_delayed(): ...
async def test_forty_first_request_inside_sixty_seconds_is_delayed(): ...
async def test_two_thousand_first_request_inside_five_hours_is_delayed(): ...
async def test_two_instances_share_the_same_limit(postgres_url): ...
```

Each request reservation writes one expiring reservation for each official window while holding a PostgreSQL advisory lock derived from the Token fingerprint. Tests must include requests immediately before and after window boundaries.

- [ ] **Step 2: Write transport retry and redaction tests**

```python
async def test_401_is_not_retried(): ...
async def test_429_honors_retry_after(): ...
async def test_timeout_and_5xx_have_bounded_retries(): ...
async def test_error_text_never_contains_token(): ...
async def test_unknown_response_fields_are_preserved(): ...
async def test_missing_required_field_raises_schema_error(): ...
```

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `uv run pytest tests/unit/test_transport.py tests/integration/test_rate_limit.py -v`

Expected: FAIL because the limiter and transport do not exist.

- [ ] **Step 4: Implement limiter and transport**

Calculate the Token fingerprint with HMAC-SHA-256 using a separate local fingerprint key. Never use a plain unsalted hash of the Token. Inject clocks and sleep functions into the limiter and retry policy so tests do not sleep.

- [ ] **Step 5: Verify concurrency and retries**

Run: `uv run pytest tests/unit/test_transport.py tests/integration/test_rate_limit.py -v`

Expected: PASS, including two concurrent limiter instances.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/maimemo_client tests/unit/test_transport.py tests/integration/test_rate_limit.py
git commit -m "feat: add shared api transport safeguards"
```

### Task 4: 通用响应模型与 Markji 七个只读 API

**Files:**
- Create: `src/maimemo_mcp/maimemo_client/models.py`
- Create: `src/maimemo_mcp/maimemo_client/markji.py`
- Create: `tests/fixtures/maimemo/markji/*.json`
- Create: `tests/contract/test_markji_client.py`

**Interfaces:**
- Produces: Pydantic response models with `extra="allow"` and strict required fields.
- Produces: `MarkjiClient.list_folders(request: ListFoldersRequest) -> ListFoldersResponse`
- Produces: `MarkjiClient.list_decks(request: ListDecksRequest) -> ListDecksResponse`
- Produces: `MarkjiClient.get_deck(deck: str) -> GetDeckResponse`
- Produces: `MarkjiClient.list_chapters(deck: str) -> ListChaptersResponse`
- Produces: `MarkjiClient.get_chapter(deck: str, chapter: str) -> GetChapterResponse`
- Produces: `MarkjiClient.get_card(deck: str, card: str) -> GetCardResponse`
- Produces: `MarkjiClient.query_files(request: QueryFilesRequest) -> QueryFilesResponse`

- [ ] **Step 1: Create redacted fixtures from the pinned OpenAPI examples**

Fixtures must include a valid response, a valid empty response, an unknown optional field and a missing required field for each response shape. Do not use live personal data.

- [ ] **Step 2: Write seven operation contract tests**

Assert exact HTTP method, path, query/body mapping, response model and schema failure behavior for every Markji read operation.

- [ ] **Step 3: Run tests and verify they fail**

Run: `uv run pytest tests/contract/test_markji_client.py -v`

Expected: FAIL because `MarkjiClient` and response models are absent.

- [ ] **Step 4: Implement the minimal Markji client**

Input fields and limits must match the pinned OpenAPI snapshot. Every method delegates authentication, limiting and retries to `MaimemoTransport`; no endpoint method performs its own retry.

- [ ] **Step 5: Verify contracts and static typing**

Run: `uv run pytest tests/contract/test_markji_client.py -v`

Run: `uv run mypy src/maimemo_mcp/maimemo_client`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/maimemo_client/models.py src/maimemo_mcp/maimemo_client/markji.py tests/fixtures/maimemo/markji tests/contract/test_markji_client.py
git commit -m "feat: add markji read client"
```

### Task 5: 内容、云词本和单词七个只读 API

**Files:**
- Create: `src/maimemo_mcp/maimemo_client/memo_content.py`
- Create: `tests/fixtures/maimemo/memo_content/*.json`
- Create: `tests/contract/test_memo_content_client.py`
- Modify: `src/maimemo_mcp/maimemo_client/models.py`

**Interfaces:**
- Produces: `MemoContentClient.get_interpretations(voc_id: str) -> InterpretationsResponse`
- Produces: `MemoContentClient.get_notes(voc_id: str) -> NotesResponse`
- Produces: `MemoContentClient.list_notepads(request: ListNotepadsRequest) -> ListNotepadsResponse`
- Produces: `MemoContentClient.get_notepad(notepad_id: str) -> GetNotepadResponse`
- Produces: `MemoContentClient.get_phrases(voc_id: str) -> PhrasesResponse`
- Produces: `MemoContentClient.get_vocabulary(voc_id: str) -> VocabularyResponse`
- Produces: `MemoContentClient.query_vocabulary(request: QueryVocabularyRequest) -> QueryVocabularyResponse`

- [ ] **Step 1: Add redacted valid, empty, unknown-field and invalid fixtures**

Use only the pinned OpenAPI examples and synthetic IDs.

- [ ] **Step 2: Write seven operation contract tests**

Tests assert method, path, parameter placement, declared maximum batch size, response parsing and required-field failures.

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/contract/test_memo_content_client.py -v`

Expected: FAIL because the content client is absent.

- [ ] **Step 4: Implement the content client and models**

Keep public API models separate from database ORM models. Preserve original spelling and unknown response fields.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/contract/test_memo_content_client.py -v`

Run: `uv run mypy src/maimemo_mcp/maimemo_client`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/maimemo_client tests/fixtures/maimemo/memo_content tests/contract/test_memo_content_client.py
git commit -m "feat: add memo content read client"
```

### Task 6: 三个学习数据只读 API

**Files:**
- Create: `src/maimemo_mcp/maimemo_client/study.py`
- Create: `tests/fixtures/maimemo/study/*.json`
- Create: `tests/contract/test_study_client.py`
- Modify: `src/maimemo_mcp/maimemo_client/models.py`

**Interfaces:**
- Produces: `StudyClient.get_progress() -> StudyProgressResponse`
- Produces: `StudyClient.get_today_items(request: TodayItemsRequest) -> TodayItemsResponse`
- Produces: `StudyClient.query_records(request: StudyRecordsRequest) -> StudyRecordsResponse`
- Produces: enums or string literals for `FAMILIAR`, `VAGUE`, `FORGET`, `WELL_FAMILIAR`, `CANCEL_WELL_FAMILIAR`, while preserving unknown upstream values separately.

- [ ] **Step 1: Create complete, empty, uninitialized and schema-drift fixtures**

Include a response without `first_response`, which is optional for unfinished items, and a response missing a truly required identifier, which must fail.

- [ ] **Step 2: Write three operation contract tests plus data-quality cases**

```python
async def test_empty_today_items_is_valid_but_not_proof_of_initialization(): ...
async def test_unknown_study_response_is_preserved_and_warned(): ...
async def test_query_records_enforces_one_thousand_item_limit(): ...
```

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/contract/test_study_client.py -v`

Expected: FAIL because the study client is absent.

- [ ] **Step 4: Implement the study client and models**

Do not infer completeness in the HTTP Client. Return typed data plus parsing warnings; the ingestion and MCP service layers decide whether a response is complete.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/contract/test_study_client.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/maimemo_client tests/fixtures/maimemo/study tests/contract/test_study_client.py
git commit -m "feat: add study data read client"
```

### Task 7: 正式历史采集、规范化和幂等快照

**Files:**
- Create: `src/maimemo_mcp/storage/repositories.py`
- Create: `src/maimemo_mcp/ingestion/hashing.py`
- Create: `src/maimemo_mcp/ingestion/normalizers.py`
- Create: `src/maimemo_mcp/ingestion/service.py`
- Create: `tests/unit/test_normalizers.py`
- Create: `tests/integration/test_ingestion.py`

**Interfaces:**
- Produces: `stable_payload_hash(endpoint: str, request: Mapping[str, Any], response: Mapping[str, Any]) -> str`
- Produces: `normalize_daily_progress(response: StudyProgressResponse, observed_at: datetime) -> DailyProgressInput`
- Produces: `normalize_today_items(response: TodayItemsResponse, observed_at: datetime) -> list[DailyWordObservationInput]`
- Produces: `normalize_study_records(response: StudyRecordsResponse, observed_at: datetime) -> list[StudyRecordSnapshotInput]`
- Produces: `StudyIngestionService.collect_today(observed_at: datetime) -> IngestionResult`
- Produces: `StudyIngestionService.collect_records(observed_at: datetime) -> IngestionResult`
- Produces: `StudyRecordWindowPlanner.plan(range_start: datetime, range_end: datetime) -> list[StudyRecordWindow]`

- [ ] **Step 1: Write normalization tests**

Cover UTC-to-Shanghai learning dates, missing optional responses, unknown upstream values, stable hashing independent of JSON key order and preservation of original payloads.

- [ ] **Step 2: Write transactional ingestion tests**

```python
async def test_repeated_identical_response_creates_one_snapshot(): ...
async def test_changed_response_creates_new_observation(): ...
async def test_normalization_failure_rolls_back_snapshot_and_history(): ...
async def test_failed_run_is_recorded_in_separate_transaction(): ...
async def test_empty_uninitialized_day_does_not_overwrite_valid_day(): ...
async def test_record_sync_recursively_splits_ranges_over_one_thousand(): ...
async def test_single_day_over_one_thousand_is_marked_partial(): ...
async def test_total_count_mismatch_is_marked_partial(): ...
```

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/unit/test_normalizers.py tests/integration/test_ingestion.py -v`

Expected: FAIL because ingestion services are absent.

- [ ] **Step 4: Implement repositories and ingestion transaction boundary**

Only this service writes `api_snapshot`, `daily_progress`, `daily_word_observation` and `study_record_snapshot`. Mark the first imported state `BASELINE`. A failed main transaction is followed by a separate short transaction that records the failure without raw secrets.

For study records, call `as_count=true` first, then recursively split configured `next_study_date` ranges until each count is at most 1000 before fetching rows. Compare total count, retrieved unique IDs and window counts. If one indivisible learning-day window still reaches 1000, a record has no queryable date, or counts do not reconcile, persist the available rows but mark the ingestion `partial` with an explicit truncation warning; never describe it as a complete account export.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/unit/test_normalizers.py tests/integration/test_ingestion.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/storage/repositories.py src/maimemo_mcp/ingestion tests/unit/test_normalizers.py tests/integration/test_ingestion.py
git commit -m "feat: persist idempotent study history"
```

### Task 8: Worker 调度、单实例锁和数据健康状态

**Files:**
- Create: `src/maimemo_mcp/ingestion/scheduler.py`
- Create: `src/maimemo_mcp/ingestion/worker.py`
- Create: `tests/unit/test_scheduler.py`
- Create: `tests/integration/test_worker_locking.py`
- Modify: `src/maimemo_mcp/storage/repositories.py`

**Interfaces:**
- Produces: `Schedule.next_runs(now: datetime) -> list[ScheduledJob]`
- Produces: `Worker.run_job(job: ScheduledJob, now: datetime) -> IngestionResult`
- Produces: `Worker.run_forever() -> None`
- Produces: repository queries `get_data_health(now: datetime) -> DataHealth` and `record_daily_summary(day: date) -> None`.

- [ ] **Step 1: Write scheduler boundary tests**

Cover 30-minute today jobs, 2-hour record jobs, process-start catch-up, end-of-day summary, Shanghai midnight and a restart immediately after a scheduled run.

- [ ] **Step 2: Write two-worker locking tests**

Assert that two Worker instances racing for the same job produce exactly one formal ingestion run and release the advisory lock after success or failure.

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/unit/test_scheduler.py tests/integration/test_worker_locking.py -v`

Expected: FAIL because scheduling is absent.

- [ ] **Step 4: Implement the scheduler and Worker entry point**

Use an async scheduler with injected clock. The default intervals are fixed by the spec but configurable. Do not use OS-local time implicitly.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/unit/test_scheduler.py tests/integration/test_worker_locking.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/ingestion src/maimemo_mcp/storage/repositories.py tests/unit/test_scheduler.py tests/integration/test_worker_locking.py
git commit -m "feat: schedule reliable learning collection"
```

### Task 9: 可解释薄弱词评分

**Files:**
- Create: `src/maimemo_mcp/analysis/models.py`
- Create: `src/maimemo_mcp/analysis/scoring.py`
- Create: `src/maimemo_mcp/analysis/service.py`
- Create: `tests/unit/test_weakness_scoring.py`
- Create: `tests/integration/test_weakness_service.py`
- Modify: `src/maimemo_mcp/storage/repositories.py`

**Interfaces:**
- Produces: `WeaknessEvidence`, `WeaknessFactor`, `WeaknessResult`.
- Produces: `calculate_weakness(evidence: WeaknessEvidence, as_of: datetime, version: str = "weakness-v1") -> WeaknessResult`
- Produces: `WeaknessService.recalculate(as_of: datetime) -> int`
- Produces: `WeaknessService.list_weak_words(query: WeakWordQuery) -> list[WeaknessResult]`

- [ ] **Step 1: Write exact factor tests**

Tests pin the approved weights: recent response 35%, repeated error rate 25%, sticking 15%, interval pressure 15%, unfinished difficulty 10%. Include `FORGET > VAGUE > FAMILIAR`, time decay and score clamping to 0–100.

- [ ] **Step 2: Write confidence and correction tests**

```python
def test_missing_factors_lower_confidence_instead_of_scoring_zero(): ...
def test_new_words_are_marked_and_not_automatically_high_risk(): ...
def test_old_word_study_count_is_normalized_by_age_and_error_rate(): ...
def test_one_forget_does_not_create_sticking_classification(): ...
def test_consecutive_familiar_responses_reduce_score_gradually(): ...
```

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/unit/test_weakness_scoring.py tests/integration/test_weakness_service.py -v`

Expected: FAIL because the analysis module is absent.

- [ ] **Step 4: Implement pure scoring and persistence service**

Keep the mathematical function pure and independent of SQL. Persist factor values, reason codes, evidence range, confidence and algorithm version; generate user-facing reason text outside the pure calculator.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/unit/test_weakness_scoring.py tests/integration/test_weakness_service.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/analysis src/maimemo_mcp/storage/repositories.py tests/unit/test_weakness_scoring.py tests/integration/test_weakness_service.py
git commit -m "feat: score explainable weak words"
```

### Task 10: 追加式混淆反馈和撤销

**Files:**
- Create: `src/maimemo_mcp/feedback/models.py`
- Create: `src/maimemo_mcp/feedback/service.py`
- Create: `tests/unit/test_feedback_models.py`
- Create: `tests/integration/test_feedback_service.py`
- Modify: `src/maimemo_mcp/storage/repositories.py`

**Interfaces:**
- Produces: `FeedbackRelationType`, `FeedbackDirection`, `EvidenceType`.
- Produces: `FeedbackService.record(command: RecordFeedbackCommand) -> FeedbackEventView`
- Produces: `FeedbackService.retract(command: RetractFeedbackCommand) -> FeedbackEventView`
- Produces: `FeedbackService.list_active(query: FeedbackQuery) -> list[FeedbackEventView]`

- [ ] **Step 1: Write validation tests**

Reject identical endpoints, blank spellings, invalid evidence values and a `USER_CONFIRMED` event without explicit confirmation. Preserve directional `A_TO_B`; do not silently make it bidirectional.

- [ ] **Step 2: Write persistence semantics tests**

```python
async def test_same_idempotency_key_returns_existing_event(): ...
async def test_retract_appends_event_and_keeps_original(): ...
async def test_cannot_retract_event_twice(): ...
async def test_unresolved_spelling_is_saved_without_fake_vocabulary_id(): ...
async def test_quiz_observed_cannot_be_returned_as_user_confirmed(): ...
```

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/unit/test_feedback_models.py tests/integration/test_feedback_service.py -v`

Expected: FAIL because the feedback service is absent.

- [ ] **Step 4: Implement append-only feedback semantics**

Normalize matching spellings without losing original text. Resolution against墨墨 vocabulary is optional and failure leaves `UNRESOLVED`; it must not block recording explicit feedback.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/unit/test_feedback_models.py tests/integration/test_feedback_service.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/feedback src/maimemo_mcp/storage/repositories.py tests/unit/test_feedback_models.py tests/integration/test_feedback_service.py
git commit -m "feat: record reversible confusion feedback"
```

### Task 11: MCP v2 服务骨架、依赖生命周期和统一返回包

**Files:**
- Create: `src/maimemo_mcp/mcp_server/app.py`
- Create: `src/maimemo_mcp/mcp_server/dependencies.py`
- Create: `src/maimemo_mcp/mcp_server/envelopes.py`
- Create: `src/maimemo_mcp/mcp_server/health.py`
- Create: `tests/mcp/test_server_foundation.py`

**Interfaces:**
- Produces: `create_mcp_app(settings: Settings) -> MCPServer`
- Produces: `ToolEnvelope[T](data: T, meta: ToolMeta)`
- Produces: `ToolMeta(source: list[str], fetched_at: datetime, data_through: date | None, completeness: Completeness, warnings: list[str])`
- Produces: unauthenticated non-sensitive `/health/live` and `/health/ready` routes.

- [ ] **Step 1: Write in-process MCP tests**

```python
async def test_server_discovery_uses_mcp_v2_in_process(): ...
async def test_tool_envelope_serializes_utc_and_completeness(): ...
async def test_health_routes_expose_no_token_or_learning_data(): ...
async def test_lifespan_opens_and_closes_database_and_http_clients(): ...
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/mcp/test_server_foundation.py -v`

Expected: FAIL because the MCP server is absent.

- [ ] **Step 3: Implement the MCP SDK v2 Streamable HTTP app**

Serve `/mcp`, keep tools stateless, install one lifespan that owns database and HTTP clients, and set concise server instructions explaining that墨墨 tools are read-only while feedback tools write only local append-only events. Health routes must contain only process and dependency status.

- [ ] **Step 4: Verify in-process and HTTP transports**

Run: `uv run pytest tests/mcp/test_server_foundation.py -v`

Expected: PASS for in-process client and a local Streamable HTTP client.

- [ ] **Step 5: Commit**

```text
git add src/maimemo_mcp/mcp_server tests/mcp/test_server_foundation.py
git commit -m "feat: add mcp server foundation"
```

### Task 12: 十七个原子只读 MCP 工具

**Files:**
- Create: `src/maimemo_mcp/mcp_server/tools/markji.py`
- Create: `src/maimemo_mcp/mcp_server/tools/memo_content.py`
- Create: `src/maimemo_mcp/mcp_server/tools/study.py`
- Create: `tests/mcp/test_atomic_tools.py`
- Modify: `src/maimemo_mcp/mcp_server/app.py`

**Interfaces:**
- Produces exactly: `list_markji_folders`, `list_markji_decks`, `get_markji_deck`, `list_markji_chapters`, `get_markji_chapter`, `get_markji_card`, `query_markji_files`, `get_interpretations`, `get_notes`, `list_notepads`, `get_notepad`, `get_phrases`, `get_study_progress`, `get_today_items`, `query_study_records`, `get_vocabulary`, `query_vocabulary`.
- Consumes: `MarkjiClient`, `MemoContentClient`, `StudyClient`, `ToolEnvelope`.
- Each tool returns the live upstream result and `meta.source == ["maimemo_api"]`.

- [ ] **Step 1: Write tool inventory and safety annotation tests**

Assert exact tool-name set, `readOnlyHint=true`, `destructiveHint=false`, explicit input schemas and no registered墨墨 write tools.

- [ ] **Step 2: Write one mapping test per tool**

Each test calls through the in-process MCP client, verifies the corresponding Client method and arguments, and checks that the call does not create `api_snapshot`, learning history or feedback rows.

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/mcp/test_atomic_tools.py -v`

Expected: FAIL because tools are not registered.

- [ ] **Step 4: Implement thin tool adapters**

Do not duplicate validation already expressed by request models. Tool descriptions must state when to use the tool, distinguish IDs from spellings and mention upstream limits.

- [ ] **Step 5: Verify all 17 tools**

Run: `uv run pytest tests/mcp/test_atomic_tools.py -v`

Expected: PASS with exactly 17 approved atomic tools and zero墨墨 write tools.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/mcp_server/tools src/maimemo_mcp/mcp_server/app.py tests/mcp/test_atomic_tools.py
git commit -m "feat: expose all maimemo read tools"
```

### Task 13: 五个组合工具和两个反馈工具

**Files:**
- Create: `src/maimemo_mcp/mcp_server/tools/composite.py`
- Create: `src/maimemo_mcp/mcp_server/tools/feedback.py`
- Create: `tests/mcp/test_composite_tools.py`
- Create: `tests/mcp/test_feedback_tools.py`
- Modify: `src/maimemo_mcp/mcp_server/app.py`

**Interfaces:**
- Produces: `get_daily_study_dashboard`, `get_word_learning_profile`, `get_weak_words`, `get_due_review_overview`, `get_learning_data_health`.
- Produces: `record_confusion_feedback`, `retract_feedback`.
- Consumes: storage repositories, `WeaknessService`, `FeedbackService` and read-only clients.

- [ ] **Step 1: Write composite provenance and completeness tests**

```python
async def test_dashboard_marks_partial_when_today_was_not_initialized(): ...
async def test_word_profile_distinguishes_live_and_local_sources(): ...
async def test_weak_words_returns_reasons_confidence_and_data_through(): ...
async def test_health_reports_stale_without_exposing_private_payloads(): ...
async def test_empty_due_review_is_complete_empty_not_unavailable(): ...
```

- [ ] **Step 2: Write feedback-tool authorization semantics tests**

Verify explicit `USER_CONFIRMED`, `QUIZ_OBSERVED`, directional feedback, idempotency, unresolved words and retraction. A request phrased as system inference must be rejected unless `confirmed_by_user=true`.

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/mcp/test_composite_tools.py tests/mcp/test_feedback_tools.py -v`

Expected: FAIL because composite and feedback tools are absent.

- [ ] **Step 4: Implement service-level composition and tool registration**

Composite functions call internal services directly, never invoke other MCP tools. Use local history by default and make every live upstream call visible in `meta.source` and `fetched_at`.

- [ ] **Step 5: Verify all 24 tools and safety boundaries**

Run: `uv run pytest tests/mcp -v`

Expected: 17 atomic + 5 composite + 2 feedback tools; all tests PASS.

- [ ] **Step 6: Commit**

```text
git add src/maimemo_mcp/mcp_server/tools src/maimemo_mcp/mcp_server/app.py tests/mcp
git commit -m "feat: add learning and feedback workflows"
```

### Task 14: OpenAPI 漂移、结构化日志和运行指标

**Files:**
- Create: `scripts/check_openapi_drift.py`
- Create: `src/maimemo_mcp/logging.py`
- Create: `tests/unit/test_openapi_drift.py`
- Create: `tests/unit/test_logging_redaction.py`
- Modify: `src/maimemo_mcp/mcp_server/health.py`
- Modify: `src/maimemo_mcp/ingestion/worker.py`

**Interfaces:**
- Produces: `compare_openapi(pinned: bytes, current: bytes) -> DriftReport`
- Produces: drift severities `none`, `informational`, `high`.
- Produces: `configure_logging(settings: Settings) -> None`
- Produces: health fields for last successful collection, consecutive failures, schema hash, drift status and database migration revision.

- [ ] **Step 1: Write drift classification tests**

New optional field and new read-only operation are informational; removed operation, changed path or new required input/response field are high severity. Reordering YAML must not count as drift.

- [ ] **Step 2: Write redaction and health tests**

Inject Token-like values, bearer headers, personal response payloads and exceptions. Assert logs retain endpoint, latency, status, trace ID and error class but contain none of the sensitive values.

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/unit/test_openapi_drift.py tests/unit/test_logging_redaction.py -v`

Expected: FAIL because drift and logging modules are absent.

- [ ] **Step 4: Implement semantic drift comparison and redacted logging**

Parse operation IDs, methods, paths and required schema properties instead of diffing raw YAML text. Return nonzero CLI exit code only for high-severity drift or an unreadable spec.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/unit/test_openapi_drift.py tests/unit/test_logging_redaction.py -v`

Run: `uv run python scripts/check_openapi_drift.py --pinned openapi/maimemo-api.yaml --remote https://open.maimemo.com/api_bundle.yaml`

Expected: tests PASS; live check prints a structured drift report without credentials.

- [ ] **Step 6: Commit**

```text
git add scripts/check_openapi_drift.py src/maimemo_mcp/logging.py src/maimemo_mcp/mcp_server/health.py src/maimemo_mcp/ingestion/worker.py tests/unit/test_openapi_drift.py tests/unit/test_logging_redaction.py
git commit -m "feat: detect api drift and expose health"
```

### Task 15: Docker Compose、Tunnel 操作手册和备份恢复

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yaml`
- Create: `docs/operations.md`
- Create: `docs/tunnel-setup.md`
- Create: `scripts/backup_postgres.py`
- Create: `scripts/restore_postgres.py`
- Create: `tests/integration/test_backup_restore.py`

**Interfaces:**
- Produces: one immutable image with `mcp` and `worker` commands.
- Produces: Compose services `maimemo-mcp` and `maimemo-worker`, using an external PostgreSQL connection and a private internal network.
- Produces: verified Tunnel Client instructions based on the official release available at implementation time; no invented Docker image.
- Produces: cross-platform backup and restore commands that invoke PostgreSQL client tools without shell interpolation.

- [ ] **Step 1: Write backup/restore integration test**

The test seeds one record from each critical table, creates a custom-format dump, restores into an empty database and verifies row counts, hashes, feedback links and migration revision.

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/integration/test_backup_restore.py -v`

Expected: FAIL because scripts and deployment files are absent.

- [ ] **Step 3: Build the locked non-root image**

Install dependencies with `uv sync --frozen --no-dev`, run as a non-root user, include no `.env`, Token or test fixture with personal data, and define separate commands for MCP and Worker.

- [ ] **Step 4: Add Compose and operational documentation**

Use a secret file mount for the墨墨 Token and fingerprint key. Do not publish the MCP port by default. Document how to verify the exact official Tunnel Client release, connect it to `http://maimemo-mcp:8000/mcp` or the equivalent NAS-local address, and check Tunnel status.

- [ ] **Step 5: Implement cross-platform backup and restore scripts**

Use Python `subprocess.run()` with argument arrays and `check=True` for `pg_dump`, `createdb`, `pg_restore` and validation commands; never build a shell command string. Resolve and validate output paths, write backups to a new explicit file, and never overwrite an existing restore target unless it is explicitly named as disposable.

- [ ] **Step 6: Verify deployment and restore**

Run: `docker compose config`

Run: `docker build -t maimemo-mcp:test .`

Run: `uv run pytest tests/integration/test_backup_restore.py -v`

Expected: Compose validates, image builds, restore test PASS, image history contains no secret.

- [ ] **Step 7: Commit**

```text
git add Dockerfile .dockerignore compose.yaml docs/operations.md docs/tunnel-setup.md scripts/backup_postgres.py scripts/restore_postgres.py tests/integration/test_backup_restore.py
git commit -m "ops: add private nas deployment workflow"
```

### Task 16: 只读冒烟、对话评测和最终验收

**Files:**
- Create: `scripts/smoke_readonly_api.py`
- Create: `tests/evaluation/prompts.yaml`
- Create: `tests/evaluation/test_evaluation_cases.py`
- Create: `tests/evaluation/test_safety_boundaries.py`
- Modify: `README.md`
- Modify: `docs/operations.md`

**Interfaces:**
- Produces: an explicit allowlist-only smoke runner for the 17 read operations.
- Produces: evaluation cases for direct, indirect, follow-up, missing-data, feedback and unsupported-write prompts.
- Produces: final operator checklist mapping every spec acceptance criterion to evidence.

- [ ] **Step 1: Write the evaluation corpus**

Include at least one direct and one indirect prompt for every composite tool, identifier reuse across turns, stale and partial data, explicit feedback, speculative feedback, retraction and attempts to invoke unsupported墨墨 writes.

- [ ] **Step 2: Write evaluation-corpus and safety tests**

Parse every evaluation case and verify that referenced expected tools exist, argument constraints match their schemas, speculative feedback cases never designate a write tool, and no case references an unregistered墨墨 write capability. Actual model tool selection is an end-to-end observation performed in Step 8, not claimed by this deterministic test.

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/evaluation -v`

Expected: FAIL until the evaluation harness and metadata are complete.

- [ ] **Step 4: Implement the allowlist smoke runner and evaluation harness**

The smoke runner requires an explicit `--confirm-readonly` flag, validates operation names against the 17-operation allowlist, redacts all results and prints only pass/fail, latency and record counts. It must refuse unknown or write operations.

- [ ] **Step 5: Run the full local verification suite**

Run: `uv lock --check`

Run: `uv run ruff check .`

Run: `uv run mypy src`

Run: `uv run pytest -v`

Run: `docker compose config`

Run: `docker build -t maimemo-mcp:test .`

Expected: every command exits 0.

- [ ] **Step 6: Run protocol inspection**

Run the service locally, then run: `npx @modelcontextprotocol/inspector@latest`

Expected: Inspector connects to `/mcp`, lists exactly 24 tools, every declared output matches its output schema, 17 atomic tools are read-only, and no墨墨 write tool exists.

- [ ] **Step 7: Run the real read-only smoke test only after the user supplies the Token secret**

Run: `uv run python scripts/smoke_readonly_api.py --confirm-readonly`

Expected: all 17 read operations either PASS or report an evidence-backed account/data prerequisite; no personal payload is printed or committed.

- [ ] **Step 8: Connect Secure MCP Tunnel and run representative ChatGPT prompts**

Verify direct, indirect, follow-up, stale-data, explicit feedback and unsupported-write scenarios. Record tool name, arguments, result class, warnings and confirmation behavior without storing personal result bodies.

- [ ] **Step 9: Complete the acceptance matrix**

Update `docs/operations.md` with evidence for all ten acceptance criteria from spec section 19. Any environment-dependent item that could not run must state the exact blocker, substitute check and remaining risk.

- [ ] **Step 10: Commit**

```text
git add scripts/smoke_readonly_api.py tests/evaluation README.md docs/operations.md
git commit -m "test: verify end-to-end learning workflows"
```

## Final Verification Gate

Before claiming completion, run every command below from a clean checkout with the locked dependencies:

```text
uv lock --check
uv run ruff check .
uv run mypy src
uv run pytest -v
docker compose config
docker build -t maimemo-mcp:test .
git status --short
```

Expected: all commands exit 0 and `git status --short` is empty. Real墨墨 API smoke tests and Secure MCP Tunnel tests are separate environment-dependent gates; they must not be reported as passed unless their actual output was observed.
