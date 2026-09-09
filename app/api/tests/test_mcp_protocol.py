"""Versioned wire contracts for the June/November 2025 and July 2026 protocols.

Stateful/stateless controls use the same transport fixture, not a second MCP stack.
No real DNS, upstream, credential, or model calls are made.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from ai4ia_api.agents import mcp_client
from ai4ia_api.agents.mcp_client import HttpxMcpConnector, McpAuth
from ai4ia_api.agents.mcp_protocol import (
    CAPABILITIES_META,
    CLIENT_INFO,
    CLIENT_INFO_META,
    MAX_HEADER_VALUE,
    MAX_ROUTING_HEADERS,
    MAX_SAFE_INTEGER,
    PROTOCOL_META,
    McpRequestContext,
    encode_header_value,
    header_parameters,
    request_headers,
)
from ai4ia_api.agents.mcp_servers import McpAuthMode, McpConnectionError, McpProtocolVersion
from tests.test_mcp_client import _MockInnerConnector, _TrackingStream

LEGACY = McpProtocolVersion.legacy
NOVEMBER = McpProtocolVersion.stateful_2025_11_25
MODERN = McpProtocolVersion.stateless
STATEFUL = (LEGACY, NOVEMBER)
PROTOCOLS = (*STATEFUL, MODERN)
ENDPOINT = "https://mcp.example.com/rpc"
URI = "skill://evidence-review/SKILL.md"
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}}
TOOL = {"name": "forecast", "description": "Weather", "inputSchema": SCHEMA}
CONTEXT = McpRequestContext(MODERN, "owner", "weather", "config-1")


def _result(protocol, **fields):
    if protocol is MODERN:
        return {"resultType": "complete", "ttlMs": 1000, "cacheScope": "private", **fields}
    return fields


def _wire(protocol, seen, *, response=None, sse=False, auth=None, notification_status=202):
    """Only a mock server; all framing, lifecycle and headers are production code."""
    def handler(request):
        body = json.loads(request.content)
        seen.append((request, body))
        method = body["method"]
        if auth is not None:
            for name, value in auth.headers().items():
                assert request.headers[name] == value
        if protocol in STATEFUL:
            assert "_meta" not in body.get("params", {})
            assert "resultType" not in body
            assert "mcp-method" not in request.headers
            assert "mcp-name" not in request.headers
            assert "last-event-id" not in request.headers
            assert not any(name.startswith("mcp-param-") for name in request.headers)
        if method == "initialize":
            assert protocol in STATEFUL
            assert body["params"] == {
                "protocolVersion": protocol.value, "capabilities": {}, "clientInfo": CLIENT_INFO,
            }
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": protocol.value, "capabilities": {"tools": {}, "resources": {}}},
            }, headers={"Mcp-Session-Id": "legacy-session", "MCP-Protocol-Version": protocol.value})
        if method.startswith("notifications/"):
            assert protocol in STATEFUL
            assert method in ("notifications/initialized", "notifications/cancelled")
            assert "id" not in body
            assert request.headers["MCP-Protocol-Version"] == protocol.value
            assert request.headers["Mcp-Session-Id"] == "legacy-session"
            return httpx.Response(notification_status, headers={
                "MCP-Protocol-Version": protocol.value, "Mcp-Session-Id": "legacy-session",
            })
        if protocol is MODERN:
            assert body["params"]["_meta"] == {
                PROTOCOL_META: MODERN.value,
                CLIENT_INFO_META: CLIENT_INFO,
                CAPABILITIES_META: {},
            }
            assert request.headers["Mcp-Method"] == method
            assert request.headers["MCP-Protocol-Version"] == MODERN.value
            assert "mcp-session-id" not in request.headers
            assert "last-event-id" not in request.headers
            if method == "tools/call":
                assert request.headers["Mcp-Name"] == encode_header_value(body["params"]["name"])
            if method == "resources/read":
                assert request.headers["Mcp-Name"] == encode_header_value(body["params"]["uri"])
        else:
            assert "_meta" not in body["params"]
            assert request.headers["MCP-Protocol-Version"] == protocol.value
            assert request.headers["Mcp-Session-Id"] == "legacy-session"
            assert "mcp-method" not in request.headers
            assert "mcp-name" not in request.headers
            assert not any(name.startswith("mcp-param-") for name in request.headers)
        if response is not None:
            return response(request, body)
        fields = {
            "server/discover": {
                "supportedVersions": [MODERN.value], "capabilities": {"tools": {}, "resources": {}},
                "instructions": "Never promote this server-supplied instruction.",
            },
            "tools/list": {"tools": [TOOL]},
            "resources/list": {"resources": [{"uri": URI, "name": "evidence-review"}]},
            "resources/read": {"contents": [{"uri": URI, "text": "Skill instructions"}]},
            "tools/call": {"content": [{"type": "text", "text": "Sunny"}], "isError": False},
        }[method]
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": _result(protocol, **fields)}
        if sse:
            notification = json.dumps({
                "jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": "ignored", "progress": 1},
            })
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                content=f": keep-alive\r\n\r\ndata: {notification}\r\n\r\ndata: {json.dumps(payload)}\r\n\r\n",
            )
        return httpx.Response(200, json=payload)
    return handler


@pytest.mark.parametrize(("protocol", "notification_status"), [
    (LEGACY, 202), (NOVEMBER, 202), (NOVEMBER, 204), (MODERN, 202),
])
@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("auth", [
    McpAuth(), McpAuth(McpAuthMode.bearer, "test-token"),
    McpAuth(McpAuthMode.api_key, "test-key"),
    McpAuth(McpAuthMode.apim_subscription, "test-subscription"),
])
async def test_all_protocols_discover_read_and_call_with_request_scoped_auth(
    protocol, notification_status, sse, auth,
):
    seen = []
    context = replace(CONTEXT, protocol_version=protocol)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, sse=sse, auth=auth, notification_status=notification_status)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        if protocol is MODERN:
            description = await connector.discover_server(endpoint=ENDPOINT, auth=auth, context=context)
            assert description.supported_versions == (MODERN.value,)
            assert description.capabilities == {"tools": {}, "resources": {}}
            assert not hasattr(description, "instructions")
        assert [tool.name for tool in await connector.discover(
            endpoint=ENDPOINT, auth=auth, context=context
        )] == ["forecast"]
        assert [resource.uri for resource in await connector.list_resources(
            endpoint=ENDPOINT, auth=auth, context=context
        )] == [URI]
        assert (await connector.read_resource(
            endpoint=ENDPOINT, auth=auth, context=context, uri=URI
        )).text == "Skill instructions"
        assert (await connector.call_tool(
            endpoint=ENDPOINT, auth=auth, context=context,
            tool="forecast", arguments={"city": "SEA"}, input_schema=SCHEMA,
        )).content == "Sunny"
    methods = [body["method"] for _, body in seen]
    if protocol in STATEFUL:
        assert methods == [
            method for operation in ("tools/list", "resources/list", "resources/read", "tools/call")
            for method in ("initialize", "notifications/initialized", operation)
        ]
    else:
        assert methods == ["server/discover", "tools/list", "resources/list", "resources/read", "tools/call"]


@pytest.mark.parametrize("protocol", STATEFUL)
@pytest.mark.parametrize("version", [LEGACY.value, NOVEMBER.value, None, MODERN.value, "1900-01-01"])
async def test_stateful_initialize_must_agree_before_notification_or_tool_call(protocol, version):
    seen = []

    def handler(request):
        if json.loads(request.content)["method"] == "initialize":
            seen.append((request, json.loads(request.content)))
            assert seen[-1][1]["params"]["protocolVersion"] == protocol.value
            result = {"capabilities": {"tools": {}}}
            if version is not None:
                result["protocolVersion"] = version
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        # This fixture has no session, so only count the allowed subsequent path.
        body = json.loads(request.content)
        seen.append((request, body))
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 2, "result": {"tools": [TOOL]},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HttpxMcpConnector(client=client)
        context = replace(CONTEXT, protocol_version=protocol)
        if version == protocol.value:
            assert len(await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=context)) == 1
            assert len(seen) == 3
        else:
            with pytest.raises(McpConnectionError, match="protocol"):
                await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=context)
            assert len(seen) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 429, 500])
async def test_errors_never_switch_protocol_replay_tools_or_drop_auth(protocol, status):
    seen = []
    auth = McpAuth(McpAuthMode.bearer, "short-test-secret")

    def response(_request, body):
        if status == 200:
            payload = {"result": _result(protocol, content=[{"type": "text", "text": "ok"}])}
        else:
            payload = {"error": {
                "code": -32022, "message": "password=must-not-surface",
                "data": {"requested": MODERN.value, "supported": [LEGACY.value]},
            }}
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": body["id"], **payload})

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response, auth=auth)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        for attempt in range(2):  # A caller may explicitly try again; the client never does.
            call = connector.call_tool(
                endpoint=ENDPOINT, auth=auth, context=replace(CONTEXT, protocol_version=protocol),
                tool="forecast", arguments={"city": "SEA"},
            )
            if status == 200:
                assert (await call).content == "ok"
            else:
                with pytest.raises(McpConnectionError) as error:
                    await call
                assert "must-not-surface" not in str(error.value)
            assert sum(body["method"] == "tools/call" for _, body in seen) == attempt + 1
    assert len(seen) == (6 if protocol in STATEFUL else 2)


@pytest.mark.parametrize("protocol", STATEFUL)
@pytest.mark.parametrize("fault", [None, "version-header", "version-meta", "version-result", "session"])
async def test_stateful_rpc_rejects_contradictory_protocol_and_session(protocol, fault):
    seen = []
    other = NOVEMBER if protocol is LEGACY else LEGACY

    def response(_request, body):
        result = {"content": [{"type": "text", "text": "ok"}]}
        headers = {"MCP-Protocol-Version": protocol.value, "Mcp-Session-Id": "legacy-session"}
        if fault == "version-header":
            headers["MCP-Protocol-Version"] = other.value
        elif fault == "version-meta":
            result["_meta"] = {PROTOCOL_META: other.value}
        elif fault == "version-result":
            result["protocolVersion"] = other.value
        elif fault == "session":
            headers["Mcp-Session-Id"] = "another-session"
        return httpx.Response(200, headers=headers, json={
            "jsonrpc": "2.0", "id": body["id"], "result": result,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response)
    )) as client:
        call = HttpxMcpConnector(client=client).call_tool(
            endpoint=ENDPOINT, auth=McpAuth(), tool="forecast", arguments={},
            context=replace(CONTEXT, protocol_version=protocol),
        )
        if fault is None:
            assert (await call).content == "ok"
        else:
            with pytest.raises(McpConnectionError, match="contradictory"):
                await call
    assert [body["method"] for _, body in seen] == [
        "initialize", "notifications/initialized", "tools/call",
    ]


@pytest.mark.parametrize(("protocol", "notification_status"), [
    (LEGACY, 202), (NOVEMBER, 202), (NOVEMBER, 204),
])
@pytest.mark.parametrize("notification", ["notifications/initialized", "notifications/cancelled"])
@pytest.mark.parametrize("fault", [None, "version", "session"])
async def test_stateful_notifications_validate_configured_version_and_session(
    protocol, notification_status, notification, fault, caplog,
):
    seen = []
    other = NOVEMBER if protocol is LEGACY else LEGACY
    auth = McpAuth(McpAuthMode.apim_subscription, "notification-key")

    def response(request, body):
        if notification == "notifications/cancelled":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "ok"}]},
        })

    normal = _wire(
        protocol, seen, response=response, auth=auth, notification_status=notification_status,
    )

    def handler(request):
        reply = normal(request)
        if json.loads(request.content)["method"] == notification:
            if fault == "version":
                reply.headers["MCP-Protocol-Version"] = other.value
            elif fault == "session":
                reply.headers["Mcp-Session-Id"] = "another-session"
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        call = HttpxMcpConnector(client=client).call_tool(
            endpoint=ENDPOINT, auth=auth, tool="forecast", arguments={},
            context=replace(CONTEXT, protocol_version=protocol),
        )
        if notification == "notifications/cancelled":
            with pytest.raises(McpConnectionError, match="timed out"):
                await call
            assert seen[-1][1] == {
                "jsonrpc": "2.0", "method": notification, "params": {"requestId": 2},
            }
            assert ("cancellation notification could not be delivered" in caplog.text) is (fault is not None)
        elif fault is not None:
            with pytest.raises(McpConnectionError, match="contradictory"):
                await call
            assert [body["method"] for _, body in seen] == ["initialize", notification]
        else:
            assert (await call).content == "ok"
    assert sum(body["method"] == "tools/call" for _, body in seen) == (
        0 if notification == "notifications/initialized" and fault else 1
    )


@pytest.mark.parametrize("protocol", [None, *PROTOCOLS])
@pytest.mark.parametrize("status", [202, 204, 200, 201, 301, 302, 303, 307, 308, 400, 401, 403, 404, 429, 500, 503])
@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled"])
async def test_notification_ack_status_is_scoped_to_explicit_november(protocol, status, method):
    context = McpRequestContext() if protocol is None else replace(CONTEXT, protocol_version=protocol)
    seen = []
    auth = McpAuth(McpAuthMode.apim_subscription, "notification-key")
    params = {"requestId": 2} if method == "notifications/cancelled" else None

    def handler(request):
        body = json.loads(request.content)
        seen.append(request)
        assert body == {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        assert request.headers["Ocp-Apim-Subscription-Key"] == "notification-key"
        assert request.headers["MCP-Protocol-Version"] == context.protocol_version.value
        return httpx.Response(status, headers={"Location": "https://elsewhere.example.com/mcp"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        call = HttpxMcpConnector(client=client)._notify(
            client, ENDPOINT, {**auth.headers(), "MCP-Protocol-Version": context.protocol_version.value},
            method=method, params=params, context=context,
        )
        if status == 202 or (status == 204 and protocol is NOVEMBER):
            await call
        else:
            with pytest.raises(McpConnectionError, match=f"HTTP {status}"):
                await call
    assert len(seen) == 1
    assert str(seen[0].url) == ENDPOINT


@pytest.mark.parametrize("preloaded", [False, True])
@pytest.mark.parametrize(("headers", "content", "error"), [
    ({}, b"", None),
    ({"Content-Length": "0"}, b"", None),
    ({"Content-Type": "application/json"}, b"", None),
    ({"Content-Length": "1"}, b"", "content headers"),
    ({"Content-Length": "-1"}, b"", "content headers"),
    ({"Content-Length": "invalid"}, b"", "content headers"),
    ({"Content-Length": "0, 0"}, b"", "content headers"),
    ({"Content-Length": "+0"}, b"", "content headers"),
    ({"Transfer-Encoding": "chunked"}, b"", "content headers"),
    ({"Transfer-Encoding": "identity"}, b"", "content headers"),
    ({"Transfer-Encoding": ""}, b"", "content headers"),
    ({"Content-Encoding": "gzip"}, b"", "content headers"),
    ({"Content-Encoding": "identity"}, b"", "content headers"),
    ({"Content-Range": "bytes 0-0/1"}, b"", "content headers"),
    ({"Trailer": "Digest"}, b"", "content headers"),
    ({"Content-Length": "0"}, b"x", "must be empty"),
    ({"Content-Length": "0"}, b" ", "must be empty"),
    ({"Content-Length": "0"}, b"{", "must be empty"),
    ({"Content-Length": "0"}, b'{"jsonrpc":"2.0","id":2,"result":{}}', "must be empty"),
])
async def test_november_204_ack_requires_empty_headers_and_raw_body(preloaded, headers, content, error):
    seen = []
    stream = _TrackingStream([content, b"must not be read"] if content else [b""])
    reply = httpx.Response(
        204, headers=headers, **({"content": content} if preloaded else {"stream": stream}),
    )
    assert reply.is_stream_consumed is preloaded
    normal = _wire(NOVEMBER, seen)

    def handler(request):
        response = normal(request)
        return reply if seen[-1][1]["method"] == "notifications/initialized" else response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        call = HttpxMcpConnector(client=client).discover(
            endpoint=ENDPOINT, auth=McpAuth(), context=replace(CONTEXT, protocol_version=NOVEMBER),
        )
        if error is None:
            assert [tool.name for tool in await call] == ["forecast"]
        else:
            with pytest.raises(McpConnectionError, match=error):
                await call
    assert reply.is_closed
    if not preloaded:
        assert stream.closed
        assert stream.reads == (0 if error == "content headers" else 1)
    assert [body["method"] for _, body in seen] == [
        "initialize", "notifications/initialized", *([] if error else ["tools/list"]),
    ]


@pytest.mark.parametrize(("protocol", "method"), [
    (protocol, method) for protocol in PROTOCOLS
    for method in ("initialize", "tools/list", "resources/list", "tools/call", "resources/read")
    if protocol in STATEFUL or method != "initialize"
] + [(MODERN, "server/discover")])
@pytest.mark.parametrize("status", [200, 204])
async def test_rpc_204_is_not_a_notification_ack(protocol, method, status):
    seen = []
    normal = _wire(protocol, seen)

    def handler(request):
        response = normal(request)
        if seen[-1][1]["method"] == method and status == 204:
            return httpx.Response(204, headers={"Content-Type": "application/json"})
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HttpxMcpConnector(client=client)
        kwargs = {"endpoint": ENDPOINT, "auth": McpAuth(), "context": replace(CONTEXT, protocol_version=protocol)}
        if method in ("initialize", "tools/list"):
            call = connector.discover(**kwargs)
        elif method == "resources/list":
            call = connector.list_resources(**kwargs)
        elif method == "resources/read":
            call = connector.read_resource(**kwargs, uri=URI)
        elif method == "tools/call":
            call = connector.call_tool(**kwargs, tool="forecast", arguments={})
        else:
            call = connector.discover_server(**kwargs)
        if status == 200:
            assert await call is not None
        else:
            with pytest.raises(McpConnectionError, match="no JSON-RPC response"):
                await call
            assert not connector._cache
    assert sum(body["method"] == method for _, body in seen) == 1


@pytest.mark.parametrize("error_code", [-32020, -32021, -32022, -32601])
async def test_modern_discovery_errors_are_visible_without_legacy_fallback(error_code):
    seen = []
    def response(_request, body):
        return httpx.Response(400, json={
            "jsonrpc": "2.0", "id": body["id"],
            "error": {"code": error_code, "message": "secret=hidden", "data": {
                "supported": [LEGACY.value], "requested": MODERN.value, "requiredCapabilities": {"roots": {}},
            }},
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=response)
    )) as client:
        with pytest.raises(McpConnectionError) as error:
            await HttpxMcpConnector(client=client).discover_server(
                endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT
            )
    assert "hidden" not in str(error.value)
    assert [body["method"] for _, body in seen] == ["server/discover"]


@pytest.mark.parametrize("fault", [
    None, "session", "version-header", "version-meta", "version-result", "result-type",
    "missing-result-type", "input-required", "id", "string-id", "jsonrpc", "both", "batch",
    "content-type", "duplicate-key", "nan", "capabilities", "supported-versions",
])
async def test_modern_contradictory_responses_fail_visibly(fault):
    seen = []
    def response(_request, body):
        result = _result(MODERN, supportedVersions=[MODERN.value], capabilities={"tools": {}})
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        headers = {}
        if fault == "session":
            headers["Mcp-Session-Id"] = "legacy"
        elif fault == "version-header":
            headers["MCP-Protocol-Version"] = LEGACY.value
        elif fault == "version-meta":
            result["_meta"] = {PROTOCOL_META: LEGACY.value}
        elif fault == "version-result":
            result["protocolVersion"] = LEGACY.value
        elif fault == "result-type":
            result["resultType"] = "unnegotiated-extension"
        elif fault == "missing-result-type":
            del result["resultType"]
        elif fault == "input-required":
            result["resultType"] = "input_required"
        elif fault == "id":
            payload["id"] = 999
        elif fault == "string-id":
            payload["id"] = str(body["id"])
        elif fault == "jsonrpc":
            payload["jsonrpc"] = "1.0"
        elif fault == "both":
            payload["error"] = {"code": -32603, "message": "not also a result"}
        elif fault == "batch":
            payload = [payload]
        elif fault == "content-type":
            headers["Content-Type"] = "text/plain"
        elif fault == "duplicate-key":
            return httpx.Response(200, content='{"jsonrpc":"2.0","id":2,"id":3,"result":{}}',
                                  headers={"Content-Type": "application/json"})
        elif fault == "nan":
            return httpx.Response(200, content='{"jsonrpc":"2.0","id":2,"result":{"ttlMs":NaN}}',
                                  headers={"Content-Type": "application/json"})
        elif fault == "capabilities":
            result["capabilities"] = {"tools": True}
        elif fault == "supported-versions":
            result["supportedVersions"] = [LEGACY.value]
        return httpx.Response(200, json=payload, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=response)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        call = connector.discover_server(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
        if fault is None:
            assert (await call).supported_versions == (MODERN.value,)
            assert connector._cache
        else:
            with pytest.raises(McpConnectionError):
                await call
            assert not connector._cache
    assert len(seen) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("credential", [None, "", "bad\r\nvalue", "valid-credential"])
async def test_authenticated_connections_never_degrade_to_anonymous(protocol, credential):
    seen = []
    auth = McpAuth(McpAuthMode.bearer, credential)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, auth=auth)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        call = connector.discover(
            endpoint=ENDPOINT, auth=auth, context=replace(CONTEXT, protocol_version=protocol)
        )
        if credential == "valid-credential":
            assert len(await call) == 1
            assert seen
        else:
            with pytest.raises(McpConnectionError, match="credential"):
                await call
            assert not seen


class _WaitingStream(httpx.AsyncByteStream):
    def __init__(self, chunk=b": started\n\n"):
        self.chunk = chunk
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield self.chunk
        self.started.set()
        await self.release.wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("finished", [False, True])
async def test_november_204_ack_wait_is_bounded_and_closes_stream(finished):
    seen = []
    stream = _WaitingStream(chunk=b"")
    if finished:
        stream.release.set()
    normal = _wire(NOVEMBER, seen)

    def handler(request):
        response = normal(request)
        if seen[-1][1]["method"] == "notifications/initialized":
            return httpx.Response(204, stream=stream)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        call = HttpxMcpConnector(client=client, timeout_s=0.05).discover(
            endpoint=ENDPOINT, auth=McpAuth(), context=replace(CONTEXT, protocol_version=NOVEMBER),
        )
        if finished:
            assert [tool.name for tool in await call] == ["forecast"]
        else:
            with pytest.raises(McpConnectionError, match="transport error"):
                await call
    assert stream.started.is_set()
    assert stream.closed
    assert [body["method"] for _, body in seen] == [
        "initialize", "notifications/initialized", *(["tools/list"] if finished else []),
    ]


@pytest.mark.parametrize(("protocol", "notification_status"), [
    (LEGACY, 202), (NOVEMBER, 202), (NOVEMBER, 204), (MODERN, 202),
])
@pytest.mark.parametrize("cancel", [False, True])
async def test_cancellation_and_total_timeout_close_stream_without_replay(
    protocol, notification_status, cancel, caplog,
):
    seen = []
    stream = _WaitingStream()
    auth = McpAuth(McpAuthMode.api_key, "cancellation-key")
    def response(_request, _body):
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response, auth=auth, notification_status=notification_status)
    )) as client:
        connector = HttpxMcpConnector(client=client, timeout_s=1 if cancel else 0.05)
        task = asyncio.create_task(connector.call_tool(
            endpoint=ENDPOINT, auth=auth, tool="forecast", arguments={},
            context=replace(CONTEXT, protocol_version=protocol),
        ))
        await asyncio.wait_for(stream.started.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(McpConnectionError, match="timed out"):
                await task
    assert stream.closed
    methods = [body["method"] for _, body in seen]
    assert methods.count("tools/call") == 1
    if protocol in STATEFUL:
        assert methods[-1] == "notifications/cancelled"
        assert seen[-1][1]["params"] == {"requestId": 2}
        assert "cancellation notification could not be delivered" not in caplog.text
    else:
        assert methods == ["tools/call"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_stream_returns_on_final_result_without_waiting_for_disconnect(protocol):
    seen = []
    payload = {"jsonrpc": "2.0", "id": 2, "result": _result(protocol, tools=[TOOL])}
    stream = _TrackingStream([
        b": comment\n\n",
        f"data: {json.dumps(payload)}\n\n".encode(),
        b"must not be consumed after the final response",
    ])
    def response(_request, _body):
        return httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response)
    )) as client:
        result = await HttpxMcpConnector(client=client).discover(
            endpoint=ENDPOINT, auth=McpAuth(), context=replace(CONTEXT, protocol_version=protocol)
        )
    assert len(result) == 1
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("private", [False, True])
async def test_dns_pin_uses_public_control_and_denies_rebinding_before_http(protocol, private):
    seen = []
    lookups = []
    def resolver(host):
        lookups.append(host)
        return ["93.184.216.34", "127.0.0.1"] if private else ["93.184.216.34"]
    connector = _MockInnerConnector(_wire(protocol, seen), resolver=resolver)
    call = connector.call_tool(
        endpoint=ENDPOINT, auth=McpAuth(), tool="forecast", arguments={},
        context=replace(CONTEXT, protocol_version=protocol),
    )
    if private:
        with pytest.raises(McpConnectionError, match="permitted egress"):
            await call
        assert not seen
    else:
        assert (await call).content == "Sunny"
        assert seen
        for request, _ in seen:
            assert request.url.host == "93.184.216.34"
            assert request.headers["host"] == "mcp.example.com"
            assert request.extensions["sni_hostname"] == "mcp.example.com"
    assert lookups == ["mcp.example.com"]


def _body(arguments=None, name="forecast"):
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": name, "arguments": arguments or {}, "_meta": {
            PROTOCOL_META: MODERN.value, CLIENT_INFO_META: CLIENT_INFO, CAPABILITIES_META: {},
        },
    }}


@pytest.mark.parametrize("text", ["SEA", " padded ", "\u4e16\u754c", "a\nb", "=?base64?literal?="])
def test_header_encoding_round_trips_without_injection_or_sentinel_ambiguity(text):
    schema = {"properties": {"city": {"type": "string", "x-mcp-header": "Region"}}}
    name = "forecast" if "\n" in text else text
    headers = request_headers(_body({"city": text}, name=name), protocol=MODERN, input_schema=schema)
    for name in (("Mcp-Param-Region",) if "\n" in text else ("Mcp-Name", "Mcp-Param-Region")):
        value = headers[name]
        assert value.isascii() and "\n" not in value
        if text == "SEA":
            assert value == text
        else:
            assert value.startswith("=?base64?") and value.endswith("?=")
            assert base64.b64decode(value[9:-2]).decode("utf-8") == text


def test_nested_annotation_paths_primitive_types_and_null_omission():
    schema = {"properties": {"route": {"properties": {
        "count": {"type": "integer", "x-mcp-header": "Count"},
        "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
        "optional": {"type": "string", "x-mcp-header": "Optional"},
        "absent": {"type": "string", "x-mcp-header": "Absent"},
    }}}}
    headers = request_headers(_body({"route": {
        "count": MAX_SAFE_INTEGER, "enabled": True, "optional": None,
    }}), protocol=MODERN, input_schema=schema)
    assert headers["Mcp-Param-Count"] == str(MAX_SAFE_INTEGER)
    assert headers["Mcp-Param-Enabled"] == "true"
    assert "Mcp-Param-Optional" not in headers
    assert "Mcp-Param-Absent" not in headers


@pytest.mark.parametrize("schema", [
    {"x-mcp-header": "Root", "type": "string"},
    {"properties": {"p": {"x-mcp-header": "", "type": "string"}}},
    {"properties": {"p": {"x-mcp-header": "Bad\r\nName", "type": "string"}}},
    {"properties": {"p": {"x-mcp-header": "x" * 65, "type": "string"}}},
    {"properties": {"p": {"x-mcp-header": "Route", "type": "number"}}},
    {"properties": {"p": {"x-mcp-header": "Route", "type": ["string", "null"]}}},
    {"properties": {"p": {"x-mcp-header": "Route", "type": "string"},
                    "q": {"x-mcp-header": "route", "type": "string"}}},
    {"anyOf": [{"properties": {"p": {"x-mcp-header": "Route", "type": "string"}}}]},
    {"properties": {"p": {"items": {"x-mcp-header": "Route", "type": "string"}}}},
    {"$defs": {"p": {"x-mcp-header": "Route", "type": "string"}}},
    {"properties": {"password": {"x-mcp-header": "Route", "type": "string"}}},
    {"properties": {"p": {"x-mcp-header": "AccessToken", "type": "string"}}},
    {"properties": {"p": {"x-mcp-header": "Bearer", "type": "string"}}},
    {"properties": {"p": {"x-mcp-header": "Route", "type": "string", "writeOnly": True}}},
])
def test_invalid_or_sensitive_header_annotations_are_rejected_not_silently_omitted(schema):
    assert header_parameters({"properties": {"p": {"x-mcp-header": "Route", "type": "string"}}})
    with pytest.raises(McpConnectionError):
        header_parameters(schema)


@pytest.mark.parametrize("value", [True, 1.5, "1", MAX_SAFE_INTEGER + 1, -(MAX_SAFE_INTEGER + 1)])
def test_integer_mirrors_require_exact_safe_integer_values(value):
    schema = {"properties": {"count": {"type": "integer", "x-mcp-header": "Count"}}}
    assert request_headers(_body({"count": 1}), protocol=MODERN, input_schema=schema)["Mcp-Param-Count"] == "1"
    with pytest.raises(McpConnectionError, match="type or range"):
        request_headers(_body({"count": value}), protocol=MODERN, input_schema=schema)


@pytest.mark.parametrize("value", ["token=short", "abcdEFGH1234567890abcdEFGH1234567890", "actual-key"])
def test_sensitive_values_never_become_routing_headers(value):
    schema = {"properties": {"route": {"type": "string", "x-mcp-header": "Region"}}}
    assert request_headers(
        _body({"route": "safe-region"}), protocol=MODERN, input_schema=schema, secret="actual-key"
    )["Mcp-Param-Region"] == "safe-region"
    with pytest.raises(McpConnectionError, match="sensitive"):
        request_headers(_body({"route": value}), protocol=MODERN, input_schema=schema, secret="actual-key")


def test_header_count_value_and_total_size_are_bounded():
    schema = {"properties": {
        f"p{i}": {"type": "string", "x-mcp-header": f"H{i}"}
        for i in range(MAX_ROUTING_HEADERS)
    }}
    assert len(header_parameters(schema)) == MAX_ROUTING_HEADERS
    with pytest.raises(McpConnectionError, match="annotation"):
        header_parameters({"properties": {**schema["properties"],
            "extra": {"type": "string", "x-mcp-header": "Extra"},
        }})
    assert len(encode_header_value("a" * MAX_HEADER_VALUE)) == MAX_HEADER_VALUE
    with pytest.raises(McpConnectionError, match="bounds"):
        encode_header_value("a" * (MAX_HEADER_VALUE + 1))
    args = {f"p{i}": "route. " * 200 for i in range(MAX_ROUTING_HEADERS)}
    with pytest.raises(McpConnectionError, match="bounds"):
        request_headers(_body(args), protocol=MODERN, input_schema=schema)


@pytest.mark.parametrize("fault", [None, "version", "capabilities", "initialize"])
def test_request_metadata_cannot_contradict_protocol_or_claim_unimplemented_capabilities(fault):
    body = _body()
    if fault == "version":
        body["params"]["_meta"][PROTOCOL_META] = LEGACY.value
    elif fault == "capabilities":
        body["params"]["_meta"][CAPABILITIES_META] = {"sampling": {}}
    elif fault == "initialize":
        body["method"] = "initialize"
    if fault is None:
        assert request_headers(body, protocol=MODERN)["Mcp-Method"] == "tools/call"
    else:
        with pytest.raises(McpConnectionError, match="contradicts"):
            request_headers(body, protocol=MODERN)


async def test_discovery_filters_only_invalid_annotated_tools_and_keeps_safe_control(caplog):
    seen = []
    bad = {"name": "bad", "inputSchema": {
        "properties": {"password": {"type": "string", "x-mcp-header": "Route"}},
    }}
    def response(_request, body):
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"], "result": _result(MODERN, tools=[bad, TOOL]),
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=response)
    )) as client:
        assert [tool.name for tool in await HttpxMcpConnector(client=client).discover(
            endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT,
        )] == ["forecast"]
    assert "unsafe parameter-header schema" in caplog.text


@pytest.mark.parametrize("stateful", STATEFUL)
@pytest.mark.parametrize("cache_scope", ["private", "public"])
async def test_list_cache_never_crosses_owner_auth_endpoint_server_protocol_or_config(cache_scope, stateful):
    seen = []
    def response(_request, body):
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": _result(MODERN, tools=[TOOL], cacheScope=cache_scope),
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=response)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        auth = McpAuth(McpAuthMode.bearer, "auth-a")
        first = await connector.discover(endpoint=ENDPOINT, auth=auth, context=CONTEXT)
        first[0].name = "caller-mutated"
        again = await connector.discover(endpoint=ENDPOINT, auth=auth, context=CONTEXT)
        assert again[0].name == "forecast"
        assert len(seen) == 1
        variants = [
            (replace(CONTEXT, owner_id="other-owner"), ENDPOINT, auth),
            (replace(CONTEXT, server_id="other-server"), ENDPOINT, auth),
            (replace(CONTEXT, configuration_revision="new-config"), ENDPOINT, auth),
            (CONTEXT, ENDPOINT + "/other", auth),
            (CONTEXT, ENDPOINT, McpAuth(McpAuthMode.bearer, "auth-b")),
            (CONTEXT, ENDPOINT, McpAuth(McpAuthMode.api_key, "auth-a")),
        ]
        for index, (context, endpoint, credential) in enumerate(variants, start=2):
            await connector.discover(endpoint=endpoint, auth=credential, context=context)
            assert len(seen) == index
        # No auth values or approvals are retained in a cache key.
        assert "auth-a" not in repr(list(connector._cache))
        assert "auth-b" not in repr(list(connector._cache))
        stateful_seen = []
        client._transport = httpx.MockTransport(_wire(stateful, stateful_seen))
        await connector.discover(
            endpoint=ENDPOINT, auth=auth, context=replace(CONTEXT, protocol_version=stateful)
        )
        assert [body["method"] for _, body in stateful_seen] == [
            "initialize", "notifications/initialized", "tools/list",
        ]
        assert all(key[0].protocol_version is MODERN for key in connector._cache)


@pytest.mark.parametrize("ttl", [None, -1, 0, 100, 10**12])
async def test_cache_hints_are_bounded_and_expire_on_access(monkeypatch, ttl):
    clock = [100.0]
    monkeypatch.setattr(mcp_client, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    def response(_request, body):
        result = _result(MODERN, tools=[TOOL])
        if ttl is None:
            result.pop("ttlMs")
        else:
            result["ttlMs"] = ttl
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=response)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        for _ in range(2):
            await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
        assert len(seen) == (1 if ttl and ttl > 0 else 2)
        clock[0] += (min(ttl, mcp_client.MAX_CACHE_TTL_MS) / 1000 if ttl and ttl > 0 else 0) + 0.001
        await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
        assert len(seen) == (2 if ttl and ttl > 0 else 3)


async def test_cache_eviction_invalidation_and_errors_never_return_stale_success(monkeypatch):
    monkeypatch.setattr(mcp_client, "MAX_CACHE_ENTRIES", 2)
    seen = []
    fail = [False]
    normal = _wire(MODERN, seen)
    def handler(request):
        if fail[0]:
            seen.append((request, json.loads(request.content)))
            return httpx.Response(503)
        return normal(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HttpxMcpConnector(client=client)
        for owner in ("one", "two", "three"):
            await connector.discover(
                endpoint=ENDPOINT, auth=McpAuth(), context=replace(CONTEXT, owner_id=owner)
            )
        assert len(connector._cache) == 2
        assert {key[0].owner_id for key in connector._cache} == {"two", "three"}
        assert connector._cache_bytes <= mcp_client.MAX_CACHE_BYTES
        context = replace(CONTEXT, owner_id="three")
        await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=context)
        assert len(seen) == 3
        connector.invalidate(context)
        fail[0] = True
        with pytest.raises(McpConnectionError, match="503"):
            await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=context)
        assert len(seen) == 4
        assert all(key[0].owner_id != "three" for key in connector._cache)
        assert any(key[0].owner_id == "two" for key in connector._cache)


async def test_cache_hit_still_checks_dns_and_resource_bodies_are_never_cached():
    seen = []
    private = [False]
    def resolver(_host):
        return ["127.0.0.1"] if private[0] else ["93.184.216.34"]
    connector = _MockInnerConnector(_wire(MODERN, seen), resolver=resolver)
    for _ in range(2):
        await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
    assert len(seen) == 1
    for _ in range(2):
        await connector.read_resource(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT, uri=URI)
    assert len(seen) == 3
    private[0] = True
    with pytest.raises(McpConnectionError, match="permitted egress"):
        await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
    assert len(seen) == 3


@pytest.mark.parametrize("fault", [None, "repeat", "scope"])
async def test_paginated_lists_cache_by_cursor_and_reject_contradictions(fault):
    seen = []
    def response(_request, body):
        cursor = body["params"].get("cursor")
        result = _result(MODERN, tools=[{**TOOL, "name": cursor or "first"}])
        if cursor is None or fault == "repeat":
            result["nextCursor"] = "second"
        if cursor and fault == "scope":
            result["cacheScope"] = "public"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=response)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        if fault is None:
            for _ in range(2):
                result = await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
                assert [tool.name for tool in result] == ["first", "second"]
            assert len(seen) == 2
        else:
            with pytest.raises(McpConnectionError, match="cursor|cache scopes"):
                await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
            assert not connector._cache


async def test_concurrent_stateless_requests_have_distinct_ids_and_matching_responses():
    ids = []
    ready = asyncio.Event()
    async def handler(request):
        body = json.loads(request.content)
        ids.append(body["id"])
        if len(ids) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 1)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": _result(MODERN, content=[{"type": "text", "text": str(body["id"])}]),
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HttpxMcpConnector(client=client)
        results = await asyncio.gather(*[
            connector.call_tool(
                endpoint=ENDPOINT, auth=McpAuth(), tool="forecast", arguments={}, context=CONTEXT
            ) for _ in range(2)
        ])
    assert len(set(ids)) == 2
    assert {result.content for result in results} == {str(value) for value in ids}


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_transport_read_timeout_cancels_stateful_without_tool_replay(protocol):
    seen = []
    def response(request, _body):
        raise httpx.ReadTimeout("token=never-log", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response)
    )) as client:
        with pytest.raises(McpConnectionError, match="timed out") as error:
            await HttpxMcpConnector(client=client).call_tool(
                endpoint=ENDPOINT, auth=McpAuth(), tool="forecast", arguments={},
                context=replace(CONTEXT, protocol_version=protocol),
            )
    assert "never-log" not in str(error.value)
    methods = [body["method"] for _, body in seen]
    assert methods.count("tools/call") == 1
    assert ("notifications/cancelled" in methods) is (protocol in STATEFUL)


@pytest.mark.parametrize("fits", [False, True])
async def test_cache_serialized_byte_budget_evicts_only_when_needed(monkeypatch, fits):
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(MODERN, seen))) as client:
        connector = HttpxMcpConnector(client=client)
        await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT)
        size = connector._cache_bytes
        monkeypatch.setattr(mcp_client, "MAX_CACHE_BYTES", 2 * size - int(not fits))
        await connector.discover(
            endpoint=ENDPOINT, auth=McpAuth(), context=replace(CONTEXT, owner_id="other")
        )
        assert len(connector._cache) == (2 if fits else 1)
        assert connector._cache_bytes <= mcp_client.MAX_CACHE_BYTES
        assert len(seen) == 2


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("fits", [False, True])
async def test_raw_response_bounds_close_all_protocols_without_overreading(protocol, fits):
    seen = []
    payload = {"jsonrpc": "2.0", "id": 2, "result": _result(
        protocol, content=[{"type": "text", "text": "x" * 2000}],
    )}
    raw = json.dumps(payload).encode()
    stream = _TrackingStream([raw[index:index + 128] for index in range(0, len(raw), 128)])
    def response(_request, _body):
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response)
    )) as client:
        connector = HttpxMcpConnector(client=client, max_bytes=4000 if fits else 512)
        call = connector.call_tool(
            endpoint=ENDPOINT, auth=McpAuth(), tool="forecast", arguments={},
            context=replace(CONTEXT, protocol_version=protocol),
        )
        if fits:
            assert (await call).content == "x" * 2000
            assert stream.reads == len(stream._chunks)
        else:
            with pytest.raises(McpConnectionError, match="response too large"):
                await call
            assert stream.reads == 5
    assert stream.closed


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_redirects_cannot_reach_another_host_even_with_redirect_enabled_test_client(protocol):
    seen = []
    def response(_request, _body):
        return httpx.Response(307, headers={"Location": "https://127.0.0.1/private"})
    async with httpx.AsyncClient(
        follow_redirects=True, transport=httpx.MockTransport(_wire(protocol, seen, response=response)),
    ) as client:
        with pytest.raises(McpConnectionError, match="307"):
            await HttpxMcpConnector(client=client).call_tool(
                endpoint=ENDPOINT, auth=McpAuth(), tool="forecast", arguments={},
                context=replace(CONTEXT, protocol_version=protocol),
            )
    assert all(request.url.host == "mcp.example.com" for request, _ in seen)
    assert sum(body["method"] == "tools/call" for _, body in seen) == 1


@pytest.mark.parametrize("protocol", STATEFUL)
@pytest.mark.parametrize("operation", ["tools/list", "resources/list"])
@pytest.mark.parametrize("session_bound", [False, True])
async def test_stateful_pagination_keeps_session_scoped_cursors_and_distinct_request_ids(
    protocol, operation, session_bound,
):
    requests = []
    initialized = []
    lookups = []
    clients = []
    auth = McpAuth(McpAuthMode.api_key, "legacy-key")
    def handler(request):
        body = json.loads(request.content)
        assert request.headers["X-API-Key"] == "legacy-key"
        method = body["method"]
        if method == "initialize":
            session = f"session-{len(initialized) + 1}"
            initialized.append(session)
            return httpx.Response(200, headers={"Mcp-Session-Id": session}, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": protocol.value, "capabilities": {"tools": {}, "resources": {}}},
            })
        assert request.headers["MCP-Protocol-Version"] == protocol.value
        assert "_meta" not in body.get("params", {})
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == operation
        session = request.headers["Mcp-Session-Id"]
        cursor = body["params"].get("cursor")
        requests.append((session, cursor, body["id"]))
        if cursor and session_bound and cursor != f"{session}:next":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "error": {"code": -32602, "message": "Cursor belongs to another session."},
            })
        name = "second" if cursor else "first"
        fields = (
            {"tools": [{**TOOL, "name": name}]} if operation == "tools/list"
            else {"resources": [{"uri": f"skill://{name}/SKILL.md", "name": name}]}
        )
        if not cursor:
            fields["nextCursor"] = f"{session}:next" if session_bound else "portable"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": fields})
    def resolver(host):
        lookups.append(host)
        return ["93.184.216.34"]
    class Connector(_MockInnerConnector):
        def _new_client(self, pinned_ip):
            client = super()._new_client(pinned_ip)
            clients.append(client)
            return client
    connector = Connector(handler, resolver=resolver)
    method = connector.discover if operation == "tools/list" else connector.list_resources
    result = await method(
        endpoint=ENDPOINT, auth=auth, context=replace(CONTEXT, protocol_version=protocol),
    )
    assert [item.name for item in result] == ["first", "second"]
    if session_bound:
        assert initialized == ["session-1"]
        assert requests == [("session-1", None, 2), ("session-1", "session-1:next", 3)]
    assert lookups == ["mcp.example.com"]
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("protocol", STATEFUL)
@pytest.mark.parametrize("operation", ["tools/list", "resources/list"])
@pytest.mark.parametrize("fault", [None, "repeat", "page-limit"])
async def test_stateful_pagination_bounds_and_cache_hints_never_escape_session(protocol, operation, fault):
    seen = []

    def response(_request, body):
        page = body["id"] - 2
        fields = (
            {"tools": [{**TOOL, "name": f"tool-{page}"}]} if operation == "tools/list"
            else {"resources": [{"uri": f"skill://resource-{page}/SKILL.md"}]}
        )
        if not page or fault:
            fields["nextCursor"] = "repeated" if fault == "repeat" else f"cursor-{page + 1}"
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {**fields, "ttlMs": 1000, "cacheScope": "public"},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, response=response)
    )) as client:
        connector = HttpxMcpConnector(client=client)
        method = connector.discover if operation == "tools/list" else connector.list_resources
        context = replace(CONTEXT, protocol_version=protocol)
        if fault is None:
            for _ in range(2):
                assert len(await method(endpoint=ENDPOINT, auth=McpAuth(), context=context)) == 2
            assert [body["method"] for _, body in seen] == [
                "initialize", "notifications/initialized", operation, operation,
            ] * 2
        else:
            with pytest.raises(McpConnectionError, match="cursor|pagination"):
                await method(endpoint=ENDPOINT, auth=McpAuth(), context=context)
            pages = [body for _, body in seen if body["method"] == operation]
            assert len(pages) == (2 if fault == "repeat" else mcp_client.MAX_LIST_PAGES)
            assert len(seen) == len(pages) + 2
        assert not connector._cache
        assert connector._cache_bytes == 0


@pytest.mark.parametrize("protocol", STATEFUL)
async def test_stateful_pagination_item_cap_closes_owned_client_without_fetching_another_page(protocol):
    clients = []
    seen = []
    def response(_request, body):
        assert "cursor" not in body["params"]
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"tools": [{**TOOL, "name": f"tool-{i}"} for i in range(50)], "nextCursor": "unused"},
        })
    class Connector(_MockInnerConnector):
        def _new_client(self, pinned_ip):
            client = super()._new_client(pinned_ip)
            clients.append(client)
            return client
    connector = Connector(_wire(protocol, seen, response=response), resolver=lambda _host: ["93.184.216.34"])
    assert len(await connector.discover(
        endpoint=ENDPOINT, auth=McpAuth(), context=replace(CONTEXT, protocol_version=protocol),
    )) == 50
    assert len(seen) == 3
    assert len(clients) == 1 and clients[0].is_closed
