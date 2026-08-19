## Problem Statement

KiroaaS advertises compatibility with Codex but currently exposes only OpenAI Chat Completions and Anthropic Messages. Current Codex custom model providers use the Responses wire protocol, so Codex cannot use KiroaaS directly. The older KiroProxy implementation demonstrates some useful request conversions but does not implement a complete Responses event lifecycle, reports zero usage, and parses AWS Event Stream frames at unsafe HTTP chunk boundaries. Users also need the current Kiro GPT 5.6 models to appear alongside the existing Kiro model catalog without introducing misleading aliases or a second static model source in the desktop UI.

## Solution

Add a first-class, stateless Responses Provider to KiroaaS. It will implement `POST /v1/responses`, preserve Responses request and output semantics, translate supported Client Tools and conversation items through KiroaaS's existing unified conversion layer, reuse the buffered AWS Event Stream parser, and encode complete non-streaming Responses objects and streaming Responses events. It will preserve the External Model ID while routing with the Kiro Model ID, build the Model Catalog from dynamic account evidence with a Curated Fallback, and add `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` as the primary canonical models. Unsupported stateful, hosted, remote-media, service-tier, and structured-output capabilities will fail explicitly rather than being silently ignored or simulated.

## User Stories

1. As a Codex CLI user, I want to configure KiroaaS as a custom Responses Provider, so that I can use Kiro models for coding tasks.
2. As a Codex App user, I want the same provider configuration to work, so that CLI and desktop workflows use one local gateway.
3. As a Responses client developer, I want KiroaaS to follow the official Responses contract, so that integration does not depend on undocumented Codex tolerance.
4. As a user, I want `POST /v1/responses` to accept a string input, so that simple prompts work without constructing item arrays.
5. As a user, I want `POST /v1/responses` to accept message item arrays, so that I can resend full stateless conversation history.
6. As a user, I want developer and system instructions to retain their relative priority as far as Kiro permits, so that my coding instructions are not silently treated as ordinary user text.
7. As a user, I want non-streaming Responses objects with stable internal IDs and indexes, so that SDK response parsing works predictably.
8. As a user, I want complete streaming lifecycle events, so that Codex can update response, item, content-part, text, and tool-call state correctly.
9. As a user, I want sequence numbers to increase monotonically, so that streaming events can be ordered and validated.
10. As a user, I want text deltas to be emitted exactly once even when AWS frames cross HTTP chunks, so that output is not truncated or duplicated.
11. As a user, I want a terminal completed event after successful generation, so that Codex knows the response is finished.
12. As a user, I want a terminal failed event when an established stream fails, so that Codex does not hang or mistake partial output for success.
13. As a user, I want pre-stream validation and upstream failures returned as HTTP errors, so that failures have one unambiguous transport representation.
14. As a user, I want function tools translated to Kiro and returned as function Tool Calls, so that Codex can execute them locally.
15. As a user, I want custom tools to preserve their custom call type and raw string input, so that custom Tool Results pair correctly.
16. As a Codex user, I want shell, local-shell, tool-search, and apply-patch Client Tools recognized explicitly, so that coding workflows do not degrade them into guessed generic tools.
17. As a user, I want every Tool Call and Tool Result to preserve the same `call_id`, so that multi-turn tool loops remain valid.
18. As a user, I want multiple Tool Calls to have independent IDs and output indexes, so that parallel results cannot cross-wire.
19. As a user, I want unsupported tool types rejected with actionable errors, so that missing capabilities are visible.
20. As a user, I want Hosted Tools rejected until their real Responses events and result semantics are implemented, so that Kiro text output is not misrepresented as hosted execution.
21. As a user, I want readable reasoning summaries from prior turns retained as assistant context, so that useful stateless context is not discarded.
22. As a user, I want opaque encrypted reasoning ignored safely, so that KiroaaS does not expose or pretend to understand unavailable content.
23. As a user, I want base64 data-URL image input supported, so that Codex can send images Kiro can consume directly.
24. As an operator, I want remote image URLs and file references rejected, so that KiroaaS does not become an unbounded downloader or SSRF surface.
25. As a user, I want KiroaaS to preserve my complete input instead of silently trimming or summarizing it, so that content decisions remain under client control.
26. As a user, I want context-limit failures to explain how to reduce the request, so that I can recover without guessing what the gateway removed.
27. As a user, I want reasoning effort mapped to a documented Thinking Budget, so that low through max settings have predictable best-effort behavior.
28. As a user, I want `none` to disable gateway thinking behavior, so that I can request direct output.
29. As a user, I want safe metadata fields accepted, so that current Codex requests are not rejected for irrelevant metadata.
30. As a user, I want unsupported stateful fields rejected, so that `previous_response_id` or stored conversations cannot appear to work while losing context.
31. As a user, I want unsupported structured-output requests rejected, so that prompt-only JSON suggestions are not presented as schema guarantees.
32. As a user, I want usage to contain the best available estimate, so that local usage reporting is more truthful than fixed zero values.
33. As an operator, I want usage estimation provenance in internal diagnostics, so that estimates can be distinguished from upstream measurements.
34. As a user, I want the response to echo my External Model ID, so that aliases or internal routing do not unexpectedly rename the selected model.
35. As an operator, I want logs to include the corresponding Kiro Model ID, so that model-routing failures can be diagnosed.
36. As a user, I want `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` visible first in the Model Catalog, so that current recommended Kiro models are easy to select.
37. As a user, I want existing Claude, DeepSeek, GLM, MiniMax, and Qwen canonical models retained, so that adding GPT 5.6 does not remove other Kiro capabilities.
38. As a user, I want aliases accepted but hidden from the Model Catalog, so that one underlying model is not advertised multiple times.
39. As a multi-account operator, I want unknown successful models verified per account, so that one account's access is not treated as universal availability.
40. As an operator, I want Verified Model evidence to expire, so that removed or plan-restricted models do not remain permanently advertised.
41. As an operator, I want dynamic discovery preferred over the Curated Fallback, so that the catalog follows Kiro when discovery is available.
42. As an operator, I want a Curated Fallback when discovery is unavailable, so that runtime-only accounts still expose a useful catalog.
43. As an operator, I want credit multipliers to remain informational, so that descriptions never influence routing or account selection.
44. As a user, I want account failover only before streaming begins, so that one response never combines two separate model executions.
45. As a user, I want upstream retries only before any Responses event is emitted, so that text and Tool Calls are never duplicated after partial delivery.
46. As an operator, I want client disconnection to cancel the upstream request, so that abandoned tasks do not consume Kiro credit.
47. As a user, I want each HTTP request to receive a new response identity, so that independent generations are never conflated by input hashing.
48. As an operator, I want raw prompts, source code, command output, and Tool Results absent from default logs, so that normal diagnostics do not collect workspace secrets.
49. As a maintainer, I want existing Chat Completions and Anthropic behavior unchanged, so that the new protocol adapter does not regress current clients.
50. As a maintainer, I want one high-level integration seam to validate the provider, so that tests verify externally visible behavior without coupling to internal helpers.
51. As a maintainer, I want official, captured Codex, and cross-chunk fixtures with provenance, so that protocol conformance and client compatibility remain distinguishable.
52. As a user, I want a documented Codex provider configuration and capability limits, so that setup and unsupported features are clear before use.
53. As a desktop user, I want the UI to consume the backend Model Catalog, so that the frontend never maintains a conflicting model list.

## Implementation Decisions

- Implement the Responses Provider as a first-class protocol adapter alongside the existing OpenAI and Anthropic adapters. Do not wrap Chat Completions and do not copy KiroProxy's monolithic handler.
- Add only stateless response creation through `POST /v1/responses`. Continue exposing `GET /v1/models`. Response retrieval, deletion, cancellation endpoints, input-item listing, token-count endpoints, compaction, stored conversations, and background responses are not implemented.
- Reuse the existing Bearer API-key authentication, account selection, authentication refresh, HTTP retry, model resolution, unified Kiro payload construction, JSON Schema sanitation, tool-name handling, image conversion, and buffered AWS Event Stream parsing.
- Keep Responses request models separate from Chat Completion request models. Accept string input and item arrays while retaining unknown, non-semantic metadata only through an explicit allowlist.
- Preserve `instructions` and system/developer messages in their original relative order and inject them once through the existing unified system-prompt path. Document that Kiro instruction hierarchy is a best-effort approximation.
- Remain stateless. Reject `previous_response_id`, conversation references, `background=true`, and `store=true`. Accept omitted `store` and `store=false`.
- Accept safe fields such as selected `include` values, prompt-cache keys, and metadata when they do not require unavailable output. Reject service-tier guarantees. Treat text verbosity as best-effort instruction steering.
- Reject non-default structured-output formats until KiroaaS can validate the complete non-streaming and streaming output contract.
- Support base64 image data URLs. Reject remote URLs, file IDs, and file URLs without downloading them.
- Do not delete, summarize, or reorder client conversation content to fit context limits. Return an actionable context-length error instead.
- Parse Responses input into a protocol-specific intermediate representation that retains original item type, role, item ID, call ID, tool registration, content blocks, images, and External Model ID before conversion into existing unified messages and tools.
- Support Client Tool definitions and replay items for function, custom, local-shell/shell, tool-search, and apply-patch behavior. Use a registry keyed by tool name and call identity so emitted output retains the registered Responses type.
- Bridge custom raw-string tools through an internal Kiro JSON object containing a single `input` string, then restore custom call semantics and raw input at the Responses boundary.
- Preserve call IDs exactly for replayed calls and results. Generate stable unique call IDs when Kiro omits one. Do not reuse response, item, or call identities across HTTP requests.
- Support multiple Tool Calls with stable output ordering and independent indexes. A Kiro tool event that arrives atomically may be represented by one arguments delta followed by the corresponding arguments-done event.
- Reject unknown Client Tool types. Reject Hosted Tools including web search, image generation, and remote MCP until their official item and event contracts can be produced truthfully.
- Retain readable prior reasoning summaries as assistant context. Ignore unavailable encrypted reasoning content and empty reasoning items without fabricating reasoning output.
- Map Thinking Budget values as follows: `none` disables thinking, `minimal` uses 1024, `low` uses 4096, `medium` uses 12000, `high` uses 20000, `xhigh` uses 22528, and the KiroaaS extension `max` uses 24576. Do not claim these are OpenAI-native reasoning tokens.
- Preserve the requested External Model ID in every response object. Resolve and log the Kiro Model ID separately.
- Build the Model Catalog from dynamic model discovery when available. When discovery is unavailable, use the Curated Fallback. Add the three GPT 5.6 models to the Curated Fallback without removing existing canonical Kiro models.
- Present `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` first, followed by other Canonical Model IDs in stable order. Report ownership as Kiro. Put the supplied 2.4x, 1x, and 0.1x credit multipliers in descriptions only.
- Hide aliases from Model Catalog output while continuing to accept them as input. Do not map the three GPT 5.6 IDs to Claude aliases; they pass through as Kiro Model IDs.
- Record successful unknown models as timestamped Verified Models for the accepting account only. Expire the evidence with the account model-cache TTL. Never mutate the Curated Fallback from runtime success.
- For non-streaming requests, collect the complete Kiro stream and build one official Responses object containing only non-empty message and Tool Call output items.
- For streaming requests, implement an explicit event state machine with monotonically increasing sequence numbers, shared response/item/call identities, output and content indexes, response lifecycle events, item lifecycle events, content-part lifecycle events, text delta/done events, function or custom argument delta/done events, and one terminal completed or failed event.
- Feed all upstream bytes through the existing buffered AWS Event Stream parser. Never infer AWS frame boundaries from individual HTTP chunks.
- Before the first Responses event, validation and upstream failures return ordinary HTTP errors. Once streaming begins, terminate failures with `response.failed`.
- Allow account failover, token refresh retry, and generation retry only before the first Responses event is emitted. Never restart generation after a partial stream.
- On client disconnect or request cancellation, cancel upstream reading and close the request-scoped HTTP client. Do not retain background work for reconnection.
- Estimate usage with the existing tokenizer and Kiro context-usage information. Track the measurement source internally. Never fill usage with fabricated fixed zero values.
- Default diagnostics record request/response identity, External and Kiro Model IDs, item and tool types, lengths, status, timing, account identity, and error classification. Raw bodies remain limited to explicitly enabled debug logging and existing redaction controls.
- Update the desktop/API documentation with a Codex provider configuration, Responses examples, supported fields, Thinking Budget mapping, tool limits, state limitations, and Hosted Tool limitations. The frontend continues to obtain models from `GET /v1/models` and does not embed a second catalog.
- Treat real current Codex CLI and Codex App text and Client Tool loops as release gates, not optional manual demonstrations.

## Testing Decisions

- Use one primary high-level seam: invoke the FastAPI application through `POST /v1/responses` with authentication and a network-isolated Kiro upstream stub that returns controlled AWS Event Stream bytes. This seam should exercise routing, validation, protocol conversion, account behavior, upstream parsing, output construction, streaming, and errors together.
- Test externally observable HTTP, JSON, and SSE behavior rather than private helper calls. Pure lower-level tests are justified only for the existing AWS parser boundary or deterministic protocol state machine when a failure cannot be isolated through the primary seam.
- Follow the repository's existing network-isolation fixture and FastAPI route/integration-test conventions. Use the existing full-flow, route, converter, streaming-core, and parser tests as prior art.
- Maintain three fixture classes: minimal official Responses contracts, sanitized captures from a named Codex CLI/App version, and adversarial AWS streams split across every significant frame boundary. Each fixture records its provenance.
- Verify string input, item-array history, instructions, system/developer ordering, readable reasoning summaries, ignored encrypted reasoning, and empty items.
- Verify base64 image input and deterministic rejection of remote URLs and file references.
- Verify function, custom, shell/local-shell, tool-search, and apply-patch definitions, calls, outputs, replay, call-ID pairing, multiple calls, errors, missing IDs, duplicate IDs, malformed arguments, and unknown tool types.
- Verify explicit rejection of Hosted Tools, stateful fields, service-tier promises, structured-output formats, unsupported media, and unsupported tool-choice guarantees.
- Verify non-streaming text-only, tool-only, mixed text/tool, empty, failed, and context-limit responses.
- Verify the full streaming text and Client Tool event order, every required field, monotonically increasing sequence numbers, consistent IDs, stable indexes, exactly-once deltas, and one terminal event.
- Verify AWS frames split before and after length fields, headers, payloads, CRC bytes, UTF-8 boundaries, adjacent frames, and final partial frames without loss or duplication.
- Verify HTTP errors before streaming and `response.failed` after streaming begins.
- Verify retry and account failover before the first event, and verify that neither occurs after the first event.
- Verify client cancellation closes the upstream stream and performs no background continuation.
- Verify usage estimation shape and internal provenance without asserting implementation-specific tokenizer internals.
- Verify Model Catalog stable ordering, canonical-only display, Kiro ownership, GPT 5.6 descriptions, dynamic discovery, Curated Fallback behavior, per-account Verified Model promotion, TTL expiry, alias acceptance, and duplicate removal.
- Run the complete existing Chat Completions and Anthropic suites to prove the new adapter does not alter their public behavior.
- Perform release-gate end-to-end checks with the current Codex CLI and Codex App: authenticated model discovery, non-streaming text, streaming text, single Client Tool loop, multiple Tool Calls, cancellation, and an unsupported capability error.

## Out of Scope

- Server-side response storage, `previous_response_id`, stored conversations, response retrieval/deletion/cancellation APIs, input-item listing, compaction, and background mode.
- Cross-request idempotency, SSE resumption, or deterministic response IDs derived from input.
- Hosted web search, hosted image generation, remote MCP execution, file search, computer use, and other server-executed OpenAI tools.
- Remote image downloads, file upload storage, OpenAI file IDs, and file URLs.
- Guaranteed structured outputs or JSON Schema enforcement.
- Exact OpenAI-native reasoning semantics, encrypted reasoning decryption, or exact reasoning-token accounting.
- Automatic history deletion, summarization, or content-level truncation.
- Guaranteed service tiers or OpenAI-specific performance promises.
- WebSocket transport for Responses.
- Replacing or removing existing OpenAI Chat Completions and Anthropic Messages APIs.

## Further Notes

- KiroProxy is reference material for input shapes and fixtures only. Its simplified streaming encoder, zero usage values, and per-HTTP-chunk AWS parsing are explicitly not implementation baselines.
- The supplied OpenCode configuration is the source for the three GPT 5.6 canonical IDs, display names, credit descriptions, and Thinking Budget values. Runtime availability must still be confirmed through account evidence.
- The official Codex provider configuration supports only the Responses wire protocol, making `POST /v1/responses` a prerequisite rather than an optional compatibility enhancement.
- The project glossary and accepted architectural decisions define the canonical vocabulary and protocol boundaries for implementation.
