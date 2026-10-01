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
`--operation get_vocabulary` 仅运行指定 allowlist 项。输出只包含操作名、PASS/FAIL 或
PREREQUISITE、耗时和记录数，不输出或保存响应正文、异常正文及个人数据。

部分按 ID 查询的接口使用固定无效占位符，只验证认证、路由和只读调用链；其
`PREREQUISITE` 表示需要从该账户先取得真实资源 ID，并不表示接口通过。真实冒烟失败时
不要把终端输出扩展为响应正文日志。

## 对话评测

`tests/evaluation/prompts.yaml` 覆盖每个组合工具的直接和间接表达、多轮标识符复用、
过期/部分数据、明确反馈、推测性反馈、撤销以及不支持的墨墨写入请求。确定性测试只校验
语料结构、已注册工具、参数 Schema 和安全边界，不声称已经验证模型会选择正确工具。
真正的工具选择、参数生成和确认行为必须在 Secure MCP Tunnel 连通后由 ChatGPT 工作区
端到端观察，并按 `docs/operations.md` 只记录工具名、参数类别、结果类别和警告代码。
