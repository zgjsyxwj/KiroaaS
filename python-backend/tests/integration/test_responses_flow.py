# -*- coding: utf-8 -*-

"""High-level, network-isolated Responses provider tests."""

import json
import struct
from typing import AsyncIterator, List, Optional
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

    def __init__(self, status_code: int = 200, error_body: bytes = b"") -> None:
        self.payload: Optional[dict] = None
        self.status_code = status_code
        self.error_body = error_body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.payload = json.loads(request.content)
        if self.status_code != 200:
            return httpx.Response(
                self.status_code,
                request=request,
                content=self.error_body or b'{"message":"stub upstream failure"}',
            )
        frames = [
            _aws_event_frame(event)
            for event in (
                {"content": "Hello "},
                {"content": "world"},
                {"content": "你好"},
                {"usage": 1.2},
                {"contextUsagePercentage": 10.0},
            )
        ]
        wire = b"".join(frames)
        utf8_start = wire.index("你好".encode("utf-8"))
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
