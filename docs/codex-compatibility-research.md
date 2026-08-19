# KiroaaS 与 Codex 兼容性研究

更新时间：2026-08-19  
研究范围：当前 KiroaaS 源码、仓库内 KiroProxy 对照实现、OpenAI/Codex 官方文档。本文只记录研究结论，不修改功能代码。

## 结论摘要

当前 KiroaaS 不能直接作为 Codex 的自定义 Responses provider 使用。它注册了 `/v1/models` 和 `/v1/chat/completions`，没有 `/v1/responses`；这不是把请求路径改名就能解决的协议差异。KiroaaS 的入口和注册位置见 [routes_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/routes_openai.py:122)（122-161）及 [main.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/main.py:567)（567-572）。

Codex 当前自定义 provider 的 `wire_api` 只有 `responses` 一个支持值，而且省略时默认也是 `responses`；`base_url`、`env_key`、`requires_openai_auth`、SSE 空闲超时和重试也在同一配置项中定义。[Codex Configuration Reference](https://developers.openai.com/codex/config-reference)（页面当前重定向到 learn.chatgpt.com）明确说明了这一点。因此，“KiroaaS 已支持 Chat Completions，所以 Codex 可直接使用”是不成立的假设。

推荐的长期方案是：新增独立的 Responses 入站适配层和 Responses SSE 事件编码器，复用 KiroaaS 已有的账户选择、认证、模型解析、Kiro payload 构造及 AWS Event Stream 解析；不要把 Responses 请求硬塞进 `ChatCompletionRequest`，也不要把 KiroProxy 的逐 `httpx` chunk 原始 AWS 帧解析器直接复制过来。

## 1. 官方协议约束

### 1.1 Codex provider 约束

Codex 配置参考中的关键事实：

- `model_providers.<id>.base_url` 是 provider API 基地址；`env_key` 指定从环境变量读取 API key。
- `requires_openai_auth` 默认是 `false`，适合 KiroaaS 这类自定义 Bearer key provider。
- `stream_idle_timeout_ms` 默认 300000 ms，`stream_max_retries` 默认 5；服务端应保持合法 SSE 流，并避免无界空闲。
- `wire_api` 的唯一支持值是 `responses`，省略时也默认为 `responses`。
- `model_reasoning_effort` 的有效值包括 `minimal`、`low`、`medium`、`high`、`xhigh`，且标注为 Responses API only。

来源：[Codex Configuration Reference](https://developers.openai.com/codex/config-reference)。

因此，后续实现和验证的最低 HTTP 契约应是：

```toml
model_provider = "kiroaas"

[model_providers.kiroaas]
name = "KiroaaS"
base_url = "http://127.0.0.1:8000/v1"
env_key = "KIROAAS_API_KEY"
wire_api = "responses"
requires_openai_auth = false
```

上例只表达 Codex provider 配置形状；实际 `base_url` 是否需要带 `/v1`，应以当前 Codex 版本的 URL 拼接行为做一次端到端验证。

### 1.2 Responses 请求、工具和流式事件

- `POST /v1/responses` 的输入可以是字符串或 item 数组；`instructions` 是独立的高优先级指令字段；工具定义位于 Responses 工具格式。
- 函数调用输出 item 使用 `type: "function_call"`、`call_id`、`name` 和 JSON 编码的 `arguments`。下一轮把工具结果作为 `type: "function_call_output"`、同一个 `call_id` 和 `output` 送回。见 [Function calling guide](https://developers.openai.com/api/docs/guides/function-calling)。
- `stream=true` 时服务端通过 SSE 发出事件。官方事件模型要求事件携带 `sequence_number`；文本消息至少涉及 output item、content part、文本 delta/done 和最终 response 生命周期事件。见 [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)。
- 官方示例明确给出 `response.created`、`response.output_item.added/done`、`response.content_part.added/done`、`response.function_call_arguments.delta/done`、`response.completed` 和 `response.failed` 等事件。工具参数 delta 是字符串片段，最终 done 事件给出完整 JSON 字符串。
- `/v1/models` 的标准响应是 `{object: "list", data: [...]}`，模型对象至少包含 `id`、`object`、`created`、`owned_by`。见 [Models API reference](https://developers.openai.com/api/reference/resources/models/methods/list)。

这里的“至少”是协议字段要求，不代表每个 Codex 版本对所有事件的严格程度完全相同。实现时应按规范完整事件流设计，并用实际 Codex 客户端做验收。

## 2. 当前 KiroaaS 差距分析

| 能力 | 当前事实 | 影响 |
| --- | --- | --- |
| Responses 路由 | 只有 `GET /v1/models` 和 `POST /v1/chat/completions`；路由文件没有 `/v1/responses`。见 [routes_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/routes_openai.py:122) 122-161。 | Codex 的 `wire_api=responses` 请求没有匹配入口。 |
| 应用注册 | OpenAI 路由注释和注册只覆盖 models/chat completions。见 [main.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/main.py:567) 567-572。 | 即使新增 handler，也要纳入统一 FastAPI router。 |
| 入站数据模型 | `ChatCompletionRequest` 只描述 `model/messages/stream`、生成参数和 Chat tools。见 [models_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/models_openai.py:128) 128-184。 | Responses 的 input item、instructions、function_call_output、local_shell 等不能可靠地用现有模型表达。 |
| OpenAI 输出 | `streaming_openai.py` 生成 Chat Completions SSE；非流式也收集为 Chat completion。见 [streaming_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/streaming_openai.py:72) 72-108 和 576-688。 | 返回对象、事件名、tool call ID 和生命周期都不是 Responses wire format。 |
| 可复用转换 | `converters_openai.py` 已把 Chat messages/tools 转成 `UnifiedMessage`/`UnifiedTool`，并委托 core 构造 Kiro payload。见 [converters_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/converters_openai.py:141) 141-293、393-445。 | 可复用低层 Kiro 转换，但需要新的 Responses input adapter。 |
| 可复用 Kiro payload | core 已处理 schema 清洗、工具名限制、toolUses/toolResults 配对、角色交替、历史和图片。见 [converters_core.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/converters_core.py:439) 439-634、995-1398、1405-1597。 | 适合作为 Responses adapter 的下游，不应重复实现 Kiro 格式。 |
| Kiro 流解析 | `AwsEventStreamParser.feed()` 会把跨 chunk 的 bytes 累积到 buffer，再解析完整 JSON；`parse_kiro_stream()` 使用该解析器。见 [parsers.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/parsers.py:211) 211-306、[streaming_core.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/streaming_core.py:118) 118-154。 | 这是 Responses SSE 适配器应复用的稳定边界。 |
| 模型列表 | `/v1/models` 返回账户 resolver 的缓存列表。见 [routes_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/routes_openai.py:138) 138-157。runtime endpoint 分支明确跳过 `/ListAvailableModels`，初始化和刷新都使用 `FALLBACK_MODELS`。见 [account_manager.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/account_manager.py:67) 67-93、500-545、581-624。 | “模型列表长期不更新”在当前 runtime 模式下是设计限制加静态 fallback 过期风险，不是单纯的 Responses 路由 bug。 |
| 动态学习 | `report_success()` 会把成功的未知模型加入 account mapping，但没有把它加入 model cache 的公开列表。见 [account_manager.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/account_manager.py:765) 765-797。 | 成功调用未知模型后，`/v1/models` 仍可能不显示它。 |
| fallback 内容 | `FALLBACK_MODELS` 目前已有若干较新的 Claude、DeepSeek、GLM、MiniMax、Qwen ID，但源码也承认 fallback 会过时。见 [config.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/config.py:269) 269-290。 | 不应直接复制 KiroProxy 的旧 fallback；应明确动态列表、缓存和 fallback 的优先级。 |

## 3. KiroProxy 对照：可复用部分和不能照搬的部分

### 3.1 可复用设计

KiroProxy 已提供一条完整的参考路径：

1. [main.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/main.py:153) 153-156 注册 `/v1/responses`，handler 独立于 Chat Completions。
2. `_convert_responses_input_to_kiro()` 将字符串/消息 item、assistant 历史、函数调用、工具输出、图片和 instructions 转成 Kiro history/current user。见 [responses.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/handlers/responses.py:258) 258-444。
3. `_convert_tools_to_kiro()` 同时识别 Responses 的平铺 function tool 和 Chat 的嵌套 function tool，并把 `local_shell`、`tool_search`、`image_generation` 降级为 Kiro 普通工具，把 web search 转成 Kiro webSearchTool。见 [responses.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/handlers/responses.py:447) 447-603。
4. `handle_responses()` 负责请求解析、账户选择、历史截断、工具配对、Kiro 请求和流/非流分支。见 [responses.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/handlers/responses.py:606) 606-806。
5. KiroProxy 的 `/v1/models` 优先访问 q host 的 `/ListAvailableModels`，失败才使用缓存和静态 fallback。见 [main.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/main.py:71) 71-133，以及 [env_config.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/env_config.py:147) 147-151。

这些代码可以作为输入语义、工具类型和响应对象的参考，但 KiroaaS 使用 `runtime.{region}.kiro.dev` 时不能假定 q host 的 `/ListAvailableModels` 也存在。两套代码的 endpoint 假设不同，必须用当前认证方式验证。

### 3.2 KiroProxy Responses streaming 的规范缺口

KiroProxy 的流 handler 在 [responses.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/handlers/responses.py:843) 843-1067。其最小文本路径可以作为联调起点，但不是规范完整实现：

| 缺口 | 代码证据 | 规范影响 |
| --- | --- | --- |
| 没有 `sequence_number` | `response.created`、message output item、tool item、`response.completed` 的 payload 都没有该字段，见 935-1065。 | 官方 Responses streaming event 对该字段有定义；缺失会影响严格客户端的事件排序和状态机。 |
| 缺少 content part 生命周期 | 文本路径直接发 `response.output_text.delta`，然后发 `response.output_item.done`，没有 `response.content_part.added/done`。见 948-1011。 | 不能视为完整的 message/content 层级事件流。 |
| 缺少文本 done 事件 | 只有 delta，没有 `response.output_text.done`。见 961-976。 | 客户端不能按规范事件确认文本片段完成，只能依赖 output item done。 |
| 函数参数没有 delta/done | 工具调用只在 1022-1043 一次性发 output item added/done，arguments 直接放入最终 item。 | 工具调用流不符合官方 `response.function_call_arguments.delta/done` 事件模型。 |
| usage 固定为零 | `response.completed` 在 1057-1063 固定 `input_tokens/output_tokens/total_tokens=0`。 | 可作为“未知”占位，但不能声称 token 统计准确；需要在文档和测试中明确。 |
| AWS frame 按 httpx chunk 解析 | 963-967 把每个 `aiter_bytes()` chunk 直接交给 `_extract_content_from_chunk()`；1075-1106 只在当前 chunk 内按 total length 解析，遇到半帧就 break，异常被静默吞掉。 | TCP/HTTP chunk 边界不等于 AWS Event Stream frame 边界，可能丢首字、尾字或 tool 事件。 |

KiroaaS 自己的 `AwsEventStreamParser` 已有跨 chunk buffer 和完整 JSON 边界处理，故应复用它，而不是复制 KiroProxy 的 `_extract_content_from_chunk()`。

### 3.3 最小可用与规范完整的界线

| 层级 | 目标 | 可接受内容 | 不能作出的承诺 |
| --- | --- | --- | --- |
| 最小联调 | 先让 Codex 完成纯文本请求 | 非流式完整 response；或流式 `response.created`、文本 delta、output item done、`response.completed`；稳定 response/item ID。 | 不能保证所有 Codex 版本接受缺少 sequence/content-part/text-done 的简化流。 |
| 规范完整 | 作为长期 provider | 每个事件的 `sequence_number`；response created/in-progress/completed/failed/incomplete；message output item；content part added/done；output text delta/done；function call item 和 arguments delta/done；正确的 call_id、output_index、content_index；可获得时填真实 usage。 | Kiro 后端没有 token 计数时不能伪造精确 usage；Kiro 不支持的 hosted tool 不能伪装成真正的 OpenAI hosted tool。 |

实现顺序可以先完成最小非流式，再增加规范完整流式；但验收标准应以第二层为准，避免形成只能被某个客户端容错接受的短期协议。

## 4. 建议目标架构和数据流

```text
Codex
  │ POST /v1/responses + Bearer key
  ▼
Responses route / request adapter
  │ input + instructions + tools + response options
  ▼
Responses IR（消息、工具、call_id、图片、模型名）
  │
  ├── model resolver / account manager / auth / retry
  ├── UnifiedMessage + UnifiedTool
  └── converters_core.build_kiro_payload()
          │
          ▼
      Kiro runtime generateAssistantResponse
          │ AWS Event Stream bytes
          ▼
  AwsEventStreamParser / parse_kiro_stream
          │ KiroEvent
          ▼
  Responses response builder + SSE state machine
          │
          ▼
      Codex response object / Responses events
```

建议分层如下：

- **Responses adapter**：保留原始 Responses item 语义，解析 `input`、`instructions`、消息角色、图片、函数工具和 tool output。它可以借鉴 KiroProxy 的转换，但应输出 KiroaaS 的 `UnifiedMessage`/`UnifiedTool` 或专门的 Responses IR。
- **共享下游**：复用 `AccountManager`、`ModelResolver`、`KiroHttpClient`、`converters_core.build_kiro_payload()` 和 `parse_kiro_stream()`。Chat 与 Responses 只在协议边界分叉。
- **Responses event encoder**：维护 response ID、output item ID、call ID、output/content index 和递增 sequence number。Kiro 的 content/tool events 到达时，按状态机发出合法 SSE，而不是把 Chat chunk 重新包装。
- **错误与 usage**：Kiro HTTP/解析/超时错误分别映射为 HTTP error 或 `response.failed`；若 Kiro 只提供 context percentage 而没有 token 数，usage 应显式为未知或约定的零值，并在响应扩展/日志中标注来源。
- **模型显示名与内部名**：请求中的 Codex 模型名、实际送给 Kiro 的内部模型名、返回 response 的 model 字段要分别保存，不能因 alias 解析而丢失用户请求名。
- **模型列表**：保留 KiroaaS 的 account resolver 聚合；runtime 模式继续使用 fallback 作为兜底，但应增加显式刷新策略、时间戳/来源信息，并确认是否存在可认证访问的动态模型接口。不要把“成功调用未知模型”只写入 account mapping 后就认为 `/v1/models` 已更新。

## 5. 分阶段实现清单

### P0：契约和测试基线

- 定义 Responses request/response/event 的内部类型或受控字典结构，保留未知字段以便 Codex 演进。
- 将 KiroProxy fixture `tests/fixtures/e2e/responses_codex_stream.json` 转为 KiroaaS 的无网络单元测试输入；不要把 fixture 的 `source.reference` 当作官方规范。
- 固定 response ID、item ID、call ID、index 和 sequence number 的生成规则。
- 加入 `/v1/responses` 路由、Bearer API key 校验和请求错误格式测试。

### P1：非流式 Responses

- 支持纯文本 input、消息 item、instructions、model、基本 generation/reasoning 选项。
- 复用模型解析、账户 failover、Kiro payload 和完整 AWS Event Stream 收集器。
- 输出 `object=response`、`status`、message output item、`output_text`，并覆盖无文本/工具调用两种结果。
- 先实现普通 function tool 和 `function_call_output` 往返；严格检查 call_id 配对。

### P2：规范流式 Responses

- 用 `parse_kiro_stream()` 产生 KiroEvent，禁止按裸 httpx chunk 猜测 AWS frame。
- 实现 sequence counter 和 response/output/content 状态机。
- 文本路径发出 created/in-progress（如客户端需要）、output item added、content part added、output text delta/done、content part done、output item done、completed。
- 工具路径发出 function output item 生命周期及 arguments delta/done；确保最终 output 与事件中使用同一 item/call ID。
- 覆盖 Kiro HTTP error、首 token timeout、流中断、incomplete 和客户端断开。

### P3：工具、图片和历史

- 复用 core 的 JSON Schema 清洗、工具名限制、长描述截断和角色交替。
- 支持 Responses function/custom 工具、多个并行调用、图片 data URL。
- 对 `local_shell`、`tool_search`、`web_search`、`image_generation` 做能力声明和降级策略；不能把普通 Kiro function 冒充为真正 hosted tool。
- 验证 assistant function_call → function_call_output → 下一轮响应的跨请求历史。

### P4：模型列表和配置

- 先确认 `runtime.{region}.kiro.dev` 是否永远没有 `/ListAvailableModels`，还是仅当前认证路径没有；用真实认证请求验证，不凭 KiroProxy 的 q host 假设推断。
- 若 runtime 无动态接口：为 fallback 增加版本/来源/刷新诊断，允许成功模型进入公开缓存，或提供明确的配置追加模型；避免静默声称列表实时。
- 若存在动态接口：按 TTL 异步刷新并合并账户结果，失败时保留上次成功缓存，再退回 fallback。
- 对 `gpt-*-codex` 等 Codex 模型别名建立显式映射和能力说明；不要默认把未知 OpenAI 名称原样送给 Kiro。
- 更新 README/API 表和 Codex 配置示例，删除“仅有 Chat Completions 即兼容 Codex”的表述。

### P5：验收

- 运行全部现有 KiroaaS tests，再运行 Responses 单元、流式协议和本地 FastAPI 集成测试。
- 用当前目标 Codex CLI/App 配置真实请求：先纯文本非流式，再纯文本流式，再 function call 往返；记录客户端版本和失败事件。
- 验证 `/v1/models` 的排序、去重、缓存来源和未知模型行为。

## 6. 测试矩阵

| 类别 | 用例 | 预期断言 |
| --- | --- | --- |
| 基本响应 | 字符串 input，非流式 | 200；`object=response`；message/output_text；model 和 status 正确 |
| 基本响应 | item 数组、多 user/assistant 历史 | Kiro history 角色交替且不丢文本 |
| 指令 | `instructions`、developer/system 等高优先级输入 | 指令只进入约定位置，优先级不被 user 覆盖 |
| 流式文本 | Kiro 内容跨多个 AWS frame/chunk | 每个完整 delta 恰好一次；没有因边界丢字 |
| 流式协议 | 事件顺序和字段 | sequence 单调递增；index/ID 一致；content part 和 output item 成对 |
| 流式结束 | completed、failed、incomplete、超时 | 状态、error/incomplete_details、usage 形状正确 |
| 普通工具 | function 定义；模型发 function_call | name、JSON arguments、call_id 稳定；非流式 output 正确 |
| 工具回传 | function_call_output 成功/错误 | 与同一 call_id 配对；Kiro toolResults 正确；下一轮可继续 |
| 多工具 | 多个并行 function call、不同 output_index | 每个 item/call 独立，顺序和索引不交叉 |
| 工具类型 | custom、local_shell、tool_search、web_search、image_generation | 每类有明确映射/降级/拒绝行为，不静默伪装能力 |
| 输入媒体 | `input_image` data URL、混合文本图片 | 图片进入 Kiro 当前 user，非法 data URL 可预测报错 |
| Schema | 空 required、additionalProperties、超长描述、超长名称 | 复用 core 清洗/限制；错误信息可定位 |
| 历史 | assistant toolUses 后 tool output、缺失/重复 call_id | 合法配对通过；非法输入返回 4xx 或明确降级 |
| 模型 | alias、未知模型、`gpt-*-codex` | 内部模型名和返回显示名符合映射策略；未知模型不会破坏列表 |
| 模型列表 | 动态成功、动态失败、runtime fallback、TTL 刷新 | 来源、缓存、fallback 顺序稳定；去重；`object/data` 符合模型 API |
| 认证 | 缺失/错误 Bearer、Codex `env_key` | 401/403 行为稳定，不把 provider key 泄漏到日志 |
| 回归 | 现有 Chat Completions/Anthropic tests | 新 adapter 不改变已有协议输出 |

## 7. 风险与待确认事项

1. **Codex 客户端严格程度**：官方事件规范要求 sequence number 和 content-part/function-argument 生命周期；需要用实际目标 Codex 版本确认哪些缺失字段会立即失败。研究结论不能把 KiroProxy 的“看起来可用”当作兼容证明。
2. **Kiro runtime 模型发现**：KiroaaS 明确记录 runtime endpoint 不提供 `/ListAvailableModels`，而 KiroProxy 依赖 q host。必须确认当前账号/区域的真实 API 能力，再决定动态列表方案。
3. **Codex 模型到 Kiro 模型的映射**：`gpt-5-codex` 这类请求名未必是 Kiro 可接受的内部 ID。需要产品层决定允许哪些别名、返回哪个 model 字段，以及 `/v1/models` 是否公开别名。
4. **Usage 准确性**：KiroProxy 当前把所有 token usage 固定为 0；KiroaaS 若无法得到 tokenizer 统计，也不应制造“准确”数字。可以先返回约定的未知/零值，同时在文档中说明限制。
5. **工具能力边界**：Kiro 的普通 tool specification 与 OpenAI hosted tools 不是同一执行环境。local shell、web search、image generation 是否由 Codex 本地执行，必须与服务端输出 item 语义分开。
6. **Responses 状态存储**：若只接受 Codex 每轮完整 input，可保持无状态；若要支持 `previous_response_id`、retrieve 或跨请求服务端历史，则需要额外存储和生命周期设计，不应在第一阶段隐式加入。
7. **SSE 断线重试**：Codex 配置默认允许 SSE 重试。要么实现可安全重试/去重的响应 ID 和事件，要么明确关闭/限制重试，避免重复执行 Kiro 请求或重复工具调用。

## 8. 关键来源索引

### 官方一手来源

- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference)：provider `base_url`、`env_key`、`requires_openai_auth`、stream timeout/retries、`wire_api=responses`。
- [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)：SSE、sequence number、response 生命周期、content part、文本和函数参数事件。
- [Function calling guide](https://developers.openai.com/api/docs/guides/function-calling)：`function_call`、`call_id`、JSON arguments 和 `function_call_output` 往返。
- [Models API reference](https://developers.openai.com/api/reference/resources/models/methods/list)：`GET /v1/models` 返回结构和模型对象字段。

### 仓库源码

- KiroaaS 路由与注册：[routes_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/routes_openai.py:122)、[main.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/main.py:567)。
- KiroaaS 请求模型与 Chat 转换：[models_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/models_openai.py:128)、[converters_openai.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/converters_openai.py:141)。
- KiroaaS core payload 和协议安全处理：[converters_core.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/converters_core.py:439)。
- KiroaaS AWS stream parser：[parsers.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/parsers.py:211)、[streaming_core.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/streaming_core.py:118)。
- KiroaaS 模型缓存/账户策略：[account_manager.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/account_manager.py:67)、[config.py](/Users/sid/PycharmProjects/proxy/KiroaaS/python-backend/kiro/config.py:269)。
- KiroProxy Responses route/adapter/stream：[main.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/main.py:153)、[responses.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/handlers/responses.py:258)、[responses.py](/Users/sid/PycharmProjects/proxy/KiroProxy/kiro_proxy/handlers/responses.py:843)。

