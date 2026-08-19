# KiroaaS Gateway

KiroaaS Gateway 将外部 AI 客户端协议转换为 Kiro runtime 可接受的对话协议。本词汇表定义协议边界中的核心概念。

## Language

**Responses Provider**:
向 Codex 及其他兼容客户端提供 Responses API，并将请求转换到 Kiro runtime 的 KiroaaS 服务端。
_Avoid_: Codex proxy, OpenAI-compatible endpoint

**External Model ID**:
客户端看到并提交给 Responses Provider 的模型标识，例如 `gpt-5.6-sol`。
_Avoid_: Codex model, display model

**Canonical Model ID**:
Model Catalog 为一个模型公开的唯一主要 External Model ID；其他可接受名称只是输入别名。
_Avoid_: Preferred alias, display alias

**Kiro Model ID**:
Responses Provider 实际发送给 Kiro runtime 的模型标识。
_Avoid_: Internal model, real model

**Model Catalog**:
Responses Provider 向客户端公开的可用 External Model ID 集合。
_Avoid_: Model list, fallback models

**Curated Fallback**:
动态模型发现不可用时，用于构建 Model Catalog 的人工维护模型集合。
_Avoid_: Hardcoded models, default models

**Verified Model**:
曾被指定 Kiro 账号成功调用，并记录了验证时间的模型；验证只证明该账号在当时可用。
_Avoid_: Supported model, discovered model

**Thinking Budget**:
Responses Provider 根据客户端推理强度请求，为 Kiro 模型分配的 best-effort 思考额度。
_Avoid_: Reasoning tokens, reasoning effort

**Tool Call**:
模型请求客户端执行工具的输出项，使用 `call_id` 与后续结果关联。
_Avoid_: Tool execution, hosted call

**Tool Result**:
客户端针对 Tool Call 回传的执行结果，必须保留相同的 `call_id`。
_Avoid_: Tool response, function response

**Client Tool**:
由客户端执行的工具；Responses Provider 只传递其定义、Tool Call 和 Tool Result。
_Avoid_: Local tool, function tool

**Hosted Tool**:
由模型服务端执行并返回结果的工具，与客户端执行的 Tool Call 属于不同能力边界。
_Avoid_: Server tool, built-in function
