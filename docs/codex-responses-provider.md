# KiroaaS Responses Provider：Codex 配置与发布门槛

本文档对应 issue #10。它描述当前 `POST /v1/responses` 的真实边界，不把“能返回 HTTP 200”当作 Codex CLI/App 兼容证明。

## 1. CLI 与 App 共用配置

KiroaaS 监听本机时，先启动后端，并把后端的 `PROXY_API_KEY` 作为下面的 `KIROAAS_API_KEY`。API key 只放在环境变量中，不要写进 `config.toml`。

```bash
export KIROAAS_API_KEY='与后端 PROXY_API_KEY 相同的本地密钥'
```

在用户级 `~/.codex/config.toml` 中加入以下配置。当前 Codex 配置参考把 `model_providers.<id>.base_url` 定义为 provider API base URL，因此这里包含 `/v1`，实际请求路径是 `/v1/responses`。

```toml
model = "gpt-5.6-sol"
model_provider = "kiroaas"
model_reasoning_effort = "medium"

[model_providers.kiroaas]
name = "KiroaaS Responses"
base_url = "http://127.0.0.1:8000/v1"
env_key = "KIROAAS_API_KEY"
wire_api = "responses"

# KiroaaS 只在尚未发出 Responses 事件前重试。
# 禁用 Codex 侧重试，避免无状态请求被重复执行或重复触发 Client Tool。
request_max_retries = 0
stream_idle_timeout_ms = 300000
stream_max_retries = 0
```

配置字段对应关系：

| 项目 | 值 | 说明 |
| --- | --- | --- |
| Base URL | `http://127.0.0.1:8000/v1` | 反向代理地址；不要再附加 `/responses`。 |
| API key | `KIROAAS_API_KEY` | Codex 发送 `Authorization: Bearer ...`；值必须等于后端 `PROXY_API_KEY`。 |
| Wire API | `responses` | 只使用 `POST /v1/responses`，不是 Chat Completions。 |
| Model | `gpt-5.6-sol` | 也可使用 `/v1/models` 返回的 Canonical Model ID。 |
| Stream timeout | `300000 ms` | Codex SSE idle timeout；KiroaaS 的后端读取超时也应保持足够大。 |
| HTTP retry | `0` | 由 KiroaaS 在首个 Responses 事件前负责安全重试。 |
| Stream retry | `0` | KiroaaS 不提供 SSE 断点续传；客户端重放可能重复执行。 |

CLI 和 App 是否都读取同一份用户级配置、以及下列 gate 是否能通过，必须用对应版本的客户端实际运行确认。本次实现没有执行本机 Codex CLI/App 请求，因此下面的 App/CLI 兼容性仍是“未验证”。

## 2. 能力矩阵

### 输入与状态

- Provider 是无状态的。每次请求必须重新发送完整 `input` 数组或字符串。
- 只实现 response creation：`POST /v1/responses`。不实现 response retrieve/delete/cancel、input items、compaction、background mode 或 server-side conversation storage。
- `previous_response_id`、`conversation`、`background=true`、`store=true`、prompt template 和其他需要服务端保存状态的字段会明确报错。
- KiroaaS 不替客户端删除、摘要或重排输入。超过 Kiro payload/context 限制时返回可操作的 4xx 错误，并要求客户端缩短历史、图片或工具定义。

### Thinking Budget

`reasoning.effort` 是 KiroaaS 的 best-effort Thinking Budget，不是 OpenAI 原生 reasoning token 语义：

| effort | KiroaaS budget |
| --- | ---: |
| `none` | 禁用 gateway thinking behavior |
| `minimal` | 1024 |
| `low` | 4096 |
| `medium` | 12000 |
| `high` | 20000 |
| `xhigh` | 22528 |
| `max` | 24576（KiroaaS extension） |

Codex 当前配置参考公开的 effort 集合不包含 `max`；推荐从 `minimal`、`low`、`medium` 或 `high` 开始。额度是 prompt steering 和 Kiro 模型行为的近似，不是精确 token 上限。

### Client Tool 与 Hosted Tool

- Client Tool 由 Codex/客户端执行。KiroaaS 只传递 tool definition、Tool Call 和后续 Tool Result。
- 支持并保留 Responses 语义的 Client Tool 类型包括 `function`、`custom`、`shell`/`local_shell`、`tool_search` 和 `apply_patch` 对应的客户端桥接。
- `call_id` 必须在 Tool Call 与 `function_call_output`/对应 Tool Result 之间保持一致。多 Tool Call 使用独立 id 和 output index。
- Hosted Tool 不支持。`web_search`、`web_search_preview`、hosted `image_generation`、remote MCP、file search、computer use 等不会降级成普通 Kiro function；请求会得到明确的 unsupported capability error。
- `tool_choice` 只支持自动 Client Tool 选择；服务端不承诺 OpenAI hosted execution 或强制特定服务 tier。

### 媒体

- 支持本地 `data:image/...;base64,...` 的 Responses 图片输入，并传给 Kiro 的当前 user message。
- 拒绝远程 URL、`file_id`、`file://` 和其他需要网关下载/存储的媒体引用。这样避免 SSRF、隐式下载和服务端文件生命周期。
- 音频、视频和图片输出不在当前 provider 的发布承诺内。

### Structured Output、service tier 与 usage

- `text.format.type=text` 可用；JSON Schema、`json_object` 等 structured output 请求明确拒绝。KiroaaS 不会用 prompt 诱导冒充 schema 保证。
- `service_tier` 不提供 OpenAI 服务等级保证；非默认值明确拒绝。
- `usage` 是最佳估算：输入/输出优先使用现有 tokenizer，若 Kiro 提供 `contextUsagePercentage` 则用于估算总量；Kiro 的 `usage` 事件本身是 credit metering，不等同于 token accounting。
- 内部诊断会记录估算来源，例如 `tokenizer` 或 `Kiro contextUsagePercentage (...)`，但默认日志不记录 prompt、源代码、命令输出或 Tool Result 内容。不要把估算值当作账单精确值。

### 错误边界

- 在流式 `response.created` 之前：验证、认证、模型解析、首事件前的 Kiro HTTP/超时失败返回普通 HTTP error。
- 已发出 `response.created` 后：错误转换为一个终止的 `response.failed` SSE 事件；不会重启生成，也不会混合两个账号的输出。
- 成功流只发送一个 `response.completed`。事件含递增 `sequence_number`、稳定 response/item/call id 和对应生命周期。
- 客户端断开会取消上游读取。当前 provider 不承诺 SSE resumption 或跨请求幂等。

## 3. Fixture 与网络隔离

Fixture 注册表位于 [`python-backend/tests/fixtures/responses/manifest.json`](../python-backend/tests/fixtures/responses/manifest.json)，严格区分三类证据：

1. `official_contract`：从 OpenAI 官方 Responses streaming-events 文档提炼的最小 contract。它不是网络 capture，也不是 KiroProxy 输出。
2. `sanitized_codex_capture`：按客户端和版本预留的 capture 槽。本次运行按要求没有执行 Codex CLI/App，因此 CLI `0.147.0` 与 App `26.814.41407` 两个槽均为 `status=not_verified` 且 `capture=null`，不能用于宣称兼容。
3. `adversarial_aws_stream`：仓库内合成的 AWS Event Stream 字节，刻意在 prelude、header、payload、UTF-8 和 CRC 边界切 chunk。它只验证 KiroaaS parser 的跨传输 chunk 行为。

所有 fixture 都带 `source`、`source_type`、`recorded_at` 和 `not_kiroproxy_capture=true`。KiroProxy 只可作为历史参考，不能把其 capture 标为官方 Responses 协议或 Codex capture。

自动化测试使用全局 `block_all_network_calls`，fixture parser 测试只读取仓库文件，不建立网络连接：

```bash
cd python-backend
../.venv/bin/pytest -q tests/integration/test_codex_release_fixtures.py tests/unit/test_codex_release_gate.py
```

## 4. 发布门槛与当前结果

发布判定是合取：所有 gate 必须为 `passed`。`not_verified`、`failed`、`blocked` 都会阻断发布；非通过记录必须写出客户端版本、请求阶段和可复现的协议差异或未执行原因。判定逻辑在 [`python-backend/kiro/codex_release_gate.py`](../python-backend/kiro/codex_release_gate.py)。本次真实 Kiro 验证记录时间为 2026-08-19，使用本机现有 Kiro AWS SSO OIDC 登录链路；没有输出或提交凭据。

| Gate | 客户端/环境 | 请求阶段 | 结果 | 证据或协议差异 |
| --- | --- | --- | --- | --- |
| Kiro 认证模型发现 | KiroaaS + 本机 Kiro AWS SSO OIDC 链路 | `GET /v1/models` | `passed` | HTTP 200；账户初始化成功并获取模型缓存；当前 `/v1/models` 记录为动态发现不可用时回退 Curated Fallback，返回 15 个模型。只证明 Kiro 认证链路，不证明 Codex。 |
| Kiro 非流式文本 | KiroaaS + 本机 Kiro AWS SSO OIDC 链路 | `POST /v1/responses`, `stream=false` | `failed` | 请求在 `response.created` 前以 HTTP 502 结束；Kiro runtime 连接失败，服务端进行了 3 次上游尝试（其中 2 次重试）。 |
| Kiro 流式文本 | KiroaaS + 本机 Kiro AWS SSO OIDC 链路 | `POST /v1/responses`, `stream=true` | `failed` | 请求在 `response.created` 前以 HTTP 502 结束；没有 SSE 事件可验证，协议差异为当前环境无法连接 Kiro runtime。 |
| Hosted Tool error | KiroaaS + 本机 KiroaaS route | `POST /v1/responses` with `web_search_preview` | `passed` | 上游调用前 HTTP 400，明确返回 Hosted Tool unsupported。 |
| Codex CLI 文本/工具/取消/error | Codex CLI `0.147.0` | 未执行 | `not_verified` | 按本次执行要求不做本机 Codex CLI 实测；无客户端协议差异可报告。 |
| Codex App 文本/Client Tool | Codex App `26.814.41407` | 未执行 | `not_verified` | 按本次执行要求不做本机 Codex App 实测；无客户端协议差异可报告。 |

机器可读的完整记录在 [`python-backend/tests/fixtures/responses/release_gate_records.json`](../python-backend/tests/fixtures/responses/release_gate_records.json)，由 evaluator 强制要求完整 gate 集合，而不是只评估调用方传入的子集。CLI 的认证模型发现、非流式文本、流式文本、单 Client Tool loop、多 Tool Call、取消和 unsupported capability error 共 7 个 gate；App 的文本与 Client Tool 共 2 个 gate。当前这些客户端 gate 都是 `not_verified`，并且 Kiro 两个文本 gate 失败，因此当前不能宣称“Codex CLI/App 兼容完成”，也不能达到发布门槛。要解除阻断，必须在对应客户端和版本上实际完成这些操作；失败时将请求阶段与具体协议差异追加到记录，并重新运行 gate。

## 5. 回归命令

旧协议回归必须包含完整 Chat Completions 与 Anthropic 测试，而不是只跑 Responses：

```bash
cd python-backend
../.venv/bin/pytest -q tests/unit tests/integration
```

本次结果：完整后端回归 `1857 passed`，包含 Chat Completions 与 Anthropic；新增 Responses fixture/gate 定向测试与 Responses 回归共 `89 passed`。pytest 报告 4 个既有弃用/async mock warning，没有失败。前端 `npm test -- --run` 为 `12 passed`，`npm run build` 成功；构建工具报告的 Browserslist 数据过期和大 chunk 是既有警告。上述结果已写入 gate record，但不替代对应 Codex CLI/App 实测。

前端测试和构建：

```bash
npm test -- --run
npm run build
```

官方参考：

- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference)
- [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [Function calling guide](https://developers.openai.com/api/docs/guides/function-calling)
