# -*- coding: utf-8 -*-

"""High-level, network-isolated Responses provider tests."""

import asyncio
import json
import struct
from typing import Any, AsyncIterator, Dict, List, Optional
import httpx
import pytest
import zlib
from unittest.mock import AsyncMock
from types import SimpleNamespace


REAL_ASYNC_CLIENT = httpx.AsyncClient


class StubKiroStream(httpx.AsyncByteStream):
    """Yield deliberately split AWS Event Stream frames from a stub."""

    def __init__(self, chunks: List[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class FailingKiroStream(httpx.AsyncByteStream):
    """Raise a transport error before the first upstream event."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise httpx.ReadError("prefetch failed")
        yield b""


class KiroResponsesTransport(httpx.AsyncBaseTransport):
    """Capture the Kiro payload and return isolated AWS Event Stream bytes."""

    def __init__(
        self,
        status_code: int = 200,
        error_body: bytes = b"",
        event_batches: Optional[List[List[dict]]] = None,
        trailing_bytes: bytes = b"",
        prefetch_failures: int = 0,
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
        self.trailing_bytes = trailing_bytes
        self.prefetch_failures = prefetch_failures

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.payload = json.loads(request.content)
        self.payloads.append(self.payload)
        if self.status_code != 200:
            return httpx.Response(
                self.status_code,
                request=request,
                content=self.error_body or b'{"message":"stub upstream failure"}',
            )
        if len(self.payloads) <= self.prefetch_failures:
            return httpx.Response(200, request=request, stream=FailingKiroStream())
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
        wire = b"".join(frames) + self.trailing_bytes
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


def _parse_response_sse(body: bytes) -> List[dict]:
    """Decode data-only Responses SSE events from an HTTP body."""
    events: List[dict] = []
    for record in body.decode("utf-8").split("\n\n"):
        for line in record.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


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


def test_responses_stream_emits_one_ordered_text_lifecycle(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """Streaming text exposes one complete Responses lifecycle."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {"content": "Hello "},
                {"content": "world"},
                {"usage": 1.2},
                {"contextUsagePercentage": 10.0},
            ]
        ]
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)

    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_response_sse(response.content)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))

    response_id = events[0]["response"]["id"]
    item_id = events[1]["item"]["id"]
    assert all(event.get("response", {}).get("id", response_id) == response_id for event in events)
    assert events[2]["item_id"] == item_id
    assert events[-2]["item"]["id"] == item_id
    assert events[-1]["response"]["id"] == response_id
    assert [events[3]["delta"], events[4]["delta"]] == ["Hello ", "world"]
    assert events[5]["text"] == "Hello world"
    assert events[-1]["response"]["status"] == "completed"
    assert events[-1]["response"]["usage"]["total_tokens"] >= 0


def test_responses_stream_emits_failed_terminal_after_started_stream(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """A parser failure after created becomes one failed terminal event."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[[{"content": "partial"}], [{"content": "retry"}]],
        trailing_bytes=b"\x00" * 12,
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    assert response.status_code == 200
    events = _parse_response_sse(response.content)
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.failed"
    assert [event["type"] for event in events].count("response.completed") == 0
    assert [event["type"] for event in events].count("response.failed") == 1
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert [event["delta"] for event in events if event["type"] == "response.output_text.delta"] == [
        "partial"
    ]
    assert "partial" not in events[-1]["response"]["error"]["message"]
    assert events[-1]["response"]["usage"]["total_tokens"] >= 0


def test_responses_stream_empty_upstream_still_has_one_terminal_lifecycle(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """An empty Kiro body completes without inventing an output item."""
    from main import app

    transport = KiroResponsesTransport(event_batches=[[]])

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    events = _parse_response_sse(response.content)
    assert response.status_code == 200
    assert [event["type"] for event in events] == [
        "response.created",
        "response.completed",
    ]
    assert events[-1]["response"]["output"] == []


def test_responses_stream_preserves_function_call_lifecycle_and_call_id(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """A streamed Client Tool Call retains its registered identity and arguments."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {
                    "name": "lookup",
                    "toolUseId": "call-stream-1",
                    "input": '{"city":"Taipei"}',
                    "stop": True,
                },
                {"usage": 1.0},
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
            "input": "Look it up",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object"},
                }
            ],
        },
    )

    events = _parse_response_sse(response.content)
    types = [event["type"] for event in events]
    assert response.status_code == 200
    assert types == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    item_id = events[1]["item"]["id"]
    assert events[1]["item"]["call_id"] == "call-stream-1"
    assert events[2]["item_id"] == item_id
    assert events[2]["delta"] == '{"city": "Taipei"}'
    assert events[3]["arguments"] == '{"city": "Taipei"}'
    assert events[4]["item"]["id"] == item_id
    assert events[4]["item"]["status"] == "completed"


def test_responses_stream_upstream_http_error_stays_an_ordinary_http_error(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """An upstream status failure is returned before response.created exists."""
    from main import app

    transport = KiroResponsesTransport(
        status_code=400,
        error_body=b'{"message":"bad request","reason":"INVALID_MODEL_ID"}',
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert "response.created" not in response.text


def test_responses_stream_retries_generation_before_first_event(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """A pre-event transport failure retries without exposing a partial lifecycle."""
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[[{"content": "after retry"}]],
        prefetch_failures=1,
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    events = _parse_response_sse(response.content)
    assert response.status_code == 200
    assert len(transport.payloads) == 2
    assert events[0]["type"] == "response.created"
    assert events[-1]["type"] == "response.completed"
    assert [event["delta"] for event in events if event["type"] == "response.output_text.delta"] == [
        "after retry"
    ]


def test_responses_stream_fails_over_only_before_first_event(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """Account failover happens during prefetch and never restarts an established stream."""
    from main import app
    from kiro.account_manager import Account
    import kiro.routes_responses as routes_module

    transport = KiroResponsesTransport(
        event_batches=[[{"content": "from second account"}]],
        prefetch_failures=1,
    )

    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(routes_module, "FIRST_TOKEN_MAX_RETRIES", 1)
    monkeypatch.setattr(app.state, "account_system", True)

    original_manager = app.state.account_manager
    original_account = next(iter(original_manager._accounts.values()))
    account_one = Account(
        id="stream-account-one",
        auth_manager=original_account.auth_manager,
        model_cache=original_account.model_cache,
        model_resolver=original_account.model_resolver,
    )
    account_two = Account(
        id="stream-account-two",
        auth_manager=original_account.auth_manager,
        model_cache=original_account.model_cache,
        model_resolver=original_account.model_resolver,
    )
    monkeypatch.setattr(
        original_manager,
        "_accounts",
        {account_one.id: account_one, account_two.id: account_two},
    )

    async def choose_account(request, model, exclude_accounts=None):
        """Select the second isolated account after the first prefetch fails."""
        return account_two if exclude_accounts else account_one

    monkeypatch.setattr(routes_module, "_select_account", choose_account)
    monkeypatch.setattr(original_manager, "report_failure", AsyncMock())
    monkeypatch.setattr(original_manager, "report_success", AsyncMock())

    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    events = _parse_response_sse(response.content)
    assert response.status_code == 200
    assert len(transport.payloads) == 2
    assert [event["type"] for event in events].count("response.created") == 1
    assert events[-1]["type"] == "response.completed"
    assert [event["delta"] for event in events if event["type"] == "response.output_text.delta"] == [
        "from second account"
    ]
    original_manager.report_failure.assert_awaited_once()
    original_manager.report_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_responses_stream_cancellation_closes_all_request_resources():
    """Client cancellation closes the parser, upstream response, and HTTP client."""
    from kiro.converters_responses import convert_responses_request
    from kiro.responses_streaming import ResponsesStreamState
    from kiro.models_responses import ResponsesRequest
    from kiro.routes_responses import _PreparedResponsesStream, _stream_responses_body

    request_data = ResponsesRequest(
        model="model",
        input="hello",
        stream=True,
    )
    request_ir = convert_responses_request(request_data)

    async def cancelled_parser() -> AsyncIterator[Any]:
        """Cancel before a second upstream event can be consumed."""
        raise asyncio.CancelledError()
        yield None

    close_http_client = AsyncMock()
    close_upstream = AsyncMock()
    manager = SimpleNamespace(
        report_success=AsyncMock(),
        report_failure=AsyncMock(),
    )
    fake_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(account_manager=manager)),
        is_disconnected=AsyncMock(return_value=False),
    )
    prepared = _PreparedResponsesStream(
        account=SimpleNamespace(id="account"),
        http_client=SimpleNamespace(close=close_http_client),
        upstream_response=SimpleNamespace(aclose=close_upstream),
        parsed_stream=cancelled_parser(),
        first_event=None,
        state=ResponsesStreamState(
            request=request_data,
            request_ir=request_ir,
            response_id="resp_cancel",
            model_cache=None,
        ),
    )
    body = _stream_responses_body(fake_request, prepared)

    created = await body.__anext__()
    assert "response.created" in created
    with pytest.raises(asyncio.CancelledError):
        await body.__anext__()

    close_http_client.assert_awaited_once()
    close_upstream.assert_awaited_once()
    manager.report_success.assert_not_awaited()
    manager.report_failure.assert_not_awaited()


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

    _patch_kiro_client(monkeypatch, transport)
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


def test_responses_stream_context_limit_is_ordinary_pre_stream_error(
    test_client,
    clean_app,
    valid_proxy_api_key,
    monkeypatch,
):
    """A context-limit rejection before the first event stays ordinary HTTP."""
    from main import app

    transport = KiroResponsesTransport(
        status_code=400,
        error_body=b'{"message":"Input is too long.","reason":"CONTENT_LENGTH_EXCEEDS_THRESHOLD"}',
    )
    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)

    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "stream": True},
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    message = response.json()["error"]["message"].lower()
    assert "context limit" in message
    assert "reduce" in message
    assert "response.created" not in response.text


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
            ,
            [{"content": "replayed"}],
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


def test_responses_high_level_seam_preserves_all_client_tool_types(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """Every supported Client Tool survives the isolated Kiro bridge explicitly.

    What it does: Sends mixed Client Tool definitions, calls, and results.
    Purpose: Verify the network seam preserves each registered Responses type.
    """
    print("Testing mixed Client Tool definitions, calls, and results")
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {
                    "name": "freeform",
                    "toolUseId": "call-custom",
                    "input": '{"input":"*** Begin Patch\\n*** End Patch"}',
                    "stop": True,
                },
                {
                    "name": "shell",
                    "toolUseId": "call-shell",
                    "input": '{"commands":["pwd"]}',
                    "stop": True,
                },
                {
                    "name": "local_shell",
                    "toolUseId": "call-local-shell",
                    "input": '{"command":["pwd"],"env":{}}',
                    "stop": True,
                },
                {
                    "name": "tool_search",
                    "toolUseId": "call-search",
                    "input": '{"query":"browser"}',
                    "stop": True,
                },
                {
                    "name": "apply_patch",
                    "toolUseId": "call-apply-patch",
                    "input": '{"type":"update_file","path":"a.txt","diff":"@@"}',
                    "stop": True,
                },
                {
                    "name": "lookup",
                    "toolUseId": "call-function",
                    "input": '{"city":"Taipei"}',
                    "stop": True,
                },
            ],
            [{"content": "replayed"}],
        ]
    )
    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)

    tools = [
        {"type": "custom", "name": "freeform"},
        {"type": "shell"},
        {"type": "local_shell"},
        {"type": "tool_search", "execution": "client"},
        {"type": "apply_patch"},
        {"type": "function", "name": "lookup", "parameters": {}},
    ]
    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "Use the tools.", "tools": tools},
    )

    assert response.status_code == 200
    output = response.json()["output"]
    assert [item["type"] for item in output] == [
        "custom_tool_call",
        "shell_call",
        "local_shell_call",
        "tool_search_call",
        "apply_patch_call",
        "function_call",
    ]
    assert output[0]["input"] == "*** Begin Patch\n*** End Patch"
    assert output[1]["action"] == {"commands": ["pwd"]}
    assert output[2]["action"] == {"command": ["pwd"], "env": {}}
    assert output[3]["arguments"] == {"query": "browser"}
    assert output[4]["operation"] == {
        "type": "update_file",
        "path": "a.txt",
        "diff": "@@",
    }
    assert [item["call_id"] for item in output] == [
        "call-custom",
        "call-shell",
        "call-local-shell",
        "call-search",
        "call-apply-patch",
        "call-function",
    ]

    payload_tools = transport.payloads[0]["conversationState"]["currentMessage"][
        "userInputMessage"
    ]["userInputMessageContext"]["tools"]
    assert [tool["toolSpecification"]["name"] for tool in payload_tools] == [
        "freeform",
        "shell",
        "local_shell",
        "tool_search",
        "apply_patch",
        "lookup",
    ]
    assert payload_tools[0]["toolSpecification"]["inputSchema"]["json"] == {
        "type": "object",
        "properties": {"input": {"type": "string"}},
        "required": ["input"],
    }

    replay_input = [
        {"type": "message", "role": "user", "content": "Use the tools."},
        *[
            {
                key: value
                for key, value in call.items()
                if key in {"type", "id", "call_id", "name", "input", "action", "arguments", "operation"}
            }
            for call in output
        ],
        {
            "type": "custom_tool_call_output",
            "call_id": "call-custom",
            "output": "custom result",
        },
        {
            "type": "shell_call_output",
            "call_id": "call-shell",
            "output": [{"stdout": "shell result", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}],
        },
        {
            "type": "local_shell_call_output",
            "call_id": "call-local-shell",
            "output": "local shell result",
        },
        {
            "type": "tool_search_output",
            "call_id": "call-search",
            "tools": [{"type": "function", "name": "browser"}],
        },
        {
            "type": "apply_patch_call_output",
            "call_id": "call-apply-patch",
            "output": "patch result",
        },
        {
            "type": "function_call_output",
            "call_id": "call-function",
            "output": "function result",
        },
    ]
    replay_response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": replay_input, "tools": tools},
    )
    assert replay_response.status_code == 200
    assert replay_response.json()["output"][0]["content"][0]["text"] == "replayed"

    replay_payload = transport.payloads[1]["conversationState"]
    replay_assistant = replay_payload["history"][1]["assistantResponseMessage"]
    assert [tool["toolUseId"] for tool in replay_assistant["toolUses"]] == [
        "call-custom",
        "call-shell",
        "call-local-shell",
        "call-search",
        "call-apply-patch",
        "call-function",
    ]
    replay_results = replay_payload["currentMessage"]["userInputMessage"][
        "userInputMessageContext"
    ]["toolResults"]
    assert [result["toolUseId"] for result in replay_results] == [
        "call-custom",
        "call-shell",
        "call-local-shell",
        "call-search",
        "call-apply-patch",
        "call-function",
    ]
    assert replay_results[3]["content"][0]["text"] == (
        '[{"type": "function", "name": "browser"}]'
    )


def test_responses_high_level_seam_replays_custom_type_and_call_id(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
) -> None:
    """Custom Tool Call and Result replay retain the registered type and identity.

    What it does: Replays a custom call and its raw-string result over HTTP.
    Purpose: Verify the original custom type and call ID survive both hops.
    """
    print("Testing custom Tool Call and Result replay")
    from main import app

    transport = KiroResponsesTransport(
        event_batches=[
            [
                {
                    "name": "freeform",
                    "toolUseId": "call-custom",
                    "input": '{"input":"raw command"}',
                    "stop": True,
                }
            ],
            [{"content": "accepted"}],
        ]
    )
    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)
    tool = {"type": "custom", "name": "freeform"}
    headers = {"Authorization": f"Bearer {valid_proxy_api_key}"}

    first_response = test_client.post(
        "/v1/responses",
        headers=headers,
        json={"model": "model", "input": "run it", "tools": [tool]},
    )
    assert first_response.status_code == 200
    first_call = first_response.json()["output"][0]
    assert first_call["type"] == "custom_tool_call"
    assert first_call["call_id"] == "call-custom"

    replay_response = test_client.post(
        "/v1/responses",
        headers=headers,
        json={
            "model": "model",
            "input": [
                {"type": "message", "role": "user", "content": "run it"},
                first_call,
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-custom",
                    "output": "raw result",
                },
            ],
            "tools": [tool],
        },
    )
    assert replay_response.status_code == 200
    assert replay_response.json()["output"][0]["content"][0]["text"] == "accepted"

    replay_payload = transport.payloads[1]["conversationState"]
    replay_assistant = replay_payload["history"][1]["assistantResponseMessage"]
    assert replay_assistant["toolUses"] == [
        {
            "name": "freeform",
            "input": {"input": "raw command"},
            "toolUseId": "call-custom",
        }
    ]
    replay_result = replay_payload["currentMessage"]["userInputMessage"][
        "userInputMessageContext"
    ]["toolResults"][0]
    assert replay_result["toolUseId"] == "call-custom"
    assert replay_result["content"] == [{"text": "raw result"}]


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search_preview"},
        {"type": "image_generation"},
        {"type": "mcp", "server_url": "https://example.test/mcp"},
        {"type": "file_search"},
        {"type": "computer_use"},
        {"type": "computer_use_preview"},
        {"type": "tool_search", "execution": "server"},
        {"type": "unknown_client_tool"},
    ],
)
def test_responses_high_level_seam_rejects_hosted_or_unknown_tools_before_kiro(
    test_client: Any,
    clean_app: Any,
    valid_proxy_api_key: str,
    monkeypatch: Any,
    tool: Dict[str, Any],
) -> None:
    """Rejected tool capabilities never reach the isolated Kiro transport.

    What it does: Sends Hosted and unknown tool definitions to the endpoint.
    Purpose: Verify unsupported capabilities fail before any upstream call.
    """
    print("Testing Hosted and unknown Client Tool rejection")
    from main import app

    transport = KiroResponsesTransport()
    _patch_kiro_client(monkeypatch, transport)
    monkeypatch.setattr(app.state, "account_system", False)

    response = test_client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {valid_proxy_api_key}"},
        json={"model": "model", "input": "hello", "tools": [tool]},
    )

    assert response.status_code == 400
    message = str(response.json()["detail"])
    if tool["type"] == "unknown_client_tool":
        assert "Supported Client Tool types" in message
    else:
        assert "not supported" in message
    assert transport.payloads == []
