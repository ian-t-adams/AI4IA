"""Dual-protocol service, execution, consent and progressive-disclosure seams."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from ai4ia_api.agents import mcp_client
from ai4ia_api.agents.approvals import approval_key, arguments_digest
from ai4ia_api.agents.mcp_client import HttpxMcpConnector, McpAuth
from ai4ia_api.agents.mcp_execution import build_mcp_tool_definitions
from ai4ia_api.agents.mcp_protocol import McpRequestContext
from ai4ia_api.agents.mcp_secrets import InMemoryMcpSecretStore
from ai4ia_api.agents.mcp_servers import (
    DiscoveredTool, McpAuthMode, McpNotFoundError,
    UserMcpServer, UserMcpServerCreate, UserMcpServerUpdate,
)
from ai4ia_api.agents.mcp_service import McpServerService
from ai4ia_api.agents.mcp_skills import build_load_skill_definition
from ai4ia_api.agents.mcp_store import InMemoryUserMcpServerStore
from ai4ia_api.agents.official_mcp_service import OfficialMcpService
from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, ToolExecutionError, build_tools
from ai4ia_api.official_mcp_catalog import OfficialMcpCatalog, OfficialMcpServer
from tests.test_mcp_execution import ScriptedGateway, _assistant_text, _assistant_tool_calls, _messages
from tests.test_mcp_protocol import CONTEXT, ENDPOINT, LEGACY, MODERN, URI, _result, _wire

PUBLIC = lambda _host: ["93.184.216.34"]  # noqa: E731
ROUTING_SCHEMA = {"type": "object", "properties": {
    "city": {"type": "string", "x-mcp-header": "Region"},
}}


def _server(protocol, plane="byo"):
    return UserMcpServer(
        id="weather", name="weather", userId="owner" if plane == "byo" else "__official__",
        displayName="Weather", endpoint=ENDPOINT, host="mcp.example.com", trusted=True,
        protocolVersion=protocol, configurationRevision="revision",
        discoveredTools=[DiscoveredTool(
            name="forecast", description="Weather", inputSchema=deepcopy(ROUTING_SCHEMA),
        )],
    )


class _Secrets:
    async def secret_for(self, _server):
        return None


@pytest.mark.parametrize("protocol", [LEGACY, MODERN])
@pytest.mark.parametrize("plane", ["byo", "official"])
@pytest.mark.parametrize("gate", ["allowed", "scope", "approval", "arguments", "owner"])
async def test_real_dispatch_keeps_owner_scopes_exact_approvals_and_routing_contract(protocol, plane, gate):
    seen = []
    server = _server(protocol, plane)
    current = server.model_copy(update={"userId": "wrong-owner"}) if gate == "owner" else server
    async def current_server(_name):
        return current
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(protocol, seen))) as client:
        connector = HttpxMcpConnector(client=client)
        [definition] = build_mcp_tool_definitions(
            [server], attached_tool_names=["mcp:weather/forecast"], secrets=_Secrets(),
            connector=connector, resolver=PUBLIC, budget={"used": 0}, plane_id=plane,
            current_server=current_server,
        )
        assert definition.consent_metadata["protocolVersion"] == protocol.value
        assert definition.parameters == ROUTING_SCHEMA
        definition = replace(definition, spec=replace(
            definition.spec, scopes=frozenset({"weather.read"})
        ))
        registry, executor = build_tools(extra=[definition])
        alias = definition.spec.name
        args = {"city": "SEA"}
        approved_args = {"city": "another-city"} if gate == "arguments" else args
        ctx = ToolContext(
            approvals=frozenset({alias}),
            granted_scopes=frozenset() if gate == "scope" else frozenset({"weather.read"}),
            invocation_approvals=frozenset() if gate == "approval" else frozenset({
                approval_key(alias, arguments_digest(approved_args)),
            }),
        )
        result = await run_agent_turn(
            deployment="dep", messages=_messages(), tool_names=[alias],
            gateway=ScriptedGateway([
                _assistant_tool_calls([("c1", alias, '{"city":"SEA"}')]),
                _assistant_text("done"),
            ]), registry=registry, executor=executor, ctx=ctx,
        )
    if gate == "allowed":
        calls = [(request, body) for request, body in seen if body["method"] == "tools/call"]
        assert len(calls) == 1
        request, body = calls[0]
        assert body["params"]["arguments"] == args
        assert any(step.kind == "tool_result" for step in result.steps)
        if protocol is MODERN:
            assert request.headers["Mcp-Param-Region"] == args["city"]
        else:
            assert "Mcp-Param-Region" not in request.headers
    else:
        assert not seen
        assert any(step.kind in ("tool_denied", "tool_error") for step in result.steps)


@pytest.mark.parametrize("protocol", [LEGACY, MODERN])
@pytest.mark.parametrize("change", [None, "protocol", "routing-schema", "owner"])
async def test_mutation_during_credential_resolution_cannot_change_the_bound_call(protocol, change):
    seen = []
    server = _server(protocol)
    class Secrets:
        async def secret_for(self, _server):
            if change == "protocol":
                server.protocolVersion = LEGACY if protocol is MODERN else MODERN
            elif change == "routing-schema":
                server.discoveredTools[0].inputSchema["properties"]["city"]["x-mcp-header"] = "Other"
            elif change == "owner":
                server.userId = "different-owner"
            return None
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(protocol, seen))) as client:
        [definition] = build_mcp_tool_definitions(
            [server], attached_tool_names=["mcp:weather/forecast"], secrets=Secrets(),
            connector=HttpxMcpConnector(client=client), resolver=PUBLIC, budget={"used": 0},
        )
        call = definition.handler({"city": "SEA"}, ToolContext())
        if change is None:
            assert (await call)["content"] == "Sunny"
            assert seen
        else:
            with pytest.raises(ToolExecutionError):
                await call
            assert not seen


@pytest.mark.parametrize("protocol", [LEGACY, MODERN])
async def test_byo_reconnects_preserve_protocol_and_rotate_auth_without_cached_grants(protocol):
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(protocol, seen))) as client:
        connector = HttpxMcpConnector(client=client)
        service = McpServerService(
            InMemoryUserMcpServerStore(), connector=connector,
            secret_store=InMemoryMcpSecretStore(), resolver=PUBLIC,
        )
        server = await service.create("owner", UserMcpServerCreate(
            name="weather", endpoint=ENDPOINT, protocolVersion=protocol,
            authMode=McpAuthMode.bearer, secret="first-key", trusted=True,
        ))
        with pytest.raises(McpNotFoundError):
            await service.get("someone-else", "weather")
        assert server.protocolVersion is protocol
        assert not connector._cache  # Management save never caches authorization or discovery.
        original_revision = server.configurationRevision
        server = await service.update("owner", "weather", UserMcpServerUpdate(
            endpoint=ENDPOINT, authMode=McpAuthMode.bearer, secret="rotated-key", trusted=False,
        ))
        assert server.protocolVersion is protocol
        assert server.configurationRevision != original_revision
        assert server.trusted is False
        assert await service.secret_for(server) == "rotated-key"
        assert not connector._cache
        await service.test("owner", "weather")
        assert not connector._cache
        lists = [request for request, body in seen if body["method"] == "tools/list"]
        assert len(lists) == 3
        assert lists[0].headers["Authorization"] == "Bearer first-key"
        assert all(request.headers["Authorization"] == "Bearer rotated-key" for request in lists[1:])
        await service.delete("owner", "weather")
        assert not await service.list_for("owner")


def _official(connector, protocol, *, resources=True):
    return OfficialMcpService(
        OfficialMcpCatalog(servers=[OfficialMcpServer(
            id="weather", displayName="Weather", path="weather/mcp",
            protocolVersion=protocol, resourcesEnabled=resources,
        )]),
        gateway_url="https://apim.example.com",
        subscription_key="official-key", connector=connector, resolver=PUBLIC,
        retry_interval_s=0,
    )


@pytest.mark.parametrize("protocol", [LEGACY, MODERN])
async def test_official_progressive_resources_use_the_same_protocol_and_keep_instruction_opt_in(protocol):
    seen = []
    auth = McpAuth(McpAuthMode.apim_subscription, "official-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(protocol, seen, auth=auth)
    )) as client:
        service = _official(HttpxMcpConnector(client=client), protocol)
        [server] = await service.list_all()
        assert server.protocolVersion is protocol
        assert server.userId == "__official__"
        assert server.resourcesEnabled
        assert not any(body["method"] == "resources/read" for _, body in seen)
        definition = build_load_skill_definition(servers=[server], reader=service)
        assert definition is not None
        assert definition.consent_metadata["skills"][0]["server"]["protocolVersion"] == protocol.value
        result = await definition.handler({"name": "evidence-review"}, ToolContext())
        assert result["instructions"] == "Skill instructions"
        assert result["source"]["uri"] == URI
        assert result["contentSha256"]
        assert sum(body["method"] == "resources/read" for _, body in seen) == 1
        server.resourcesEnabled = False
        assert build_load_skill_definition(servers=[server], reader=service) is None
        with pytest.raises(ValueError, match="not enabled"):
            await service.read_resource(server, URI)
        assert sum(body["method"] == "resources/read" for _, body in seen) == 1


async def test_official_modern_cache_expires_and_invalidates_for_config_and_auth(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(mcp_client, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(MODERN, seen))) as client:
        connector = HttpxMcpConnector(client=client)
        service = _official(connector, MODERN)
        [server] = await service.list_all()
        assert len(seen) == 3
        assert await service.list_all() == [server]
        assert len(seen) == 3
        clock[0] += 1
        await service.list_all()
        assert len(seen) == 6
        service._subscription_key = "rotated-official-key"
        await service.list_all()
        assert len(seen) == 9
        assert all(request.headers["Ocp-Apim-Subscription-Key"] == "rotated-official-key"
                   for request, _ in seen[-3:])
        server.endpoint += "/replacement"
        await service.list_all()
        assert len(seen) == 12
        assert all(str(request.url).endswith("/replacement") for request, _ in seen[-3:])
        service.refresh()
        assert not connector._cache
        await service.list_all()
        assert len(seen) == 15


@pytest.mark.parametrize("protocol", [LEGACY, MODERN])
async def test_official_resource_failure_is_visible_partial_and_retry_stays_in_protocol(protocol, caplog):
    seen = []
    failure = [False]
    normal = _wire(protocol, seen)
    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "resources/list" and failure[0]:
            seen.append((request, body))
            raise httpx.ConnectError("credential=do-not-log", request=request)
        return normal(request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = _official(HttpxMcpConnector(client=client), protocol)
        [server] = await service.list_all()
        assert server.discoveredTools and server.discoveredResources
        failure[0] = True
        service.refresh()
        await service.list_all()
        assert server.discoveredTools
        assert not server.discoveredResources
        assert server.lastError
        assert "do-not-log" not in caplog.text
        failure[0] = False
        await service.list_all()
        assert server.discoveredResources
        assert server.lastError is None


async def test_invalidation_fences_in_flight_discovery_cache_fills():
    started = asyncio.Event()
    release = asyncio.Event()
    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": json.loads(request.content)["id"],
            "result": _result(MODERN, tools=[]),
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = HttpxMcpConnector(client=client)
        task = asyncio.create_task(connector.discover(
            endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT
        ))
        await asyncio.wait_for(started.wait(), 1)
        connector.invalidate(CONTEXT)
        release.set()
        assert await task == []
        assert not connector._cache
        assert await connector.discover(endpoint=ENDPOINT, auth=McpAuth(), context=CONTEXT) == []
        assert connector._cache  # The identical response caches after, not across, invalidation.


@pytest.mark.parametrize("change", ["protocolVersion", "endpoint", "authMode", "secretRef", "resourcesEnabled"])
def test_request_context_changes_for_connection_configuration_even_without_revision_bump(change):
    server = _server(LEGACY)
    original = McpRequestContext.for_server(server)
    value = {
        "protocolVersion": MODERN, "endpoint": ENDPOINT + "/other",
        "authMode": McpAuthMode.bearer, "secretRef": "different-key-reference", "resourcesEnabled": True,
    }[change]
    assert McpRequestContext.for_server(server.model_copy()) == original
    setattr(server, change, value)
    assert McpRequestContext.for_server(server) != original


class _RefreshingWire:
    def __init__(self, block_method):
        self.block_method = block_method
        self.refreshing = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def response(self, _request, body):
        method = body["method"]
        if self.refreshing and method == self.block_method:
            self.started.set()
            await self.release.wait()
        label = "new" if self.refreshing else "old"
        fields = {
            "server/discover": {"supportedVersions": [MODERN.value], "capabilities": {"tools": {}, "resources": {}}},
            "tools/list": {"tools": [{
                "name": f"{label}_tool", "description": label, "inputSchema": ROUTING_SCHEMA,
            }]},
            "resources/list": {"resources": [{"uri": URI, "name": "evidence-review", "description": label}]},
            "resources/read": {"contents": [{"uri": URI, "text": f"{label} skill"}]},
        }[method]
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"], "result": _result(MODERN, ttlMs=10, **fields),
        })


@pytest.mark.parametrize("block_method", ["tools/list", "resources/list"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_official_readers_wait_for_refresh_and_cancellation_cannot_serve_stale_metadata(
    monkeypatch, block_method, cancel,
):
    clock = [100.0]
    monkeypatch.setattr(mcp_client, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    wire = _RefreshingWire(block_method)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=wire.response)
    )) as client:
        service = _official(HttpxMcpConnector(client=client), MODERN)
        service._retry_interval_s = 60
        [server] = await service.list_all()
        assert [tool.name for tool in server.discoveredTools] == ["old_tool"]
        assert (await service.read_resource(server, URI)).text == "old skill"
        clock[0] += 0.011
        wire.refreshing = True
        refresh = asyncio.create_task(service.list_all())
        await asyncio.wait_for(wire.started.wait(), 1)
        reader = asyncio.create_task(service.list_all())
        resource = asyncio.create_task(service.read_resource(server, URI))
        try:
            await asyncio.sleep(0)
            assert not reader.done()  # Backoff must not expose the expired snapshot.
            assert not resource.done()
            if cancel:
                refresh.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await refresh
                [after] = await reader
                assert not after.discoveredTools
                assert not after.discoveredResources
                assert after.lastError == "MCP discovery was cancelled."
                assert after.consecutiveFailures == 0
                with pytest.raises(ValueError, match="not advertised"):
                    await resource
                before = len(seen)
                [backoff] = await service.list_all()
                assert not backoff.discoveredTools and not backoff.discoveredResources
                assert backoff.lastError
                assert len(seen) == before
            else:
                wire.release.set()
                await refresh
                [after] = await reader
                assert [tool.name for tool in after.discoveredTools] == ["new_tool"]
                assert after.discoveredResources[0].description == "new"
                assert after.lastError is None
                assert (await resource).text == "new skill"
            assert sum(body["method"] == "resources/read" for _, body in seen) == (1 if cancel else 2)
        finally:
            wire.release.set()
            if not refresh.done():
                refresh.cancel()
            await asyncio.gather(refresh, reader, resource, return_exceptions=True)


@pytest.mark.parametrize("change", [None, "auth", "endpoint", "refresh"])
async def test_official_in_flight_discovery_cannot_publish_invalidated_identity(monkeypatch, change):
    clock = [100.0]
    monkeypatch.setattr(mcp_client, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    wire = _RefreshingWire("tools/list")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        _wire(MODERN, seen, response=wire.response)
    )) as client:
        service = _official(HttpxMcpConnector(client=client), MODERN)
        [server] = await service.list_all()
        clock[0] += 0.011
        wire.refreshing = True
        task = asyncio.create_task(service.list_all())
        await asyncio.wait_for(wire.started.wait(), 1)
        if change == "auth":
            service._subscription_key = "replacement-key"
        elif change == "endpoint":
            server.endpoint += "/replacement"
        elif change == "refresh":
            service.refresh()
        wire.release.set()
        [result] = await task
        if change is None:
            assert [tool.name for tool in result.discoveredTools] == ["new_tool"]
        else:
            assert not result.discoveredTools and not result.discoveredResources
            assert result.lastError == "MCP configuration changed during discovery."
            assert result.consecutiveFailures == 0
            [fresh] = await service.list_all()
            assert [tool.name for tool in fresh.discoveredTools] == ["new_tool"]
            assert fresh.lastError is None
