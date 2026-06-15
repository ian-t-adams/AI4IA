"""Tests for HttpxMcpConnector (Phase 12A) via httpx.MockTransport.

Exercises the MCP Streamable-HTTP discovery handshake (initialize ->
notifications/initialized -> tools/list) without a live server: JSON and SSE
response decoding, auth-header shaping, the tool cap, and error mapping.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai4ia_api.agents.mcp_client import (
    HttpxMcpConnector,
    McpAuth,
    _PinnedHttpsTransport,
)
from ai4ia_api.agents.mcp_servers import (
    MAX_TOOLS_PER_SERVER,
    McpAuthMode,
    McpConnectionError,
)
from ai4ia_api.agents.ssrf import validate_public_https_url

_ENDPOINT = "https://mcp.example.com/rpc"


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


# --- Execution: tools/call (Phase 12B Increment B) ---------------------------


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


# --- Transport-owned IP pinning (Phase 12B Increment B) ----------------------


class _SpyTransport(httpx.AsyncBaseTransport):
    """Records the request it receives and returns a canned 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None


async def test_pinned_transport_rewrites_to_ip_preserving_host_and_sni():
    spy = _SpyTransport()
    transport = _PinnedHttpsTransport(_only_resolver(["93.184.216.34"]), inner=spy)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.post(_ENDPOINT, json={"hi": 1})

    assert resp.status_code == 200
    sent = spy.requests[0]
    # The socket connects to the validated IP, but the Host header and TLS SNI
    # stay bound to the real hostname so cert verification still works.
    assert sent.url.host == "93.184.216.34"
    assert sent.headers.get("Host") == "mcp.example.com"
    assert sent.extensions.get("sni_hostname") == "mcp.example.com"


async def test_pinned_transport_rejects_non_https():
    spy = _SpyTransport()
    transport = _PinnedHttpsTransport(_only_resolver(["93.184.216.34"]), inner=spy)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(Exception):  # SsrfError propagates through the client
            await client.post("http://mcp.example.com/rpc", json={})
    assert spy.requests == []


async def test_call_tool_rejects_dns_rebind_at_socket_layer():
    # A resolver that is public the first time (so an up-front validate passes)
    # then private on the next resolution — a classic DNS rebind between validate
    # and connect.
    calls = {"n": 0}

    def rebinding_resolver(_host: str) -> list[str]:
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] == 1 else ["127.0.0.1"]

    # Up-front host validation passes: the name is public at validate time.
    assert (
        validate_public_https_url(_ENDPOINT, resolver=rebinding_resolver)
        == "mcp.example.com"
    )

    # The connector uses its OWN client (no injected transport), so the
    # transport-owned guard re-resolves at connect time and refuses the now-private
    # address before any bytes leave the process.
    connector = HttpxMcpConnector(resolver=rebinding_resolver)
    with pytest.raises(McpConnectionError, match="permitted egress"):
        await connector.call_tool(
            endpoint=_ENDPOINT, auth=McpAuth(), tool="t", arguments={}
        )
    # validate consumed resolution #1; the connect-time re-resolve was #2 and was
    # rejected before a socket opened (no further resolutions).
    assert calls["n"] == 2


def _only_resolver(addr: list[str]):
    return lambda _host: list(addr)
