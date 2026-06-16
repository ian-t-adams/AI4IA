"""Tests for per-turn MCP tool execution (Phase 12B Increment B).

Covers the governed :class:`ToolDefinition` builder (:mod:`ai4ia_api.agents.mcp_execution`)
both in isolation (handler success/error, secret resolution, SSRF re-validation, the
per-turn call budget) and end-to-end through the REAL
:func:`~ai4ia_api.agents.runtime.run_agent_turn` (trusted runs without approval,
untrusted is denied without approval and allowed with it, two servers attached in one
turn both run with no cross-denial, and secret-bearing I/O is redacted in the trace).

Nothing touches DNS or a live server: the ``FakeMcpConnector`` returns canned tool
results and a stub resolver yields a public IP unless a test simulates a rebind.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ai4ia_api.agents.mcp_client import (
    FakeMcpConnector,
    HttpxMcpConnector,
    McpAuth,
    McpToolResult,
)
from ai4ia_api.agents.mcp_execution import (
    MAX_MCP_TOOL_CALLS_PER_TURN,
    build_mcp_tool_definitions,
    build_mcp_turn_tools,
    trusted_tool_names,
)
from ai4ia_api.agents.mcp_secrets import InMemoryMcpSecretStore
from ai4ia_api.agents.mcp_servers import (
    DiscoveredTool,
    McpAuthMode,
    UserMcpServer,
    UserMcpServerCreate,
)
from ai4ia_api.agents.mcp_service import McpServerService
from ai4ia_api.agents.mcp_store import InMemoryUserMcpServerStore
from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, ToolExecutionError
from ai4ia_api.agents.tools import DenyReason

_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731 - terse test stub
_REBIND_RESOLVER = lambda _host: ["127.0.0.1"]  # noqa: E731 - host now resolves internal


def _tool(name: str, schema: dict | None = None) -> DiscoveredTool:
    return DiscoveredTool(name=name, description=f"{name} tool", inputSchema=schema or {})


def _server(
    name: str,
    *,
    host: str | None = None,
    tools: list[DiscoveredTool] | None = None,
    trusted: bool = False,
    auth_mode: McpAuthMode = McpAuthMode.none,
    secret_ref: str | None = None,
    enabled: bool = True,
) -> UserMcpServer:
    host = host or f"{name}.example.com"
    return UserMcpServer(
        id=name,
        userId="u1",
        name=name,
        displayName=name,
        endpoint=f"https://{host}/rpc",
        host=host,
        authMode=auth_mode,
        trusted=trusted,
        enabled=enabled,
        secretRef=secret_ref,
        discoveredTools=tools or [_tool("do_thing")],
    )


class _Secrets:
    """Minimal :class:`SecretResolver` mapping server name -> secret."""

    def __init__(self, by_name: dict[str, str] | None = None) -> None:
        self._by_name = by_name or {}

    async def secret_for(self, server: UserMcpServer) -> str | None:
        return self._by_name.get(server.name)


# --- ScriptedGateway (mirrors test_agent_runtime) ----------------------------


def _assistant_tool_calls(calls: list[tuple[str, str, str]]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                        for cid, name, args in calls
                    ],
                }
            }
        ]
    }


def _assistant_text(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class ScriptedGateway:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls.append({"messages": [dict(m) for m in messages], "params": params or {}})
        if not self._responses:
            return _assistant_text("(no more scripted responses)")
        return self._responses.pop(0)


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are the analyst."},
        {"role": "user", "content": "Use the tool."},
    ]


# --- Builder unit tests ------------------------------------------------------


def test_build_returns_none_when_no_mcp_tools_attached():
    servers = [_server("weather")]
    assert (
        build_mcp_turn_tools(
            servers=servers,
            attached_tool_names=["calculator"],
            secrets=_Secrets(),
            connector=FakeMcpConnector(),
        )
        is None
    )


def test_build_merges_builtins_with_attached_mcp_tools():
    servers = [_server("weather", trusted=True, tools=[_tool("forecast")])]
    built = build_mcp_turn_tools(
        servers=servers,
        attached_tool_names=["calculator", "mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=FakeMcpConnector(),
    )
    assert built is not None
    registry, executor, ctx = built
    # Built-ins are present alongside the MCP tool (fresh, not the app singletons).
    assert registry.get("calculator") is not None
    assert registry.get("mcp:weather/forecast") is not None
    assert executor.get("mcp:weather/forecast") is not None
    # Egress check is skipped (empty target_hosts); trusted server -> auto-approved.
    assert ctx.target_hosts == frozenset()
    assert ctx.approvals == frozenset({"mcp:weather/forecast"})


def test_build_only_includes_attached_owned_tools():
    servers = [_server("weather", tools=[_tool("forecast"), _tool("alerts")])]
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=FakeMcpConnector(),
        budget={"used": 0},
    )
    assert [d.spec.name for d in defs] == ["mcp:weather/forecast"]


def test_trusted_tool_names_only_for_trusted_servers():
    servers = [
        _server("a", trusted=True, tools=[_tool("x")]),
        _server("b", trusted=False, tools=[_tool("y")]),
    ]
    names = trusted_tool_names(servers, ["mcp:a/x", "mcp:b/y"])
    assert names == frozenset({"mcp:a/x"})


def test_mcp_tool_uses_inputschema_as_parameters():
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    servers = [_server("weather", tools=[_tool("forecast", schema)])]
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=FakeMcpConnector(),
        budget={"used": 0},
    )
    assert defs[0].parameters == schema


def test_empty_inputschema_falls_back_to_object_schema():
    servers = [_server("weather", tools=[_tool("forecast", {})])]
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=FakeMcpConnector(),
        budget={"used": 0},
    )
    assert defs[0].parameters == {"type": "object", "properties": {}}


# --- Handler unit tests ------------------------------------------------------


async def _run_handler(defn, args, ctx=None):
    return await defn.handler(args, ctx or ToolContext())


async def test_handler_success_returns_bounded_dict():
    servers = [_server("weather", tools=[_tool("forecast")])]
    connector = FakeMcpConnector(
        call_results={"forecast": McpToolResult(content="Sunny", is_error=False)}
    )
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=connector,
        resolver=_PUBLIC_RESOLVER,
        budget={"used": 0},
    )
    out = await _run_handler(defs[0], {"city": "SEA"})
    assert out == {"content": "Sunny", "isError": False}
    # The connector was called against the server's own endpoint + tool name.
    endpoint, tool, args, _auth = connector.tool_calls[0]
    assert endpoint == "https://weather.example.com/rpc"
    assert tool == "forecast"
    assert args == {"city": "SEA"}


async def test_handler_surfaces_remote_error_flag():
    servers = [_server("weather", tools=[_tool("forecast")])]
    connector = FakeMcpConnector(
        call_results={"forecast": McpToolResult(content="upstream 500", is_error=True)}
    )
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=connector,
        resolver=_PUBLIC_RESOLVER,
        budget={"used": 0},
    )
    out = await _run_handler(defs[0], {})
    assert out == {"content": "upstream 500", "isError": True}


async def test_handler_resolves_secret_and_passes_auth():
    servers = [
        _server(
            "weather",
            auth_mode=McpAuthMode.bearer,
            secret_ref="ref-1",
            tools=[_tool("forecast")],
        )
    ]
    connector = FakeMcpConnector()
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets({"weather": "s3cr3t"}),
        connector=connector,
        resolver=_PUBLIC_RESOLVER,
        budget={"used": 0},
    )
    await _run_handler(defs[0], {})
    _endpoint, _called, _args, auth = connector.tool_calls[0]
    assert isinstance(auth, McpAuth)
    assert auth.mode is McpAuthMode.bearer
    assert auth.secret == "s3cr3t"


async def test_handler_resolves_secret_from_inmemory_secret_store():
    """Secret resolution through the REAL service + InMemoryMcpSecretStore."""
    secret_store = InMemoryMcpSecretStore()
    svc = McpServerService(
        InMemoryUserMcpServerStore(),
        connector=FakeMcpConnector(tools=[_tool("forecast")]),
        secret_store=secret_store,
        resolver=_PUBLIC_RESOLVER,
    )
    await svc.create(
        "u1",
        UserMcpServerCreate(
            name="weather",
            endpoint="https://weather.example.com/rpc",
            authMode=McpAuthMode.api_key,
            secret="top-secret-key",
        ),
    )
    servers = await svc.list_for("u1")
    exec_connector = FakeMcpConnector()
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=svc,  # the service resolves the durable secret
        connector=exec_connector,
        resolver=_PUBLIC_RESOLVER,
        budget={"used": 0},
    )
    await _run_handler(defs[0], {})
    _endpoint, _called, _args, auth = exec_connector.tool_calls[0]
    assert auth.mode is McpAuthMode.api_key
    assert auth.secret == "top-secret-key"


async def test_handler_rejects_dns_rebind_at_call_time():
    servers = [_server("weather", tools=[_tool("forecast")])]
    connector = FakeMcpConnector()
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=connector,
        resolver=_REBIND_RESOLVER,  # host now resolves to a private/internal IP
        budget={"used": 0},
    )
    with pytest.raises(ToolExecutionError):
        await _run_handler(defs[0], {})
    # SSRF guard fired BEFORE any outbound tool call was attempted.
    assert connector.tool_calls == []


async def test_handler_enforces_per_turn_budget():
    servers = [_server("weather", tools=[_tool("forecast")])]
    connector = FakeMcpConnector()
    budget = {"used": 0}
    defs = build_mcp_tool_definitions(
        servers,
        attached_tool_names=["mcp:weather/forecast"],
        secrets=_Secrets(),
        connector=connector,
        resolver=_PUBLIC_RESOLVER,
        budget=budget,
        max_calls=1,
    )
    await _run_handler(defs[0], {})  # consumes the only allowed call
    with pytest.raises(ToolExecutionError):
        await _run_handler(defs[0], {})
    assert len(connector.tool_calls) == 1


def test_default_budget_is_bounded():
    assert MAX_MCP_TOOL_CALLS_PER_TURN >= 1


# --- End-to-end through run_agent_turn ---------------------------------------


async def _run_turn(servers, attached, connector, gateway, *, approved=()):
    built = build_mcp_turn_tools(
        servers=servers,
        attached_tool_names=attached,
        secrets=_Secrets(),
        connector=connector,
        resolver=_PUBLIC_RESOLVER,
        approved=approved,
    )
    assert built is not None
    registry, executor, ctx = built
    return await run_agent_turn(
        deployment="dep",
        messages=_messages(),
        tool_names=list(attached),
        gateway=gateway,
        registry=registry,
        executor=executor,
        ctx=ctx,
    )


async def test_trusted_server_tool_runs_without_approval():
    servers = [_server("weather", trusted=True, tools=[_tool("forecast")])]
    connector = FakeMcpConnector(
        call_results={"forecast": McpToolResult(content="Clear skies")}
    )
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls([("c1", "mcp:weather/forecast", "{}")]),
            _assistant_text("It will be clear."),
        ]
    )
    result = await _run_turn(servers, ["mcp:weather/forecast"], connector, gateway)
    assert result.text == "It will be clear."
    kinds = [s.kind for s in result.steps]
    assert "tool_result" in kinds
    step = next(s for s in result.steps if s.kind == "tool_result")
    assert step.result == {"content": "Clear skies", "isError": False}
    assert len(connector.tool_calls) == 1


async def test_untrusted_server_tool_denied_without_approval():
    servers = [_server("weather", trusted=False, tools=[_tool("forecast")])]
    connector = FakeMcpConnector()
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls([("c1", "mcp:weather/forecast", "{}")]),
            _assistant_text("Sorry, blocked."),
        ]
    )
    result = await _run_turn(servers, ["mcp:weather/forecast"], connector, gateway)
    denied = [s for s in result.steps if s.kind == "tool_denied"]
    assert denied and denied[0].detail == DenyReason.approval_required.value
    # The remote tool was never actually invoked.
    assert connector.tool_calls == []


async def test_untrusted_server_tool_allowed_with_approval():
    servers = [_server("weather", trusted=False, tools=[_tool("forecast")])]
    connector = FakeMcpConnector(
        call_results={"forecast": McpToolResult(content="Approved run")}
    )
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls([("c1", "mcp:weather/forecast", "{}")]),
            _assistant_text("Done."),
        ]
    )
    result = await _run_turn(
        servers,
        ["mcp:weather/forecast"],
        connector,
        gateway,
        approved=["mcp:weather/forecast"],
    )
    kinds = [s.kind for s in result.steps]
    assert "tool_result" in kinds and "tool_denied" not in kinds
    assert len(connector.tool_calls) == 1


async def test_two_servers_both_run_no_cross_denial():
    servers = [
        _server("a", host="a.example.com", trusted=True, tools=[_tool("ta")]),
        _server("b", host="b.example.com", trusted=True, tools=[_tool("tb")]),
    ]
    connector = FakeMcpConnector(
        call_results={
            "ta": McpToolResult(content="from-a"),
            "tb": McpToolResult(content="from-b"),
        }
    )
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls(
                [("c1", "mcp:a/ta", "{}"), ("c2", "mcp:b/tb", "{}")]
            ),
            _assistant_text("Both done."),
        ]
    )
    result = await _run_turn(servers, ["mcp:a/ta", "mcp:b/tb"], connector, gateway)
    assert result.text == "Both done."
    results = {
        s.tool: s.result for s in result.steps if s.kind == "tool_result"
    }
    # Neither tool was egress-denied because of the OTHER server's host.
    assert results["mcp:a/ta"] == {"content": "from-a", "isError": False}
    assert results["mcp:b/tb"] == {"content": "from-b", "isError": False}
    assert not [s for s in result.steps if s.kind == "tool_denied"]


async def test_secret_bearing_io_is_redacted_in_trace():
    schema = {"type": "object", "properties": {"api_key": {"type": "string"}}}
    servers = [_server("weather", trusted=True, tools=[_tool("forecast", schema)])]
    connector = FakeMcpConnector(
        call_results={
            "forecast": McpToolResult(content="token=abcdEFGH1234567890abcdEFGH1234567890")
        }
    )
    secret_arg = "supersecretvalue1234567890ABCDEFGHIJ"
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls(
                [("c1", "mcp:weather/forecast", json.dumps({"api_key": secret_arg}))]
            ),
            _assistant_text("ok"),
        ]
    )
    result = await _run_turn(servers, ["mcp:weather/forecast"], connector, gateway)
    step = next(s for s in result.steps if s.kind == "tool_result")
    # The secret-named argument is masked wholesale; the long token in the result
    # is redacted — neither plaintext appears in the trace.
    assert secret_arg not in json.dumps(step.arguments)
    assert "abcdEFGH1234567890abcdEFGH1234567890" not in json.dumps(step.result)


async def test_disabled_server_tool_is_denied():
    servers = [_server("weather", trusted=True, enabled=False, tools=[_tool("forecast")])]
    connector = FakeMcpConnector()
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls([("c1", "mcp:weather/forecast", "{}")]),
            _assistant_text("blocked"),
        ]
    )
    result = await _run_turn(servers, ["mcp:weather/forecast"], connector, gateway)
    denied = [s for s in result.steps if s.kind == "tool_denied"]
    assert denied and denied[0].detail == DenyReason.disabled.value
    assert connector.tool_calls == []


# --- Executed redaction + 8KB-truncation seam over a real content array ------
#
# The other end-to-end tests use FakeMcpConnector (canned McpToolResult), which
# bypasses the wire-level content-block parsing. This one drives a REAL MCP
# ``tools/call`` JSON-RPC response (a ``result.content`` array, NOT ``result.tools``)
# through HttpxMcpConnector + httpx.MockTransport and the REAL run_agent_turn, so the
# full chain is exercised: transport -> _parse_tool_result/_content_to_text ->
# handler {content,isError} -> runtime redact_obj (trace) + _truncate (model turn).
# It proves both governance controls actually fire on parsed remote content.

_REDACTABLE_TOKEN = "abcdEFGH1234567890abcdEFGH1234567890"  # 36 chars -> matches _LONG_TOKEN_RE
_TRUNCATE_SUFFIX = "...[truncated]"
_RUNTIME_RESULT_CAP = 8192


def _httpx_call_tool_connector(content_blocks: list[dict]) -> HttpxMcpConnector:
    """HttpxMcpConnector whose MockTransport answers the handshake then returns a
    ``tools/call`` result carrying ``content_blocks`` (a real MCP content array)."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content).get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
                headers={"Mcp-Session-Id": "sess"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"content": content_blocks, "isError": False},
                },
            )
        raise AssertionError(f"unexpected method {method}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxMcpConnector(client=client)


async def test_tools_call_content_is_redacted_and_truncated_through_runtime():
    # A content array with (a) a redactable high-entropy token and (b) an oversized
    # text block that pushes the JSON-encoded result well past the 8KB runtime cap.
    oversized = "X" * (_RUNTIME_RESULT_CAP * 2)
    connector = _httpx_call_tool_connector(
        [
            {"type": "text", "text": f"token={_REDACTABLE_TOKEN}"},
            {"type": "text", "text": oversized},
        ]
    )
    servers = [_server("weather", trusted=True, tools=[_tool("forecast")])]
    gateway = ScriptedGateway(
        [
            _assistant_tool_calls([("c1", "mcp:weather/forecast", "{}")]),
            _assistant_text("done"),
        ]
    )

    result = await _run_turn(servers, ["mcp:weather/forecast"], connector, gateway)

    # Redaction fired: the trace step's result ran through redact_obj, so the raw
    # token never appears and the redaction placeholder is present.
    step = next(s for s in result.steps if s.kind == "tool_result")
    trace_blob = json.dumps(step.result)
    assert _REDACTABLE_TOKEN not in trace_blob
    assert "***REDACTED***" in step.result["content"]

    # 8KB truncation applied: the tool message fed back to the model (seen by the
    # gateway on the follow-up completion) is capped and flagged as truncated.
    final_messages = gateway.calls[-1]["messages"]
    tool_msg = next(m for m in final_messages if m.get("role") == "tool")
    content = tool_msg["content"]
    assert content.endswith(_TRUNCATE_SUFFIX)
    assert len(content.encode("utf-8")) <= _RUNTIME_RESULT_CAP + len(_TRUNCATE_SUFFIX)
    # The original oversized payload was far larger than the cap.
    assert len(oversized) > _RUNTIME_RESULT_CAP
