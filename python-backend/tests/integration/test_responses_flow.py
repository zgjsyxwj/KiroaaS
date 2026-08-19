# -*- coding: utf-8 -*-

"""High-level, network-isolated Responses provider tests."""

import json
import struct
from typing import Any, AsyncIterator, List, Optional
import httpx
import zlib


REAL_ASYNC_CLIENT = httpx.AsyncClient


class StubKiroStream(httpx.AsyncByteStream):
    """Yield deliberately split AWS Event Stream frames from a stub."""

    def __init__(self, chunks: List[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class KiroResponsesTransport(httpx.AsyncBaseTransport):
    """Capture the Kiro payload and return isolated AWS Event Stream bytes."""

    def __init__(
        self,
        status_code: int = 200,
        error_body: bytes = b"",
        event_batches: Optional[List[List[dict]]] = None,
    ) -> None:
        """Initialize an isolated Kiro response transport.

        Args:
            status_code: HTTP status returned by the upstream stub.
            error_body: Optional body returned for a non-success status.
            event_batches: Ordered AWS event batches, one batch per request.
        """
        self.payload: Optional[dict] = None
        self.payloads: List[dict] = []
        self.status_code = status_code
        self.error_body = error_body
        self.event_batches = event_batches

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.payload = json.loads(request.content)
        self.payloads.append(self.payload)
        if self.status_code != 200:
            return httpx.Response(
                self.status_code,
                request=request,
                content=self.error_body or b'{"message":"stub upstream failure"}',
            )
        if self.event_batches:
            batch_index = min(len(self.payloads) - 1, len(self.event_batches) - 1)
            events = self.event_batches[batch_index]
        else:
            events = [
                {"content": "Hello "},
                {"content": "world"},
                {"content": "你好"},
                {"usage": 1.2},
                {"contextUsagePercentage": 10.0},
            ]
        frames = [_aws_event_frame(event) for event in events]
        wire = b"".join(frames)
        frame_boundaries = {0, len(wire), 5, 17, 31, 47, 63}
        offset = 0
        for frame in frames:
            payload_start = frame.index(b'{"')
            payload_end = frame.index(b"}", payload_start) + 1
            frame_boundaries.update(
                {
                    offset,
                    offset + 1,
                    offset + 8,
                    offset + 12,
                    offset + payload_start,
                    offset + payload_start + 1,
                    offset + payload_end,
                    offset + len(frame) - 4,
                    offset + len(frame) - 1,
                    offset + len(frame),
                }
            )
            offset += len(frame)
        if "你好".encode("utf-8") in wire:
            utf8_start = wire.index("你好".encode("utf-8"))
            frame_boundaries.update(
                {
                    utf8_start + 1,
                    utf8_start + 2,
                    utf8_start + 4,
                    utf8_start + 5,
                }
            )
        split_points = tuple(sorted(point for point in frame_boundaries if point < len(wire))) + (len(wire),)
        stream = StubKiroStream(
            [wire[start:end] for start, end in zip(split_points, split_points[1:])]
        )
        return httpx.Response(200, request=request, stream=stream)


def _aws_event_frame(payload: dict) -> bytes:
    """Encode one minimal AWS Event Stream frame for the isolated stub."""
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    header_name = b":message-type"
    headers = (
        bytes([len(header_name)])
        + header_name
        + b"\x07"
        + struct.pack(">H", len(b"event"))
        + b"event"
    )
    prelude = struct.pack(">II", 16 + len(headers) + len(payload_bytes), len(headers))
    prelude_crc = struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + prelude_crc + headers + payload_bytes
    return message + struct.pack(">I", zlib.crc32(message) & 0xFFFFFFFF)


def _patch_kiro_client(monkeypatch: Any, transport: KiroResponsesTransport) -> None:
    """Route request-scoped Kiro clients to one isolated transport."""
    import kiro.http_client

    def make_stub_client(**kwargs: Any) -> httpx.AsyncClient:
        """Build one request-scoped client for the supplied transport."""
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", make_stub_client)


def test_authenticated_responses_flow_uses_kiro_stub_and_formal_output(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """Exercise HTTP auth, conversion, Kiro collection and output construction."""
    from main import app

    transport = KiroResponsesTransport()

    def make_stub_client(**kwargs):
        """Build the request-scoped client against the isolated transport."""
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    # KiroHttpClient deliberately creates one client per streamed request.
    # Patch its constructor seam so the test cannot reach the network.
    import kiro.http_client

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", make_stub_client)
    monkeypatch.setattr(app.state, "account_system", False)

    array_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={
            "model": "auto-kiro",
            "instructions": "Be concise",
            "input": [
                {"type": "message", "role": "system", "content": "Use plain text"},
                {"type": "message", "role": "user", "content": "Hello"},
            ],
        },
    )
    array_body = array_response.json()
    array_payload = transport.payload
    string_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "gpt-5.6-sol", "input": "Hello again"},
    )
    string_body = string_response.json()

    assert array_response.status_code == 200
    assert string_response.status_code == 200
    assert array_body["object"] == "response"
    assert array_body["model"] == "auto-kiro"
    assert array_body["status"] == "completed"
    assert array_body["output"][0]["type"] == "message"
    assert array_body["output"][0]["content"][0]["text"] == "Hello world你好"
    assert array_body["usage"]["input_tokens"] > 0
    assert array_body["usage"]["output_tokens"] > 0
    assert array_body["usage"]["total_tokens"] > 0
    assert string_body["output"][0]["content"][0]["text"] == "Hello world你好"
    assert array_body["id"] != string_body["id"]
    assert array_body["output"][0]["id"] != string_body["output"][0]["id"]

    array_message = array_payload["conversationState"]["currentMessage"]
    assert "Be concise\n\nUse plain text\n\n" in array_message["userInputMessage"]["content"]
    assert array_message["userInputMessage"]["modelId"] == "auto"
    current_message = transport.payload["conversationState"]["currentMessage"]
    assert current_message["userInputMessage"]["content"].endswith("Hello again")


def test_responses_requires_bearer_auth_without_touching_kiro(
    test_client,
    clean_app,
    monkeypatch,
):
    """Authentication is enforced before any upstream request is created."""
    from main import app

    called = False

    def fail_if_called(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("Kiro client must not be created for unauthenticated input")

    import kiro.http_client

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", fail_if_called)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        json={"model": "model", "input": "hello"},
    )

    assert response.status_code == 401
    assert called is False


def test_responses_returns_sanitized_upstream_error(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """Upstream failures remain HTTP errors before a Responses object exists."""
    from main import app

    transport = KiroResponsesTransport(status_code=400)

    def make_stub_client(**kwargs):
        """Build a request-scoped error client against the isolated transport."""
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    import kiro.http_client

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", make_stub_client)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "kiro_api_error"


def test_responses_does_not_echo_non_json_upstream_error(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """Opaque upstream bodies never become client-visible diagnostic text."""
    from main import app

    transport = KiroResponsesTransport(
        status_code=400,
        error_body=b"secret prompt and source code",
    )

    def make_stub_client(**kwargs):
        """Build a request-scoped client against the isolated error transport."""
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    import kiro.http_client

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", make_stub_client)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello"},
    )

    assert response.status_code == 400
    assert "secret prompt" not in response.text


def test_responses_high_level_seam_preserves_history_images_and_safe_controls(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """The HTTP seam preserves stateless history and passes base64 images to Kiro."""
    from main import app

    transport = KiroResponsesTransport()

    def make_stub_client(**kwargs):
        """Build the request-scoped client against the isolated transport."""
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    import kiro.http_client

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", make_stub_client)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={
            "model": "model",
            "input": [
                {"type": "message", "role": "user", "content": "first"},
                {"type": "message", "role": "assistant", "content": "second"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "third"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AAAA",
                        },
                    ],
                },
            ],
            "include": ["reasoning.encrypted_content"],
            "metadata": {"trace_id": "trace-1"},
            "prompt_cache_key": "cache-1",
            "text": {"format": {"type": "text"}, "verbosity": "low"},
            "store": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["store"] is False
    payload = transport.payload
    assert payload is not None
    history = payload["conversationState"]["history"]
    assert history[0]["userInputMessage"]["content"].endswith("first")
    assert history[1]["assistantResponseMessage"]["content"] == "second"
    current = payload["conversationState"]["currentMessage"]["userInputMessage"]
    assert current["content"].endswith("third")
    assert current["images"] == [{"format": "png", "source": {"bytes": "AAAA"}}]


def test_responses_high_level_seam_rejects_stateful_and_contract_changing_fields(
    test_client,
    clean_app,
    valid_proxy_api_key,
):
    """Unsupported ownership and output guarantees fail at the HTTP boundary."""
    cases = [
        ({"previous_response_id": "resp_old"}, "previous_response_id"),
        ({"conversation": "conv_old"}, "conversation"),
        ({"background": True}, "background"),
        ({"store": True}, "storage"),
        ({"service_tier": "default"}, "service_tier"),
        ({"text": {"format": {"type": "json_schema"}}}, "Structured output"),
        ({"tool_choice": "required"}, "tool choice"),
    ]

    for extra, expected_text in cases:
        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={"model": "model", "input": "hello", **extra},
        )
        assert response.status_code == 400
        assert expected_text.lower() in response.json()["detail"].lower()


def test_responses_high_level_seam_rejects_remote_and_file_media_before_upstream(
    test_client,
    clean_app,
    valid_proxy_api_key,
):
    """Remote URLs and file references never reach the Kiro transport."""
    cases = [
        {"type": "input_image", "image_url": "https://example.test/image.png"},
        {"type": "input_image", "file_id": "file_123"},
        {"type": "input_file", "file_url": "file:///tmp/input.txt"},
    ]

    for block in cases:
        response = test_client.post(
            "/v1/responses",
            headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
            json={
                "model": "model",
                "input": [
                    {"type": "message", "role": "user", "content": [block]}
                ],
            },
        )
        assert response.status_code == 400
        assert any(word in response.json()["detail"].lower() for word in ("url", "file", "base64"))


def test_responses_high_level_seam_reports_actionable_context_limit_error(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """A Kiro context rejection tells the client to reduce its own request."""
    from main import app

    transport = KiroResponsesTransport(
        status_code=400,
        error_body=b'{"message":"Input is too long.","reason":"CONTENT_LENGTH_EXCEEDS_THRESHOLD"}',
    )

    def make_stub_client(**kwargs):
        """Build the request-scoped client against the isolated error transport."""
        return REAL_ASYNC_CLIENT(transport=transport, **kwargs)

    import kiro.http_client

    monkeypatch.setattr(kiro.http_client.httpx, "AsyncClient", make_stub_client)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello"},
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"].lower()
    assert "context limit" in message
    assert "reduce" in message


def test_responses_high_level_seam_returns_validation_error_for_malformed_input(
    test_client,
    clean_app,
    valid_proxy_api_key,
):
    """Malformed JSON shapes fail validation without invoking Kiro."""
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": {"role": "user", "content": "hello"}},
    )

    assert response.status_code == 422


def test_responses_client_tool_loop_preserves_call_id_and_sanitizes_schema(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """A client tool call can be replayed with its result through the HTTP seam."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {
                    "name": "get_weather",
                    "toolUseId": "kiro-call-1",
                    "input": '{"city":"Taipei"}',
                    "stop": True,
                }
            ],
            [{"content": "The weather result was accepted."}],
        ]
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)

    tool = {
        "type": "function",
        "name": "get_weather",
        "description": "Get the current weather.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    }
    first_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "What is the weather?", "tools": [tool]},
    )

    assert first_response.status_code == 200
    first_body = first_response.json()
    call = first_body["output"][0]
    assert call["type"] == "function_call"
    assert call["call_id"] == "kiro-call-1"
    assert call["name"] == "get_weather"
    assert call["arguments"] == '{"city": "Taipei"}'

    first_payload = transport.payloads[0]
    kiro_tool = first_payload["conversationState"]["currentMessage"][
        "userInputMessage"
    ]["userInputMessageContext"]["tools"][0]["toolSpecification"]
    assert kiro_tool["name"] == "get_weather"
    assert "additionalProperties" not in kiro_tool["inputSchema"]["json"]

    replay_input = [
        {"type": "message", "role": "user", "content": "What is the weather?"},
        {
            "type": "function_call",
            "call_id": call["call_id"],
            "name": call["name"],
            "arguments": call["arguments"],
        },
        {
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": '{"temperature":26}',
        },
    ]
    second_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": replay_input, "tools": [tool]},
    )

    assert second_response.status_code == 200
    assert second_response.json()["output"][0]["content"][0]["text"] == (
        "The weather result was accepted."
    )
    replay_payload = transport.payloads[1]
    replay_history = replay_payload["conversationState"]["history"]
    replay_assistant = replay_history[1]["assistantResponseMessage"]
    assert replay_assistant["toolUses"][0]["toolUseId"] == call["call_id"]
    replay_current = replay_payload["conversationState"]["currentMessage"][
        "userInputMessage"
    ]
    replay_results = replay_current["userInputMessageContext"]["toolResults"]
    assert replay_results[0]["toolUseId"] == call["call_id"]
    assert replay_results[0]["content"] == [{"text": '{"temperature":26}'}]


def test_responses_multiple_client_tool_calls_keep_order_and_pair_results(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """Parallel Tool Calls retain independent IDs and client result order."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {
                    "name": "lookup_city",
                    "toolUseId": "call-city",
                    "input": '{"city":"Taipei"}',
                    "stop": True,
                },
                {
                    "name": "lookup_timezone",
                    "toolUseId": "call-timezone",
                    "input": '{"city":"Taipei"}',
                    "stop": True,
                },
                {
                    "name": "lookup_language",
                    "toolUseId": "call-language",
                    "input": '{"city":"Taipei"}',
                    "stop": True,
                },
            ],
            [{"content": "Both results received."}],
        ]
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    tools = [
        {
            "type": "function",
            "name": "lookup_city",
            "parameters": {"type": "object"},
        },
        {
            "type": "function",
            "name": "lookup_timezone",
            "parameters": {"type": "object"},
        },
        {
            "type": "function",
            "name": "lookup_language",
            "parameters": {"type": "object"},
        },
    ]

    first_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "Look these up.", "tools": tools},
    )
    assert first_response.status_code == 200
    calls = first_response.json()["output"]
    assert [item["call_id"] for item in calls] == [
        "call-city",
        "call-timezone",
        "call-language",
    ]
    assert [item["name"] for item in calls] == [
        "lookup_city",
        "lookup_timezone",
        "lookup_language",
    ]

    replay_input = [
        {"type": "message", "role": "user", "content": "Look these up."},
        {
            "type": "function_call",
            "call_id": "call-city",
            "name": "lookup_city",
            "arguments": '{"city":"Taipei"}',
        },
        {
            "type": "function_call",
            "call_id": "call-timezone",
            "name": "lookup_timezone",
            "arguments": '{"city":"Taipei"}',
        },
        {
            "type": "function_call",
            "call_id": "call-language",
            "name": "lookup_language",
            "arguments": '{"city":"Taipei"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-timezone",
            "output": "UTC+8",
        },
        {
            "type": "function_call_output",
            "call_id": "call-language",
            "output": "Mandarin",
        },
        {
            "type": "function_call_output",
            "call_id": "call-city",
            "output": "Taipei, Taiwan",
        },
    ]
    second_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": replay_input, "tools": tools},
    )

    assert second_response.status_code == 200
    replay_payload = transport.payloads[1]
    replay_current = replay_payload["conversationState"]["currentMessage"][
        "userInputMessage"
    ]
    replay_results = replay_current["userInputMessageContext"]["toolResults"]
    assert [item["toolUseId"] for item in replay_results] == [
        "call-timezone",
        "call-language",
        "call-city",
    ]


def test_responses_missing_kiro_call_ids_are_request_scoped_and_ordered(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """Missing upstream IDs receive unique IDs stable within one response only."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {"name": "run", "input": "{}", "stop": True},
                {"name": "run", "input": "{}", "stop": True},
            ],
            [{"name": "run", "input": "{}", "stop": True}],
        ]
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    request = {
        "model": "model",
        "input": "Run twice.",
        "tools": [{"type": "function", "name": "run", "parameters": {}}],
    }

    first_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json=request,
    )
    second_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json=request,
    )

    first_ids = [item["call_id"] for item in first_response.json()["output"]]
    second_ids = [item["call_id"] for item in second_response.json()["output"]]
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(first_ids) == 2
    assert len(set(first_ids)) == 2
    assert first_ids[0] != first_ids[1]
    assert set(first_ids).isdisjoint(second_ids)


def test_responses_text_and_tool_output_items_keep_stable_order(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """Mixed Kiro text and Tool Call output uses response array order."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {"content": "Before tool. "},
                {
                    "name": "run",
                    "toolUseId": "call-mixed",
                    "input": "{}",
                    "stop": True,
                },
                {"content": "After tool."},
            ]
        ]
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={
            "model": "model",
            "input": "Do it.",
            "tools": [{"type": "function", "name": "run", "parameters": {}}],
        },
    )

    assert response.status_code == 200
    output = response.json()["output"]
    assert [item["type"] for item in output] == ["message", "function_call"]
    assert output[0]["content"][0]["text"] == "Before tool. After tool."
    assert output[1]["call_id"] == "call-mixed"


def test_responses_rejects_duplicate_kiro_tool_call_ids_actionably(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """Duplicate upstream IDs never produce cross-wired Responses output."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {
                    "name": "first",
                    "toolUseId": "duplicate-call",
                    "input": "{}",
                    "stop": True,
                },
                {
                    "name": "second",
                    "toolUseId": "duplicate-call",
                    "input": "{}",
                    "stop": True,
                },
            ]
        ]
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={
            "model": "model",
            "input": "Run both.",
            "tools": [
                {"type": "function", "name": "first", "parameters": {}},
                {"type": "function", "name": "second", "parameters": {}},
            ],
        },
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "kiro_protocol_error"
    assert "duplicate" in response.json()["error"]["message"].lower()
