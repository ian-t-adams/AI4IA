"""Tests for the MCP-server models, governance mapping, and service (Phase 12A).

Uses the in-memory store + the deterministic ``FakeMcpConnector`` + a stub
resolver so nothing touches real DNS or a live server.
"""
from __future__ import annotations

import pytest

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpAuth
from ai4ia_api.agents.mcp_servers import (
    MAX_MCP_SERVERS_PER_USER,
    DiscoveredTool,
    McpAuthMode,
    McpConflictError,
    McpConnectionError,
    McpNotFoundError,
    McpValidationError,
    UserMcpServer,
    UserMcpServerCreate,
    UserMcpServerUpdate,
    discovered_tool_to_spec,
    namespaced_tool_name,
)
from ai4ia_api.agents.mcp_store import InMemoryUserMcpServerStore
from ai4ia_api.agents.mcp_service import McpServerService
from ai4ia_api.agents.tools import ToolRisk

_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub
_TOOLS = [
    DiscoveredTool(name="get_forecast", description="Weather forecast"),
    DiscoveredTool(name="get_alerts", description="Severe weather alerts"),
]


def _service(*, tools=None, error=None, resolver=None, max_servers=MAX_MCP_SERVERS_PER_USER):
    return McpServerService(
        InMemoryUserMcpServerStore(),
        connector=FakeMcpConnector(tools if tools is not None else list(_TOOLS), error=error),
        max_servers=max_servers,
        resolver=resolver or _PUBLIC_RESOLVER,
    )


def _create(name="weather", **over):
    body = dict(name=name, endpoint="https://mcp.example.com/rpc")
    body.update(over)
    return UserMcpServerCreate(**body)


# --- Governance mapping (pure) -----------------------------------------------


def test_namespaced_tool_name():
    assert namespaced_tool_name("weather", "get_forecast") == "mcp:weather/get_forecast"


def test_discovered_tool_to_spec_untrusted_requires_approval():
    server = UserMcpServer(
        id="weather",
        userId="u1",
        name="weather",
        displayName="Weather",
        endpoint="https://mcp.example.com/rpc",
        host="mcp.example.com",
        trusted=False,
        enabled=True,
        discoveredTools=list(_TOOLS),
    )
    spec = discovered_tool_to_spec(server, _TOOLS[0])
    assert spec.name == "mcp:weather/get_forecast"
    assert spec.risk is ToolRisk.external
    assert spec.requires_approval is True
    assert spec.egress_allowlist == frozenset({"mcp.example.com"})
    assert spec.enabled is True


def test_discovered_tool_to_spec_trusted_skips_approval_and_disabled_propagates():
    server = UserMcpServer(
        id="weather",
        userId="u1",
        name="weather",
        displayName="Weather",
        endpoint="https://mcp.example.com/rpc",
        host="mcp.example.com",
        trusted=True,
        enabled=False,
        discoveredTools=list(_TOOLS),
    )
    specs = server.tool_specs()
    assert len(specs) == 2
    assert all(s.requires_approval is False for s in specs)
    assert all(s.enabled is False for s in specs)


# --- Create ------------------------------------------------------------------


async def test_create_persists_and_caches_tools():
    svc = _service()
    server = await svc.create("u1", _create(displayName="Weather"))
    assert server.name == "weather"
    assert server.host == "mcp.example.com"
    assert [t.name for t in server.discoveredTools] == ["get_forecast", "get_alerts"]
    assert server.lastConnectedAt is not None
    assert server.lastError is None
    # No secret field exists on the durable record at all.
    assert "secret" not in server.model_dump()

    again = await svc.get("u1", "weather")
    assert again.name == "weather"


async def test_create_normalizes_name_case():
    svc = _service()
    server = await svc.create("u1", _create(name="Weather"))
    assert server.name == "weather"


async def test_create_rejects_invalid_name():
    svc = _service()
    with pytest.raises(McpValidationError):
        await svc.create("u1", _create(name="1bad"))


async def test_create_rejects_duplicate():
    svc = _service()
    await svc.create("u1", _create())
    with pytest.raises(McpConflictError):
        await svc.create("u1", _create())


async def test_create_enforces_cap():
    svc = _service(max_servers=1)
    await svc.create("u1", _create(name="one"))
    with pytest.raises(McpConflictError):
        await svc.create("u1", _create(name="two"))


async def test_create_rejects_ssrf_endpoint():
    svc = _service(resolver=lambda _h: ["10.0.0.1"])
    with pytest.raises(McpValidationError):
        await svc.create("u1", _create())


async def test_create_requires_secret_for_authed_server():
    svc = _service()
    with pytest.raises(McpValidationError):
        await svc.create("u1", _create(authMode=McpAuthMode.bearer))


async def test_create_passes_secret_to_connector_but_does_not_store_it():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(), connector=connector, resolver=_PUBLIC_RESOLVER
    )
    server = await svc.create(
        "u1", _create(authMode=McpAuthMode.api_key, secret="s3cr3t")
    )
    # The connector saw the secret transiently...
    assert connector.calls
    _endpoint, auth = connector.calls[-1]
    assert isinstance(auth, McpAuth)
    assert auth.secret == "s3cr3t"
    # ...but it is nowhere on the persisted record.
    assert "s3cr3t" not in server.model_dump_json()
    assert server.authMode is McpAuthMode.api_key


async def test_create_connection_failure_raises_and_persists_nothing():
    svc = _service(error=McpConnectionError("boom"))
    with pytest.raises(McpConnectionError):
        await svc.create("u1", _create())
    assert await svc.list_for("u1") == []


async def test_create_wraps_unexpected_connector_error():
    svc = _service(error=RuntimeError("kaboom"))
    with pytest.raises(McpConnectionError):
        await svc.create("u1", _create())


# --- Update / delete / list --------------------------------------------------


async def test_update_replaces_and_preserves_created_at():
    svc = _service()
    created = await svc.create("u1", _create(description="old"))
    updated = await svc.update(
        "u1",
        "weather",
        UserMcpServerUpdate(
            endpoint="https://mcp.example.com/rpc", description="new", trusted=True
        ),
    )
    assert updated.description == "new"
    assert updated.trusted is True
    assert updated.createdAt == created.createdAt


async def test_update_missing_raises_not_found():
    svc = _service()
    with pytest.raises(McpNotFoundError):
        await svc.update(
            "u1", "nope", UserMcpServerUpdate(endpoint="https://mcp.example.com/rpc")
        )


async def test_delete_is_idempotent():
    svc = _service()
    await svc.create("u1", _create())
    await svc.delete("u1", "weather")
    with pytest.raises(McpNotFoundError):
        await svc.get("u1", "weather")
    # Deleting again does not raise.
    await svc.delete("u1", "weather")


async def test_list_is_per_user():
    svc = _service()
    await svc.create("u1", _create(name="a"))
    await svc.create("u2", _create(name="b"))
    assert {s.name for s in await svc.list_for("u1")} == {"a"}
    assert {s.name for s in await svc.list_for("u2")} == {"b"}


# --- Test (re-discovery) -----------------------------------------------------


async def test_test_refreshes_tools():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(), connector=connector, resolver=_PUBLIC_RESOLVER
    )
    await svc.create("u1", _create())
    connector._tools = [DiscoveredTool(name="new_tool")]  # server now offers a new tool
    refreshed = await svc.test("u1", "weather")
    assert [t.name for t in refreshed.discoveredTools] == ["new_tool"]
    assert refreshed.lastError is None


async def test_test_authed_server_requires_secret():
    svc = _service()
    await svc.create("u1", _create(authMode=McpAuthMode.bearer, secret="t"))
    with pytest.raises(McpValidationError):
        await svc.test("u1", "weather")  # no secret re-supplied


async def test_test_connection_failure_records_last_error():
    connector = FakeMcpConnector(list(_TOOLS))
    svc = McpServerService(
        InMemoryUserMcpServerStore(), connector=connector, resolver=_PUBLIC_RESOLVER
    )
    await svc.create("u1", _create())
    connector._error = McpConnectionError("down")
    with pytest.raises(McpConnectionError):
        await svc.test("u1", "weather")
    stored = await svc.get("u1", "weather")
    assert stored.lastError == "down"
