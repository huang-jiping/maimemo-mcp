# maimemo-mcp

墨墨学习数据基础设施，使用 Python 3.12、PostgreSQL 和 MCP Python SDK v2。项目接入
17 个墨墨只读操作，使用 Worker 建立可追溯的学习历史，通过 5 个组合工具提供进度、
单词画像、薄弱词、复习压力和数据健康信息，并用 2 个本地追加式工具记录和撤销混淆反馈。

墨墨侧严格只读；本地反馈工具只写入 PostgreSQL 的追加事件，不修改墨墨账户数据。

## 开发

安装 uv 后运行 `uv sync --frozen`，执行 `uv run pytest -v`、
`uv run ruff check src tests` 和 `uv run mypy src`。所有依赖固定在 `uv.lock`。

## 配置

参考 `.env.example`，通过进程环境传入配置。`Settings.load()` 不自动加载 `.env`。
`MAIMEMO_DATABASE_URL`、`MAIMEMO_TOKEN_FILE` 和
`MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE` 必填。Token 和独立的指纹密钥从只读文件
或 Docker Secret 注入，仅显式调用读取接口时读取，不支持明文 Token 环境变量。
不要提交真实凭据、Token 或个人学习数据。

默认学习时区为 `Asia/Shanghai`，今日数据间隔为 30 分钟，学习记录间隔为 120 分钟。
MCP 默认监听 `0.0.0.0:8000`，便于容器内 Tunnel 访问；部署时不要将端口映射到公网。
时间处理接口要求输入带时区的 datetime，数据库时间采用 UTC。

## OpenAPI 基线

`openapi/maimemo-api.yaml` 来源为 [官方 OpenAPI](https://open.maimemo.com/api_bundle.yaml)，
下载日期为 2026-10-02；SHA-256 存于 `openapi/maimemo-api.sha256`。
本次基线包含 38 个操作，其中设计批准的 17 个操作按业务语义只读，包含查询类 POST。
固定规范不表示允许调用其中的写操作；本项目墨墨侧范围严格只读。

设计和实施计划见 `docs/superpowers/`。

## 运行形态

同一镜像提供两个互相独立的命令：

- `mcp`：Streamable HTTP MCP 服务，内部端点为 `/mcp`；
- `worker`：按上海学习日采集正式历史并计算薄弱词；MCP 或 Tunnel 重启不影响它。

推荐用 `compose.yaml` 连接 NAS 上已有的 PostgreSQL 15+。默认不发布 MCP 端口；Secure MCP
Tunnel 在同一 Docker 网络内使用 `http://maimemo-mcp:8000/mcp`，原生 NAS Tunnel Client
则只绑定 `127.0.0.1`。完整部署、迁移、密钥权限、备份恢复和 Tunnel 步骤见
`docs/operations.md` 与 `docs/tunnel-setup.md`。

## 只读真实接口冒烟

只有在用户明确提供 Token 文件并完成数据库配置后，才运行：

```text
uv run python scripts/smoke_readonly_api.py --confirm-readonly
```

脚本采用固定的 17 项 allowlist，不读取 MCP 工具发现结果来执行未来接口；缺少
`--confirm-readonly`、未知名称或任何写操作都会在调用前失败。可重复使用
`--operation get_vocabulary` 仅运行指定 allowlist 项。按账户资源 ID 查询的操作还必须用
`--resource-id OPERATION.FIELD=VALUE` 显式提供所需 ID；没有 ID 时记录 PREREQUISITE，且
不会调用该工具。输出只包含操作名、PASS/FAIL 或 PREREQUISITE、耗时、记录数和缺少的固定
字段名，不输出或保存 ID 值、响应正文、异常正文及个人数据。

资源 ID 采用 fail-closed 输入策略：只接受长度 1–1000 的非空格可打印 ASCII（字节范围
33–126）；空格、DEL、Unicode 分隔符/控制字符及其他非 ASCII 字符都在调用前拒绝，错误
消息不回显原值。如果未来官方 ID 合法字符范围改变，应先增加契约证据与回归测试再调整。

例如，只有以下参数齐全时才会调用章节读取；参数值不会出现在输出中：

```text
uv run python scripts/smoke_readonly_api.py --confirm-readonly \
  --operation get_markji_chapter \
  --resource-id get_markji_chapter.deck=ACCOUNT_DECK_ID \
  --resource-id get_markji_chapter.chapter=ACCOUNT_CHAPTER_ID
```

`PREREQUISITE` 只表示操作员尚未提供该操作要求的真实账户 ID，并不表示接口通过。提供 ID
后，资源不存在、401、429、超时、服务端错误、Schema 错误和任何未知错误都统一为 FAIL，
不能降级成 PREREQUISITE。真实冒烟失败时不要把终端输出扩展为响应正文日志。

## 对话评测

`tests/evaluation/prompts.yaml` 覆盖每个组合工具的直接和间接表达、多轮标识符复用、
过期/部分数据、明确反馈、推测性反馈、撤销以及不支持的墨墨写入请求。确定性测试只校验
语料结构、已注册工具、参数 Schema 和安全边界；多轮引用还会沿前序工具 output Schema 和
当前工具 input Schema 解析 `$ref`/`anyOf`/对象/数组路径，并 fail-closed 校验类型、可空性
和 format 兼容性。`anyOf` 各可达来源分支逐一验证，不能用其他安全分支掩盖；中间路径的
任一 null/缺失分支都会拒绝；Draft 2020-12 的 `$ref` sibling 与引用目标合取。它不声称
已经验证模型会选择正确工具。
真正的工具选择、参数生成和确认行为必须在 Secure MCP Tunnel 连通后由 ChatGPT 工作区
端到端观察，并按 `docs/operations.md` 只记录工具名、参数类别、结果类别和警告代码。
