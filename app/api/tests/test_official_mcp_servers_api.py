"""API tests for the read-only **official** MCP-server listing router.

Unlike the BYO surface (which 404s when its feature is off), the official listing
is always present: with the plane disabled it returns an **empty list** so the
agent-builder UI can call it unconditionally. With the plane on it returns the
curated servers and their discovered tools — trusted, app-global, and carrying no
secret. Tests inject a deterministic ``OfficialMcpService`` (in-memory catalog +
``FakeMcpConnector``) so nothing touches DNS or a live server.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_client import FakeMcpConnector
from ai4ia_api.agents.mcp_servers import DiscoveredTool
from ai4ia_api.agents.official_mcp_service import (
    OFFICIAL_USER_ID,
    OfficialMcpService,
)
from ai4ia_api.main import create_app
from ai4ia_api.official_mcp_catalog import OfficialMcpCatalog, OfficialMcpServer
from tests.conftest import make_settings

_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub


def _client() -> TestClient:
    """A client whose lifespan has run (official plane OFF by default → service None)."""
    c = TestClient(create_app(make_settings()))
    c.__enter__()
    return c


def _inject_official(c: TestClient, *tools: str) -> None:
    catalog = OfficialMcpCatalog(
        servers=[
            OfficialMcpServer(
                id="ms-learn",
                displayName="Microsoft Learn",
                description="Official Microsoft Learn MCP server",
                path="ms-learn/mcp",
            )
        ]
    )
    discovered = [DiscoveredTool(name=t, description=f"{t} tool") for t in tools]
    c.app.state.official_mcp_service = OfficialMcpService(
        catalog,
        gateway_url="https://apim-mcp.azure-api.net",
        subscription_key="APIM-KEY",
        connector=FakeMcpConnector(discovered),
        resolver=_PUBLIC_RESOLVER,
    )


def test_disabled_plane_returns_empty_list_not_404():
    c = _client()
    resp = c.get("/api/agents/official-mcp-servers")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"servers": []}


def test_enabled_lists_servers_with_discovered_tools():
    c = _client()
    _inject_official(c, "search_docs", "fetch_doc")

    resp = c.get("/api/agents/official-mcp-servers")
    assert resp.status_code == 200, resp.text
    servers = resp.json()["servers"]
    assert len(servers) == 1

    srv = servers[0]
    assert srv["name"] == "ms-learn"
    assert srv["displayName"] == "Microsoft Learn"
    assert srv["userId"] == OFFICIAL_USER_ID
    assert srv["trusted"] is True
    assert srv["enabled"] is True
    # Endpoint is composed from the gateway URL + the catalog path.
    assert srv["endpoint"] == "https://apim-mcp.azure-api.net/ms-learn/mcp"
    assert [t["name"] for t in srv["discoveredTools"]] == ["search_docs", "fetch_doc"]
    # No app-global credential ever leaks onto the wire shape.
    assert srv["secretRef"] is None
    assert "secret" not in srv
