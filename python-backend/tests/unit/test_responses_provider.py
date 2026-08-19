# -*- coding: utf-8 -*-

"""Unit tests for the independent non-streaming Responses adapter."""

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from kiro.converters_responses import (
    THINKING_BUDGETS,
    ResponsesConversionError,
    convert_responses_request,
)
from kiro.models_responses import ResponsesRequest
from kiro.responses_provider import (
    build_responses_kiro_payload,
    build_responses_object,
    estimate_responses_usage,
)
from kiro.streaming_core import StreamResult


def test_responses_request_keeps_instruction_order_and_injects_once():
    """System/developer content remains ordered and is not duplicated."""
    request = ResponsesRequest(
        model="gpt-5.6-sol",
        instructions="top-level instruction",
        input=[
            {"type": "message", "role": "system", "content": "system instruction"},
            {"type": "message", "role": "developer", "content": "developer instruction"},
            {"type": "message", "role": "user", "content": "hello"},
        ],
    )

    request_ir = convert_responses_request(request)

    assert request_ir.external_model_id == "gpt-5.6-sol"
    assert request_ir.system_prompt == (
        "top-level instruction\n\nsystem instruction\n\ndeveloper instruction"
    )
    assert request_ir.system_prompt.count("top-level instruction") == 1
    assert [message.role for message in request_ir.messages] == ["user"]
    assert request_ir.messages[0].content == "hello"


def test_responses_string_input_builds_independent_unified_message():
    """A string input becomes one user item without Chat model reuse."""
    request_ir = convert_responses_request(
        ResponsesRequest(model="external-model", input="hello")
    )

    assert request_ir.items[0].item_type == "message"
    assert request_ir.items[0].role == "user"
    assert request_ir.messages[0].content == "hello"


def test_responses_rejects_stateful_and_hosted_capabilities():
    """Unsupported capabilities fail explicitly before Kiro conversion."""
    with pytest.raises(ResponsesConversionError, match="previous_response_id"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input="hello",
                previous_response_id="resp_old",
            )
        )

    with pytest.raises(ResponsesConversionError, match="Hosted Tool"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input="hello",
                tools=[{"type": "web_search_preview"}],
            )
        )


def test_responses_retains_readable_reasoning_summary_as_assistant_context():
    """Readable prior reasoning summaries survive stateless replay."""
    request_ir = convert_responses_request(
        ResponsesRequest(
            model="model",
            input=[
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Plan retained"}],
                },
                {"type": "message", "role": "user", "content": "continue"},
            ],
        )
    )

    assert request_ir.messages[0].role == "assistant"
    assert request_ir.messages[0].content == "Plan retained"


def test_responses_ignores_encrypted_and_empty_reasoning_items():
    """Unavailable reasoning does not become an error or empty message."""
    request_ir = convert_responses_request(
        ResponsesRequest(
            model="model",
            input=[
                {"type": "reasoning", "encrypted_content": "opaque"},
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "user", "content": "continue"},
            ],
        )
    )

    assert [message.role for message in request_ir.messages] == ["user"]
    assert request_ir.messages[0].content == "continue"


def test_responses_keeps_readable_reasoning_content_without_summary():
    """Readable reasoning content is not mistaken for an encrypted field."""
    request_ir = convert_responses_request(
        ResponsesRequest(
            model="model",
            input=[
                {"type": "reasoning", "content": "Readable context"},
                {"type": "message", "role": "user", "content": "continue"},
            ],
        )
    )

    assert request_ir.messages[0].content == "Readable context"


@pytest.mark.parametrize("effort, expected_budget", [("none", None), *THINKING_BUDGETS.items()])
def test_responses_maps_thinking_budget_without_global_cap(effort, expected_budget):
    """Responses effort values retain their documented Kiro budget exactly."""
    request_ir = convert_responses_request(
        ResponsesRequest(
            model="model",
            input="hello",
            reasoning={"effort": effort},
        )
    )

    assert request_ir.thinking_config.enabled is (effort != "none")
    assert request_ir.thinking_config.budget_tokens == expected_budget
    assert request_ir.thinking_config.enforce_budget_cap is False
    assert request_ir.thinking_config.include_system_guidance is (effort != "none")


def test_responses_none_disables_thinking_tags_and_system_guidance():
    """The explicit none setting disables the gateway thinking behavior."""
    request = ResponsesRequest(
        model="model",
        input="hello",
        reasoning={"effort": "none"},
    )
    request_ir = convert_responses_request(request)
    payload = build_responses_kiro_payload(
        request_ir,
        profile_arn="",
        kiro_model_id="model",
    ).payload
    content = payload["conversationState"]["currentMessage"]["userInputMessage"]["content"]

    assert "<thinking_mode>" not in content
    assert "Extended Thinking Mode" not in content


def test_responses_accepts_safe_metadata_include_cache_key_and_verbosity():
    """Safe response controls are accepted and verbosity is best-effort steering."""
    request_ir = convert_responses_request(
        ResponsesRequest(
            model="model",
            input="hello",
            include=["reasoning.encrypted_content"],
            metadata={"trace_id": "trace-1"},
            prompt_cache_key="cache-1",
            text={"format": {"type": "text"}, "verbosity": "low"},
            store=False,
        )
    )

    assert "Response verbosity preference: low" in request_ir.system_prompt


def test_responses_rejects_invalid_verbosity_and_prompt_cache_retention():
    """Unsupported best-effort controls fail instead of being silently ignored."""
    with pytest.raises(ResponsesConversionError, match="verbosity"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input="hello",
                text={"verbosity": "extreme"},
            )
        )

    with pytest.raises(ResponsesConversionError, match="reasoning effort"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input="hello",
                reasoning={"effort": []},
            )
        )

    with pytest.raises(ResponsesConversionError, match="strict"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input="hello",
                tools=[
                    {
                        "type": "function",
                        "name": "run",
                        "parameters": {"type": "object"},
                        "strict": True,
                    }
                ],
            )
        )

    with pytest.raises(ResponsesConversionError, match="prompt_cache_retention"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input="hello",
                prompt_cache_retention="24h",
            )
        )


def test_responses_rejects_orphaned_tool_result_and_unimplemented_controls():
    """Protocol items and controls are rejected instead of losing semantics."""
    with pytest.raises(ResponsesConversionError, match="no preceding function_call"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input=[
                    {
                        "type": "function_call_output",
                        "call_id": "call_missing",
                        "output": "result",
                    }
                ],
            )
        )

    with pytest.raises(ResponsesConversionError, match="max_output_tokens"):
        convert_responses_request(
            ResponsesRequest(model="model", input="hello", max_output_tokens=10)
        )

    with pytest.raises(ResponsesConversionError, match="parallel_tool_calls"):
        convert_responses_request(
            ResponsesRequest(model="model", input="hello", parallel_tool_calls=False)
        )

    with pytest.raises(ResponsesConversionError, match="Duplicate function_call_output"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input=[
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "run",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "first",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "duplicate",
                    },
                ],
            )
        )

    with pytest.raises(ResponsesConversionError, match="missing matching"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input=[
                    {
                        "type": "function_call",
                        "call_id": "call_unanswered",
                        "name": "run",
                        "arguments": "{}",
                    }
                ],
            )
        )

    with pytest.raises(ResponsesConversionError, match="base64"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "https://example.test/image.png",
                            }
                        ],
                    }
                ],
            )
        )

    with pytest.raises(ResponsesConversionError, match="file ID"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "file_id": "file_123"}],
                    }
                ],
            )
        )


def test_responses_payload_limit_fails_without_trimming(monkeypatch):
    """Oversized stateless requests return a shrinkable error before Kiro."""
    monkeypatch.setattr("kiro.responses_provider.KIRO_MAX_PAYLOAD_BYTES", 100)
    request = ResponsesRequest(model="model", input="x" * 200)
    request_ir = convert_responses_request(request)

    with pytest.raises(ResponsesConversionError, match="Reduce"):
        build_responses_kiro_payload(request_ir, profile_arn="", kiro_model_id="model")


def test_responses_rejects_malformed_replayed_function_arguments():
    """Malformed replayed tool arguments fail before Kiro JSON conversion."""
    with pytest.raises(ResponsesConversionError, match="valid JSON"):
        convert_responses_request(
            ResponsesRequest(
                model="model",
                input=[
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "run",
                        "arguments": "{not-json",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "done",
                    },
                ],
            )
        )


def test_responses_output_omits_empty_message_and_preserves_tool_call_ids():
    """Output contains only non-empty message or function-call items."""
    request = ResponsesRequest(model="external-model", input="call a tool")
    request_ir = convert_responses_request(request)
    result = StreamResult(
        tool_calls=[
            {
                "id": "call_preserved",
                "type": "function",
                "function": {"name": "run", "arguments": '{"x":1}'},
            }
        ]
    )

    body = build_responses_object(
        request,
        request_ir,
        result,
        model_cache=None,
        response_id="resp_test",
    )

    assert body["id"] == "resp_test"
    assert body["object"] == "response"
    assert body["model"] == "external-model"
    assert len(body["output"]) == 1
    assert body["output"][0]["type"] == "function_call"
    assert body["output"][0]["call_id"] == "call_preserved"
    assert body["output"][0]["id"].startswith("fc_")
    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["output_tokens"] > 0
    assert body["usage"]["total_tokens"] > 0


@pytest.mark.parametrize(
    ("tool_call", "error_text"),
    [
        (
            {
                "id": "call_bad",
                "type": "function",
                "function": {"name": "run", "arguments": "not-json"},
            },
            "malformed arguments",
        ),
        (
            {
                "id": "call_missing_name",
                "type": "function",
                "function": {"arguments": "{}"},
            },
            "without a function name",
        ),
        (
            {
                "id": "call_truncated",
                "type": "function",
                "_truncation_detected": True,
                "function": {"name": "run", "arguments": "{}"},
            },
            "truncated",
        ),
    ],
)
def test_responses_output_rejects_malformed_kiro_tool_calls(
    tool_call: Dict[str, Any],
    error_text: str,
) -> None:
    """Malformed upstream Tool Calls fail instead of becoming valid output."""
    request = ResponsesRequest(model="model", input="run")
    request_ir = convert_responses_request(request)

    with pytest.raises(ResponsesConversionError, match=error_text):
        build_responses_object(
            request,
            request_ir,
            StreamResult(tool_calls=[tool_call]),
            model_cache=None,
            response_id="resp_malformed",
        )


def test_responses_usage_uses_kiro_context_percentage_when_available():
    """Kiro context usage is used and its source is retained diagnostically."""
    request_ir = convert_responses_request(
        ResponsesRequest(model="model", input="hello")
    )
    result = StreamResult(content="answer", context_usage_percentage=10.0)
    cache = SimpleNamespace(get_max_input_tokens=lambda model: 1000)

    usage = estimate_responses_usage(result, request_ir, cache, "model")

    assert usage.total_tokens == 100
    assert usage.input_source.startswith("Kiro contextUsagePercentage")
    assert usage.output_tokens > 0


def test_responses_usage_only_reports_cache_tokens_when_upstream_reports_them():
    """The Responses usage object never invents a fixed cache count."""
    request_ir = convert_responses_request(
        ResponsesRequest(model="model", input="hello")
    )
    result = StreamResult(content="answer", usage={"cachedTokens": 3})

    body = build_responses_object(
        ResponsesRequest(model="model", input="hello"),
        request_ir,
        result,
        model_cache=None,
    )

    assert body["usage"]["input_tokens_details"] == {"cached_tokens": 3}
