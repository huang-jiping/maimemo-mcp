# maimemo

[![CI](https://github.com/huang-jiping/maimemo/actions/workflows/ci.yml/badge.svg)](https://github.com/huang-jiping/maimemo/actions/workflows/ci.yml)

墨墨学习数据基础设施，使用 Python 3.12、PostgreSQL 和 MCP Python SDK v2。项目接入
17 个墨墨只读操作，使用 Worker 建立可追溯的学习历史，通过 5 个组合工具提供进度、
单词画像、薄弱词、复习压力和数据健康信息，并用 2 个本地追加式工具记录和撤销混淆反馈。

墨墨侧严格只读；本地反馈工具只写入 PostgreSQL 的追加事件，不修改墨墨账户数据。

## 开发

安装 uv 后运行 `uv sync --frozen`，执行 `uv run python -m pytest -v`、
`uv run ruff check .`，并对 `packages/*/src` 运行 mypy。所有依赖固定在 `uv.lock`。

## 配置

参考 `.env.example`，通过进程环境传入配置。各运行包只读取自己的设置：Server 和迁移
只需要 `MAIMEMO_DATABASE_URL`；MCP 与 Worker 还需要 `MAIMEMO_TOKEN_FILE` 和
`MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE`。Token 和独立的指纹密钥从只读文件
或 Docker Secret 注入，不支持明文 Token 环境变量。
不要提交真实凭据、Token 或个人学习数据。

默认学习时区为 `Asia/Shanghai`，今日数据间隔为 30 分钟，学习记录间隔为 120 分钟。
配置的两个采集间隔同时用于调度、健康新鲜度和评分证据质量，不需单独配置查询阈值。
MCP 容器内监听 `0.0.0.0:8000`；NAS 模板只发布主机回环地址 `127.0.0.1:8000`。
时间处理接口要求输入带时区的 datetime，数据库时间采用 UTC。

## OpenAPI 基线

`openapi/maimemo-api.yaml` 来源为 [官方 OpenAPI](https://open.maimemo.com/api_bundle.yaml)，
下载日期为 2026-10-02；SHA-256 存于 `openapi/maimemo-api.sha256`。
本次基线包含 38 个操作，其中设计批准的 17 个操作按业务语义只读，包含查询类 POST。
固定规范不表示允许调用其中的写操作；本项目墨墨侧范围严格只读。

设计和实施计划见 `docs/superpowers/`；镜像发布与验收规则见 `docs/operations.md`。

## 运行形态

仓库是四包工作区：共享核心 `maimemo`，以及三个可独立构建和部署的运行包
`maimemo-server`、`maimemo-mcp`、`maimemo-worker`。Compose 项目固定为 `maimemo`，包含：

- `migrate`：复用 Server 镜像的一次性数据库升级任务；
- `server`：主页、健康检查和阶段 B 的 OAuth 入口；
- `mcp`：Streamable HTTP MCP 服务，内部端点为 `/mcp`；
- `worker`：按上海学习日采集正式历史并计算薄弱词；MCP 或 Tunnel 重启不影响它。

三个镜像使用统一版本：`ghcr.io/huang-jiping/maimemo-server`、
`ghcr.io/huang-jiping/maimemo-mcp`、`ghcr.io/huang-jiping/maimemo-worker`。

正式采集在同一事务提交原始快照、规范化历史、成功 slot 和评分；评分失败会回滚并允许
同一 slot 重试。today 在锁后采样时钟，并核对两次 HTTP 前后的上海日期；跨午夜安全失败，
不把不同学习日的结果混写。缺必填字段的响应仅由 Worker 留存到隔离的
`failed_api_snapshot`，不参与 BASELINE、有效历史或评分，也不会进入 MCP 或日志。
如果失败响应回显当前 API Token，会在留档前遮盖；失败留档写入自身出错时也只抛受控类别。
迁移头为 `0004`；该失败证据表非空时，降级会明确拒绝丢失证据。

合成数据的真实 Worker→PostgreSQL→评分→MCP 回归位于
`tests/mcp/test_composite_tools.py::test_worker_persists_scores_visible_through_real_mcp`。
这些本地证据不表示真实墨墨 API 或 Secure MCP Tunnel 门禁已经通过。

推荐用 `compose.yaml` 连接 NAS 上已有的 PostgreSQL 15+。部署时先执行
`docker compose run --rm migrate upgrade head`，再启动 `server`、`mcp`、`worker`。
默认不发布 MCP 端口；Secure MCP Tunnel 在同一 Docker 网络内使用 `http://mcp:8000/mcp`，原生 NAS Tunnel Client
则只绑定 `127.0.0.1`。完整部署、迁移、密钥权限、备份恢复和 Tunnel 步骤见
`docs/operations.md`、`docs/tunnel-setup.md` 与 `docs/migration/maimemo-0.2.0.md`。

UGOS Pro 私有部署从 [DEPLOYMENT.md](DEPLOYMENT.md) 开始：在 Docker → 项目中导入
[NAS Compose](deploy/nas/compose.yaml)，项目名同样为 `maimemo`。NAS 模板使用三个经 digest
固定的镜像和四个服务，Worker 每六小时检查一次 OpenAPI 漂移。部署前必须满足 Docker
Engine 安全版本或厂商回补门禁，并从另一台 LAN 主机验证回环发布端口不可达。

## 只读真实接口冒烟

以下源码脚本仅在保留源码与锁定依赖的开发工作站执行，不属于 NAS 项目的部署或运维流程。

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
已经验证模型会选择正确工具。解析器显式区分 annotation 和 assertion：只忽略固定白名单
中的纯注解，实现 type、format、对象路径、required 以及可证明的字符串长度子集；enum、
const、pattern、数值/数组/对象收窄断言和其他未知关键字一律拒绝。Profile 输出保留其输入
已证明的 1–500 字符边界，使合法多轮引用无需绕过约束。对象 conjunction 会逐项判断属性：
未在某个 conjunct 的 `properties` 中声明的属性仍受该 conjunct 的 `additionalProperties`
约束；Schema 形式会与其他 child Schema 合取，`false` 拒绝，`true` 或缺省不增加约束。
真正的工具选择、参数生成和确认行为必须在 Secure MCP Tunnel 连通后由 ChatGPT 工作区
端到端观察，并按 `docs/operations.md` 只记录工具名、参数类别、结果类别和警告代码。
