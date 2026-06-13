"""API tests for the MCP-server management router (Phase 12A).

The feature is flag-gated: with ``custom_tools_enabled`` off the whole surface
404s (zero regression). With it on, the router drives the service's CRUD + the
``/test`` re-discovery. Tests inject a service backed by the in-memory store, the
``FakeMcpConnector``, and a public-IP stub resolver so nothing touches DNS or a
live server.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_client import FakeMcpConnector
from ai4ia_api.agents.mcp_servers import DiscoveredTool
from ai4ia_api.agents.mcp_service import McpServerService
from ai4ia_api.agents.mcp_store import InMemoryUserMcpServerStore
from ai4ia_api.main import create_app
from tests.conftest import make_settings

_TOOLS = [DiscoveredTool(name="get_forecast", description="Forecast")]
_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub


def _enabled_client(*, tools=None, error=None) -> TestClient:
    app = create_app(make_settings(custom_tools_enabled=True))
    c = TestClient(app)
    # Enter the lifespan once, then replace the service with a deterministic one.
    # (A ``with`` block would re-enter and rebuild app.state, clobbering this.)
    c.__enter__()
    c.app.state.mcp_service = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=FakeMcpConnector(tools if tools is not None else list(_TOOLS), error=error),
        resolver=_PUBLIC_RESOLVER,
    )
    return c


def _disabled_client() -> TestClient:
    app = create_app(make_settings())
    c = TestClient(app)
    c.__enter__()
    return c


def _body(name="weather", **over):
    b = dict(name=name, endpoint="https://mcp.example.com/rpc")
    b.update(over)
    return b


# --- Flag OFF: dark surface --------------------------------------------------


def test_disabled_surface_is_404():
    c = _disabled_client()
    assert c.get("/api/agents/mcp-servers").status_code == 404
    assert c.post("/api/agents/mcp-servers", json=_body()).status_code == 404
    assert c.get("/api/agents/mcp-servers/weather").status_code == 404
    assert (
        c.put(
            "/api/agents/mcp-servers/weather", json={"endpoint": "https://x.example/"}
        ).status_code
        == 404
    )
    assert c.delete("/api/agents/mcp-servers/weather").status_code == 404
    assert c.post("/api/agents/mcp-servers/weather/test").status_code == 404


# --- Flag ON: CRUD -----------------------------------------------------------


def test_create_list_get_delete():
    c = _enabled_client()
    created = c.post("/api/agents/mcp-servers", json=_body(displayName="Weather"))
    assert created.status_code == 201, created.text
    doc = created.json()
    assert doc["name"] == "weather"
    assert doc["host"] == "mcp.example.com"
    assert [t["name"] for t in doc["discoveredTools"]] == ["get_forecast"]
    assert "secret" not in doc

    listed = c.get("/api/agents/mcp-servers").json()["servers"]
    assert [s["name"] for s in listed] == ["weather"]

    got = c.get("/api/agents/mcp-servers/weather")
    assert got.status_code == 200
    assert got.json()["name"] == "weather"

    assert c.delete("/api/agents/mcp-servers/weather").status_code == 204
    assert c.get("/api/agents/mcp-servers/weather").status_code == 404


def test_create_validation_error_is_422():
    c = _enabled_client()
    resp = c.post("/api/agents/mcp-servers", json=_body(name="1bad"))
    assert resp.status_code == 422, resp.text


def test_create_duplicate_is_409():
    c = _enabled_client()
    assert c.post("/api/agents/mcp-servers", json=_body()).status_code == 201
    dup = c.post("/api/agents/mcp-servers", json=_body())
    assert dup.status_code == 409, dup.text


def test_create_connection_failure_is_502():
    from ai4ia_api.agents.mcp_servers import McpConnectionError

    c = _enabled_client(error=McpConnectionError("down"))
    resp = c.post("/api/agents/mcp-servers", json=_body())
    assert resp.status_code == 502, resp.text


def test_update_replaces_record():
    c = _enabled_client()
    c.post("/api/agents/mcp-servers", json=_body(description="old"))
    resp = c.put(
        "/api/agents/mcp-servers/weather",
        json={
            "endpoint": "https://mcp.example.com/rpc",
            "description": "new",
            "trusted": True,
        },
    )
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["description"] == "new"
    assert doc["trusted"] is True


def test_test_endpoint_refreshes_tools():
    c = _enabled_client()
    c.post("/api/agents/mcp-servers", json=_body())
    # Swap the connector's canned tools, then re-test.
    c.app.state.mcp_service._connector._tools = [DiscoveredTool(name="new_tool")]
    resp = c.post("/api/agents/mcp-servers/weather/test")
    assert resp.status_code == 200, resp.text
    assert [t["name"] for t in resp.json()["discoveredTools"]] == ["new_tool"]


def test_per_user_isolation():
    c = _enabled_client()
    assert c.post("/api/agents/mcp-servers", json=_body()).status_code == 201
    other = c.get("/api/agents/mcp-servers", headers={"X-Dev-User": "someone-else"})
    assert other.json()["servers"] == []
    # The other user also cannot see the first user's server by name.
    assert (
        c.get(
            "/api/agents/mcp-servers/weather", headers={"X-Dev-User": "someone-else"}
        ).status_code
        == 404
    )
