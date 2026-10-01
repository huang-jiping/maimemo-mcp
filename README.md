# maimemo-mcp

墨墨学习数据基础设施，使用 Python 3.12、PostgreSQL 和 MCP Python SDK v2。
当前交付项目骨架、配置接口与固定 OpenAPI 基线；服务、采集和数据库功能按实施计划继续建设。

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
