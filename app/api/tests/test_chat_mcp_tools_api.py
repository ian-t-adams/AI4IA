"""End-to-end tests for per-turn MCP tool execution through POST /api/chat.

These exercise the *flag-gated, best-effort* wiring in the chat router: with the
feature ON, a user who has registered an MCP server and attached one of its tools
to a user agent can have that remote tool invoked inside a chat turn — governed by
the same registry/redaction machinery as the built-ins (the merged registry/executor
is built per turn; the app singletons are never mutated). With the feature OFF the
MCP path is dark (``app.state.mcp_service`` is ``None``) and the built-in agent tool
path is byte-for-byte unchanged.

Nothing touches DNS or a live server: a ``FakeMcpConnector`` supplies both the
discovered tools (for registration) and the canned ``tools/call`` results (for
execution), and a public-IP stub resolver satisfies the SSRF guard.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpToolResult
from ai4ia_api.agents.mcp_secrets import InMemoryMcpSecretStore
from ai4ia_api.agents.mcp_servers import DiscoveredTool
from ai4ia_api.agents.mcp_service import McpServerService
from ai4ia_api.agents.mcp_store import InMemoryUserMcpServerStore
from ai4ia_api.main import create_app
from tests.conftest import make_settings

_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub
_FORECAST = DiscoveredTool(name="forecast", description="Forecast", inputSchema={})


def _enabled_client(connector: FakeMcpConnector) -> TestClient:
    app = create_app(make_settings(custom_tools_enabled=True))
    c = TestClient(app)
    # Enter the lifespan once, then swap in a deterministic MCP service whose
    # connector is shared between discovery (registration) and execution (chat).
    c.__enter__()
    c.app.state.mcp_service = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=connector,
        secret_store=InMemoryMcpSecretStore(),
        resolver=_PUBLIC_RESOLVER,
    )
    return c


def _register_server(c: TestClient, *, trusted: bool) -> None:
    resp = c.post(
        "/api/agents/mcp-servers",
        json={
            "name": "weather",
            "endpoint": "https://mcp.example.com/rpc",
            "trusted": trusted,
        },
    )
    assert resp.status_code == 201, resp.text
    assert [t["name"] for t in resp.json()["discoveredTools"]] == ["forecast"]


def _create_agent(c: TestClient) -> None:
    resp = c.post(
        "/api/agents",
        json={
            "name": "weatherbot",
            "systemPrompt": "You report the weather.",
            "tools": ["mcp:weather/forecast"],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["tools"] == ["mcp:weather/forecast"]


def _create_session(c: TestClient, model="gpt-5.2") -> str:
    resp = c.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class _McpToolThenAnswerGateway:
    """First completion asks for the MCP tool; the second returns prose once it
    sees the tool result fed back."""

    def __init__(self) -> None:
        self.calls = 0
        self.saw_tool_result = False

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        if self.calls == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "mcp:weather/forecast",
                                        "arguments": json.dumps({}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        if any(
            m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in messages
        ):
            self.saw_tool_result = True
        return {"choices": [{"message": {"role": "assistant", "content": "Clear skies."}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):  # pragma: no cover
        raise AssertionError("tool-enabled agent turns must not use the streaming path")


# --- Flag ON -----------------------------------------------------------------


def test_trusted_mcp_tool_runs_in_chat_turn():
    connector = FakeMcpConnector(
        [_FORECAST],
        call_results={"forecast": McpToolResult(content="Sunny and 75F")},
    )
    c = _enabled_client(connector)
    try:
        _register_server(c, trusted=True)
        _create_agent(c)
        gw = _McpToolThenAnswerGateway()
        c.app.state.gateway = gw
        sid = _create_session(c)

        resp = c.post(
            "/api/chat",
            json={"sessionId": sid, "content": "@weatherbot forecast?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "Clear skies."
        assert gw.calls == 2
        assert gw.saw_tool_result is True
        # The remote tool was actually invoked against its own endpoint.
        assert len(connector.tool_calls) == 1
        endpoint, tool, _args, _auth = connector.tool_calls[0]
        assert endpoint == "https://mcp.example.com/rpc"
        assert tool == "forecast"
    finally:
        c.__exit__(None, None, None)


def test_untrusted_mcp_tool_is_denied_without_approval_in_chat_turn():
    connector = FakeMcpConnector(
        [_FORECAST],
        call_results={"forecast": McpToolResult(content="should not run")},
    )
    c = _enabled_client(connector)
    try:
        _register_server(c, trusted=False)
        _create_agent(c)
        gw = _McpToolThenAnswerGateway()
        c.app.state.gateway = gw
        sid = _create_session(c)

        resp = c.post(
            "/api/chat",
            json={"sessionId": sid, "content": "@weatherbot forecast?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        # The model still produced a final answer, but the untrusted tool was
        # denied (approval required) and never actually invoked.
        assert gw.calls == 2
        assert connector.tool_calls == []
    finally:
        c.__exit__(None, None, None)


# --- Flag OFF: MCP path is dark ----------------------------------------------


def test_disabled_feature_leaves_mcp_path_dark(client):
    """With custom tools off, ``mcp_service`` is None so the chat router never
    enters the MCP block, and an agent cannot even attach an ``mcp:*`` tool."""
    assert getattr(client.app.state, "mcp_service", None) is None

    # An agent cannot attach an mcp:* tool when the feature is off (the router
    # supplies no owned MCP names, so validation rejects it exactly as before).
    rejected = client.post(
        "/api/agents",
        json={
            "name": "weatherbot",
            "systemPrompt": "hi",
            "tools": ["mcp:weather/forecast"],
        },
    )
    assert rejected.status_code == 422, rejected.text
