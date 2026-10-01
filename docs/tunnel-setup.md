# Secure MCP Tunnel 接入

核验日期：2026-10-02。以下步骤只采用 OpenAI 官方文档和 `openai/tunnel-client` 官方
仓库；本项目不把 Tunnel Client 打进应用镜像，也不假设某个未核验的第三方 Docker 镜像。

## 1. 当日核验结论

- OpenAI 官方说明 Secure MCP Tunnel 由私网内的 `tunnel-client` 主动建立出站 HTTPS，
  私有 MCP 无需开放公网入站端口。
- 当日 `openai/tunnel-client` 的 GitHub `releases/latest` 解析为 `v0.0.15`。运维手册仍应
  指向 `releases/latest`，每次安装/升级时重新记录实际 tag、SHA256 与来源，不把
  `v0.0.15` 当永久最新版。
- HTTP MCP 使用 `--mcp-server-url`，ChatGPT 中选择 Tunnel 并选择或填写 `tunnel_id`；
  Responses API 使用 `tunnel_id`，不能把 OpenAI 托管的 tunnel endpoint 当 `server_url`。
- 需要 Platform tunnel 的相应 Read/Use/Manage 权限，以及 ChatGPT workspace 单独允许的
  Developer mode。Tunnel 还必须关联目标 ChatGPT workspace。

官方一手来源：

- <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>
- <https://github.com/openai/tunnel-client/releases/latest>
- <https://github.com/openai/tunnel-client>
- <https://developers.openai.com/plugins/deploy/connect-chatgpt>

## 2. 下载与供应链核验

1. 打开 <https://platform.openai.com/settings/organization/tunnels>，优先使用页面提供的受支持
   下载；也可进入官方 latest release。
2. 选择与 NAS OS/CPU 匹配、文件名前缀为 `tunnel-client-` 的完整客户端 archive；不要误选
   仅提供 `run` 的 `tunnel-client-runtime-` / `tunnel-client-runtime-cloudflared-` 窄运行时，
   因为后续初始化和诊断需要完整客户端。不要从网盘或第三方镜像下载。
3. 同时下载该 release 的 `SHA256SUMS.txt` 和对应 provenance/SBOM sidecar，并按官方仓库
   README 的当期命令验证 checksum 与 GitHub attestation。验证失败立即停止。
4. 运行 `tunnel-client --version` 和 `tunnel-client help quickstart`，把版本、artifact
   SHA256、NAS 架构和验证时间写入私有运维记录。运行时 API key 不写入该记录。

当前官方仓库已经描述官方 GHCR runtime image，但 NAS 本项目优先使用原生 release binary
并由 systemd、Synology Task Scheduler 或等价 NAS supervisor 管理。若以后改用容器，必须
当场从官方 release 证据核验精确 tag/digest，不能只写 `latest`。

## 3. 配置 NAS-local HTTP MCP

先按 `docs/operations.md` 的本机 override，只在回环地址发布 MCP：

```text
http://127.0.0.1:8000/mcp
```

验证应用端点：

```text
curl --fail --silent http://127.0.0.1:8000/health/ready
```

在 Platform 创建/选择 tunnel，生成权限仅为 Tunnels Read + Use 的 runtime API key。不要把
管理员 key 用于长期 daemon。根据本机 `help quickstart` 的当期语法初始化 HTTP profile；
官方文档在核验日给出的关键参数为：

```text
tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile maimemo-nas \
  --tunnel-id tunnel_REPLACE_WITH_REAL_ID \
  --mcp-server-url http://127.0.0.1:8000/mcp
tunnel-client doctor --profile maimemo-nas --explain
tunnel-client run --profile maimemo-nas
```

通过 supervisor 注入 `CONTROL_PLANE_API_KEY`；不要把值放入命令行、profile、Compose 或
Git。NAS 必须能出站访问 `api.openai.com:443`（配置 control-plane mTLS 时为
`mtls.api.openai.com:443`），并能访问上述本机 MCP URL。禁止新增公网入站 NAT/端口转发。

## 4. 健康和 ChatGPT 连接

`tunnel-client run` 保持运行时，依次检查：

1. `tunnel-client doctor --profile maimemo-nas --explain` 成功。
2. 官方 client 的 `/healthz`、`/readyz` 与本机 `/ui` 显示进程健康、就绪且 connected。
   管理 UI 默认只允许 loopback，不要为了方便直接暴露到局域网或公网。
3. ChatGPT 打开 Settings → Security and login，启用 Developer mode（是否可用取决于计划和
   workspace policy）。
4. 在 ChatGPT Plugins 中创建 developer-mode app，Connection 选 Tunnel，选择该 tunnel 或
   填写 `tunnel_id`，然后确认发现的工具恰为项目声明的 24 个。
5. 用 MCP Inspector 和 Task 16 的评测集测试直接、间接、追问、反馈确认与不支持写操作。

若 ChatGPT 看不到 tunnel，先检查 workspace association、Tunnels Read + Use 权限及权限变更
传播时间；若发现/调用失败，确认 daemon 仍在运行并重新执行 `doctor --explain`。不得用开放
公网 MCP 端口规避这些权限或关联问题。
