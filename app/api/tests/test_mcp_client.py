"""Tests for HttpxMcpConnector (Phase 12A) via httpx.MockTransport.

Exercises the MCP Streamable-HTTP discovery handshake (initialize ->
notifications/initialized -> tools/list) without a live server: JSON and SSE
response decoding, auth-header shaping, the tool cap, and error mapping.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai4ia_api.agents.mcp_client import HttpxMcpConnector, McpAuth
from ai4ia_api.agents.mcp_servers import (
    MAX_TOOLS_PER_SERVER,
    McpAuthMode,
    McpConnectionError,
)

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


def _connector(handler) -> HttpxMcpConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxMcpConnector(client=client)


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
