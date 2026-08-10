"""Tests for HttpxMcpConnector via httpx.MockTransport.

Exercises the MCP Streamable-HTTP discovery handshake (initialize ->
notifications/initialized -> tools/list) without a live server: JSON and SSE
response decoding, auth-header shaping, the tool cap, and error mapping.
"""
from __future__ import annotations

import ipaddress
import json
from collections.abc import AsyncIterator

import httpcore
import httpx
import pytest

from ai4ia_api.agents.mcp_client import (
    HttpxMcpConnector,
    McpAuth,
    _PinnedHttpsTransport,
)
from ai4ia_api.agents.mcp_servers import (
    MAX_TOOL_NAME_LEN,
    MAX_TOOLS_PER_SERVER,
    McpAuthMode,
    McpConnectionError,
)
from ai4ia_api.agents.ssrf import SsrfError, validate_public_https_url

_ENDPOINT = "https://mcp.example.com/rpc"


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _json(body: dict, *, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


def _init_result() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
    }


def _tools_result(tools: list[dict]) -> dict:
    return {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}


def _call_result(content: list[dict], *, is_error: bool = False) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"content": content, "isError": is_error},
    }


def _connector(handler) -> HttpxMcpConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxMcpConnector(client=client)


def _connector_maxbytes(handler, max_bytes: int) -> HttpxMcpConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxMcpConnector(client=client, max_bytes=max_bytes)


async def test_discovers_tools_over_json():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        seen.append(method)
        if method == "initialize":
            return _json(_init_result(), headers={"Mcp-Session-Id": "sess-1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            # The session id from initialize must be echoed back.
            assert request.headers.get("Mcp-Session-Id") == "sess-1"
            return _json(
                _tools_result(
                    [
                        {"name": "get_forecast", "description": "Forecast"},
                        {"name": "get_alerts", "description": "Alerts"},
                    ]
                )
            )
        raise AssertionError(f"unexpected method {method}")

    tools = await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())
    assert [t.name for t in tools] == ["get_forecast", "get_alerts"]
    assert seen == ["initialize", "notifications/initialized", "tools/list"]


async def test_discovers_tools_over_sse():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            payload = json.dumps(_init_result())
            return httpx.Response(
                200,
                content=f"event: message\ndata: {payload}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            payload = json.dumps(_tools_result([{"name": "only_tool"}]))
            return httpx.Response(
                200,
                content=f"event: message\ndata: {payload}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(method)

    tools = await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())
    assert [t.name for t in tools] == ["only_tool"]


@pytest.mark.parametrize(
    "auth,header,value",
    [
        (McpAuth(mode=McpAuthMode.bearer, secret="tok"), "authorization", "Bearer tok"),
        (McpAuth(mode=McpAuthMode.api_key, secret="key"), "x-api-key", "key"),
    ],
)
async def test_sends_auth_headers(auth, header, value):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        captured[header] = request.headers.get(header, "")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_tools_result([{"name": "t"}]))

    await _connector(handler).discover(endpoint=_ENDPOINT, auth=auth)
    assert captured[header] == value


async def test_http_error_raises_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(McpConnectionError):
        await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())


async def test_jsonrpc_error_raises_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32600, "message": "bad request"},
                }
            )
        return httpx.Response(202)

    with pytest.raises(McpConnectionError):
        await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())


async def test_tools_are_capped():
    overflow = [{"name": f"tool_{i}"} for i in range(MAX_TOOLS_PER_SERVER + 10)]

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_tools_result(overflow))

    tools = await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())
    assert len(tools) == MAX_TOOLS_PER_SERVER


async def test_discover_drops_tool_names_containing_control_characters():
    # A remote server could name a tool with an embedded newline/CR/NUL to try to
    # forge log lines or downstream structured telemetry. Discovery must drop it
    # rather than surfacing it for later use as a dispatch/log identifier.
    tools = [
        {"name": "get_forecast", "description": "Forecast"},
        {"name": "evil\nSpoofedEvent=admin_login outcome=ok", "description": "hostile"},
        {"name": "evil\rcarriage", "description": "hostile"},
        {"name": "evil\x00nul", "description": "hostile"},
        {"name": "   ", "description": "blank after strip"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_tools_result(tools))

    discovered = await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())
    assert [t.name for t in discovered] == ["get_forecast"]


async def test_discover_keeps_tool_names_with_dots_slashes_and_unicode():
    # Discovery is deliberately loose about the *charset* (only control chars are
    # rejected) so legitimate real-world tool names are not silently dropped; the
    # provider-safe charset guarantee comes from the alias, not from filtering here.
    tools = [
        {"name": "weather.get_forecast", "description": "dotted"},
        {"name": "ns/tool", "description": "slashed"},
        {"name": "获取天气", "description": "unicode"},
        {"name": "get forecast", "description": "spaced"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_tools_result(tools))

    discovered = await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())
    assert [t.name for t in discovered] == [
        "weather.get_forecast",
        "ns/tool",
        "获取天气",
        "get forecast",
    ]
    assert [t.raw_name for t in discovered] == [t.name for t in discovered]


def test_parse_tools_preserves_exact_raw_and_drops_each_invalid_name():
    accepted = "  weather.get/forecast 获取  "
    payload = _tools_result(
        [
            {"name": "good"},
            {"name": "bad\nname"},
            {"name": "bad\u200bformat"},
            {"name": "\ud800"},
            {"name": "x" * (MAX_TOOL_NAME_LEN + 1)},
            {"name": accepted},
            {"name": "good"},  # duplicate is rejected, not overwritten
        ]
    )

    tools = HttpxMcpConnector._parse_tools(payload)

    assert [tool.name for tool in tools] == ["good", accepted]
    assert tools[1].rawName == accepted
    assert tools[1].raw_name == accepted


async def test_malformed_tools_result_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json({"jsonrpc": "2.0", "id": 2, "result": {"not_tools": []}})

    with pytest.raises(McpConnectionError):
        await _connector(handler).discover(endpoint=_ENDPOINT, auth=McpAuth())


# --- Execution: tools/call ----------------------------------------------------


async def test_call_tool_returns_flattened_text_content():
    seen: list[str] = []
    captured_params: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        seen.append(method)
        if method == "initialize":
            return _json(_init_result(), headers={"Mcp-Session-Id": "s1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            assert request.headers.get("Mcp-Session-Id") == "s1"
            captured_params.update(body.get("params") or {})
            return _json(
                _call_result(
                    [
                        {"type": "text", "text": "Sunny, 72F"},
                        {"type": "text", "text": "No alerts"},
                    ]
                )
            )
        raise AssertionError(f"unexpected method {method}")

    result = await _connector(handler).call_tool(
        endpoint=_ENDPOINT, auth=McpAuth(), tool="get_forecast", arguments={"city": "SEA"}
    )
    assert result.content == "Sunny, 72F\nNo alerts"
    assert result.is_error is False
    assert seen == ["initialize", "notifications/initialized", "tools/call"]
    # The tools/call request carried the MCP {name, arguments} envelope.
    assert captured_params == {"name": "get_forecast", "arguments": {"city": "SEA"}}


async def test_call_tool_non_text_block_is_noted_by_type():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(
            _call_result(
                [{"type": "text", "text": "see image"}, {"type": "image", "data": "b64"}]
            )
        )

    result = await _connector(handler).call_tool(
        endpoint=_ENDPOINT, auth=McpAuth(), tool="render", arguments={}
    )
    # Binary/non-text blocks are noted by type, never inlined.
    assert result.content == "see image\n[image content]"
    assert result.is_error is False


async def test_call_tool_surfaces_is_error():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(
            _call_result([{"type": "text", "text": "boom"}], is_error=True)
        )

    result = await _connector(handler).call_tool(
        endpoint=_ENDPOINT, auth=McpAuth(), tool="explode", arguments={}
    )
    assert result.is_error is True
    assert result.content == "boom"


async def test_call_tool_over_sse():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            payload = json.dumps(_init_result())
            return httpx.Response(
                200,
                content=f"event: message\ndata: {payload}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        payload = json.dumps(_call_result([{"type": "text", "text": "streamed"}]))
        return httpx.Response(
            200,
            content=f"event: message\ndata: {payload}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    result = await _connector(handler).call_tool(
        endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
    )
    assert result.content == "streamed"
    assert result.is_error is False


async def test_call_tool_oversized_response_raises():
    big = "x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_call_result([{"type": "text", "text": big}]))

    # max_bytes large enough for the tiny initialize body, small enough that the
    # tools/call response trips the bounded-size guard.
    with pytest.raises(McpConnectionError):
        await _connector_maxbytes(handler, 1000).call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
        )


async def test_call_tool_stream_stops_reading_immediately_after_incremental_cap():
    big = json.dumps(_call_result([{"type": "text", "text": "x" * 4000}])).encode()
    chunks = [big[index : index + 128] for index in range(0, len(big), 128)]
    stream = _TrackingStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, stream=stream)

    with pytest.raises(McpConnectionError, match="response too large"):
        await _connector_maxbytes(handler, 512).call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
        )

    assert stream.reads == 5
    assert stream.reads < len(chunks)
    assert stream.closed is True


async def test_call_tool_rejects_declared_oversize_without_reading_body():
    stream = _TrackingStream([b"must not be read"])

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200, headers={"content-length": "513"}, stream=stream
        )

    with pytest.raises(McpConnectionError, match="response too large"):
        await _connector_maxbytes(handler, 512).call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
        )

    assert stream.reads == 0
    assert stream.closed is True


async def test_call_tool_accepts_response_at_exact_streaming_boundary():
    raw = json.dumps(_call_result([{"type": "text", "text": "boundary"}])).encode()
    stream = _TrackingStream([raw[:10], raw[10:]])

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200, headers={"content-length": str(len(raw))}, stream=stream
        )

    result = await _connector_maxbytes(handler, len(raw)).call_tool(
        endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
    )

    assert result.content == "boundary"
    assert stream.reads == 2
    assert stream.closed is True


async def test_call_tool_jsonrpc_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "no such tool"}}
        )

    with pytest.raises(McpConnectionError):
        await _connector(handler).call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="missing", arguments={}
        )


async def test_call_tool_sends_auth_header():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        captured["authorization"] = request.headers.get("authorization", "")
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_call_result([{"type": "text", "text": "ok"}]))

    await _connector(handler).call_tool(
        endpoint=_ENDPOINT,
        auth=McpAuth(mode=McpAuthMode.bearer, secret="tok"),
        tool="t",
        arguments={},
    )
    assert captured["authorization"] == "Bearer tok"


# --- Transport-owned IP pinning ----------------------------------------------


class _SpyTransport(httpx.AsyncBaseTransport):
    """Records the request it receives and returns a canned 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None


class _CapturingBackend(httpcore.AsyncNetworkBackend):
    """An httpcore network backend that records the host of every TCP connect.

    Injected into a *real* ``httpx.AsyncHTTPTransport`` pool so a test can observe
    the exact host handed to the OS-level ``connect_tcp`` — i.e. the socket the
    process would actually open — without any real network. The connect is aborted
    right after the host is captured (before TLS), so nothing leaves the box.
    """

    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(  # type: ignore[override]
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        self.hosts.append(host)
        raise httpcore.ConnectError("captured at socket layer; not really connecting")

    async def connect_unix_socket(  # pragma: no cover - never used
        self, path, timeout=None, local_address=None, socket_options=None
    ):
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:  # pragma: no cover
        return None


def _real_inner_with_backend(backend: _CapturingBackend) -> httpx.AsyncHTTPTransport:
    inner = httpx.AsyncHTTPTransport()
    # Swap the pool's network backend so connect_tcp is observable but inert.
    inner._pool._network_backend = backend  # noqa: SLF001 - test seam
    return inner


class _MockInnerConnector(HttpxMcpConnector):
    """Connector that keeps the real resolve+pin but mocks the inner transport.

    Exercises the own-client path (single resolve->validate->pin) against canned
    MCP responses, so a test can assert the pin was resolved exactly once and that
    every request in the session was rewritten to the pinned IP — without a socket.
    """

    def __init__(self, handler, **kwargs) -> None:
        super().__init__(**kwargs)
        self._handler = handler

    def _new_client(self, pinned_ip: str) -> httpx.AsyncClient:  # type: ignore[override]
        transport = _PinnedHttpsTransport(
            pinned_ip, inner=httpx.MockTransport(self._handler)
        )
        return httpx.AsyncClient(
            timeout=self._timeout_s, follow_redirects=False, transport=transport
        )


async def test_pinned_transport_rewrites_to_ip_preserving_host_and_sni():
    spy = _SpyTransport()
    transport = _PinnedHttpsTransport("93.184.216.34", inner=spy)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(_ENDPOINT, json={"hi": 1})

    assert resp.status_code == 200
    sent = spy.requests[0]
    # The socket connects to the pinned IP, but the Host header and TLS SNI stay
    # bound to the real hostname so cert verification still works.
    assert sent.url.host == "93.184.216.34"
    assert sent.headers.get("Host") == "mcp.example.com"
    assert sent.extensions.get("sni_hostname") == "mcp.example.com"
    # The host handed downstream is a *literal IP*, so no hostname survives to the
    # connect layer for httpx to re-resolve (the pin is real, not a pre-flight check).
    ipaddress.ip_address(sent.url.host)  # raises if it were still a hostname
    assert sent.url.host != "mcp.example.com"


async def test_pinned_transport_tcp_connects_to_pinned_ip_not_hostname():
    # Drive a real httpx.AsyncHTTPTransport so the assertion is at the actual
    # connect_tcp boundary: the OS-level connect must target the pinned IP.
    backend = _CapturingBackend()
    transport = _PinnedHttpsTransport(
        "93.184.216.34", inner=_real_inner_with_backend(backend)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_ENDPOINT, json={})
    # httpx had no hostname left to resolve at connect time — it dialed the IP.
    assert backend.hosts == ["93.184.216.34"]


async def test_pinned_transport_rejects_non_https():
    spy = _SpyTransport()
    transport = _PinnedHttpsTransport("93.184.216.34", inner=spy)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SsrfError):  # transport refuses a non-https target
            await client.post("http://mcp.example.com/rpc", json={})
    assert spy.requests == []


async def test_call_tool_resolves_once_and_pins_every_request():
    # Fail-closed single resolution: the host is resolved+validated exactly once for
    # the whole session (initialize + notify + tools/call), and every request is
    # rewritten to that one pinned IP — no per-request re-resolution to rebind.
    calls = {"n": 0}

    def counting_public(_host: str) -> list[str]:
        calls["n"] += 1
        return ["93.184.216.34"]

    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        method = json.loads(request.content)["method"]
        if method == "initialize":
            return _json(_init_result())
        if method == "notifications/initialized":
            return httpx.Response(202)
        return _json(_call_result([{"type": "text", "text": "ok"}]))

    connector = _MockInnerConnector(handler, resolver=counting_public)
    result = await connector.call_tool(
        endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
    )

    assert result.content == "ok"
    # One resolve+validate for the entire session, not a fresh lookup per request.
    assert calls["n"] == 1
    # Every request was pinned to the validated IP (no hostname reached connect).
    assert set(seen_hosts) == {"93.184.216.34"}


async def test_call_tool_rebind_rejected_before_client_built():
    # The hard rebind: PUBLIC at validate time, PRIVATE at the connector's single pin
    # resolution -> rejected before any client (hence any socket) is constructed.
    calls = {"n": 0}

    def rebinding(_host: str) -> list[str]:
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    # An up-front validate (resolution #1) sees the public address and passes.
    assert validate_public_https_url(_ENDPOINT, resolver=rebinding) == "mcp.example.com"

    built = {"n": 0}

    class _NoBuild(HttpxMcpConnector):
        def _new_client(self, pinned_ip: str) -> httpx.AsyncClient:  # type: ignore[override]
            built["n"] += 1
            raise AssertionError("client must not be built once the pin rejects")

    connector = _NoBuild(resolver=rebinding)
    with pytest.raises(McpConnectionError, match="permitted egress"):
        await connector.call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
        )
    # The connector's single pin resolution (#2) returned a private address and was
    # rejected; no client (hence no socket) was ever constructed.
    assert calls["n"] == 2
    assert built["n"] == 0


async def test_call_tool_rejects_dns_rebind_at_socket_layer():
    # End-to-end through the connector on its REAL own-client path (real
    # AsyncHTTPTransport): same rebind, surfaced as McpConnectionError. Because the
    # connector resolves+validates ONCE at session setup and would pin the socket to
    # that IP, a private result there is refused before any bytes leave the process.
    calls = {"n": 0}

    def rebinding_resolver(_host: str) -> list[str]:
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    # Up-front host validation passes: the name is public at validate time.
    assert (
        validate_public_https_url(_ENDPOINT, resolver=rebinding_resolver)
        == "mcp.example.com"
    )

    connector = HttpxMcpConnector(resolver=rebinding_resolver)
    with pytest.raises(McpConnectionError, match="permitted egress"):
        await connector.call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
        )
    # validate consumed resolution #1; the connector's single pin resolve was #2 and
    # was rejected before a socket opened (no further resolutions).
    assert calls["n"] == 2
