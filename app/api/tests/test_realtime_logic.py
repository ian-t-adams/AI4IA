"""Voice Live (Phase 10) pure-logic unit tests.

Covers the IO-free helpers in ``routers/realtime.py`` that make the relay
governable: subprotocol credential parsing, the Origin allowlist decision,
realtime deployment resolution, upstream URL/header construction, and the
disabled-by-default config posture. No network, no WebSocket — just functions.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
from ai4ia_api.agents.tool_exec import build_tools
from ai4ia_api.auth.base import AuthCredentials, AuthError, AuthenticatedUser
from ai4ia_api.catalog import DeploymentOption, ModelCatalog, ModelEntry
from ai4ia_api.config import GatewayAuthMode
from ai4ia_api.routers.realtime import (
    BEARER_SUBPROTOCOL,
    DEV_SUBPROTOCOL,
    AuthSubprotocol,
    RealtimeFunctionCall,
    RealtimeResolutionError,
    ToolBridge,
    authenticate_subprotocol,
    build_function_call_output,
    build_session_bridge,
    build_tool_bridge,
    build_upstream_headers,
    build_upstream_url,
    flatten_realtime_tools,
    inject_session_tools,
    origin_allowed,
    parse_auth_subprotocols,
    parse_function_call_done,
    resolve_realtime_deployment,
)
from tests.conftest import make_settings


def _opt(region: str, name: str) -> DeploymentOption:
    return DeploymentOption(region=region, sku="GlobalStandard", deploymentName=name)


def _catalog() -> ModelCatalog:
    return ModelCatalog(
        models=[
            ModelEntry(
                id="gpt-5.2",
                displayName="GPT-5.2",
                category="chat",
                format="OpenAI",
                options=[_opt("eastus2", "gpt-5.2-eastus2")],
            ),
            ModelEntry(
                id="gpt-realtime",
                displayName="GPT Realtime",
                category="realtime",
                format="OpenAI",
                options=[
                    _opt("eastus2", "gpt-realtime-eastus2"),
                    _opt("swedencentral", "gpt-realtime-swedencentral"),
                ],
            ),
            ModelEntry(
                id="gpt-realtime-mini",
                displayName="GPT Realtime Mini",
                category="realtime",
                format="OpenAI",
                options=[_opt("eastus2", "gpt-realtime-mini-eastus2")],
            ),
        ]
    )


# --------------------------------------------------------------------------- #
# parse_auth_subprotocols
# --------------------------------------------------------------------------- #


def test_parse_bearer_subprotocol():
    parsed = parse_auth_subprotocols([BEARER_SUBPROTOCOL, "the.access.token"])
    assert parsed == AuthSubprotocol(marker=BEARER_SUBPROTOCOL, credential="the.access.token")


def test_parse_dev_subprotocol():
    parsed = parse_auth_subprotocols([DEV_SUBPROTOCOL, "alice"])
    assert parsed == AuthSubprotocol(marker=DEV_SUBPROTOCOL, credential="alice")


def test_parse_extra_subprotocols_ignored():
    parsed = parse_auth_subprotocols([BEARER_SUBPROTOCOL, "tok", "something-else"])
    assert parsed is not None
    assert parsed.credential == "tok"


@pytest.mark.parametrize(
    "offered",
    [
        [],
        [BEARER_SUBPROTOCOL],  # marker without credential
        ["unknown-marker", "tok"],  # unrecognized marker
        [BEARER_SUBPROTOCOL, "   "],  # blank credential
        [DEV_SUBPROTOCOL, ""],  # empty credential
    ],
)
def test_parse_rejects_malformed(offered):
    assert parse_auth_subprotocols(offered) is None


# --------------------------------------------------------------------------- #
# origin_allowed
# --------------------------------------------------------------------------- #


def test_origin_allowlist_exact_match():
    allowed = ["https://app.example.com"]
    assert origin_allowed("https://app.example.com", allowed, reflect_when_unset=True)


def test_origin_allowlist_mismatch_rejected():
    allowed = ["https://app.example.com"]
    assert not origin_allowed("https://evil.example.com", allowed, reflect_when_unset=True)


def test_origin_missing_rejected_when_allowlist_set():
    allowed = ["https://app.example.com"]
    assert not origin_allowed(None, allowed, reflect_when_unset=True)


def test_origin_empty_allowlist_reflects_in_dev():
    assert origin_allowed("https://anything", [], reflect_when_unset=True)
    assert origin_allowed(None, [], reflect_when_unset=True)


def test_origin_empty_allowlist_fail_closed_in_prod():
    # Deployed env with no configured allowlist must reject everything.
    assert not origin_allowed("https://anything", [], reflect_when_unset=False)
    assert not origin_allowed(None, [], reflect_when_unset=False)


# --------------------------------------------------------------------------- #
# resolve_realtime_deployment
# --------------------------------------------------------------------------- #


def test_resolve_defaults_to_first_realtime_model():
    model_id, deployment = resolve_realtime_deployment(_catalog(), None, None)
    assert model_id == "gpt-realtime"
    assert deployment.deploymentName == "gpt-realtime-eastus2"


def test_resolve_explicit_realtime_model():
    model_id, deployment = resolve_realtime_deployment(_catalog(), "gpt-realtime-mini", None)
    assert model_id == "gpt-realtime-mini"
    assert deployment.deploymentName == "gpt-realtime-mini-eastus2"


def test_resolve_honors_region():
    _, deployment = resolve_realtime_deployment(_catalog(), "gpt-realtime", "swedencentral")
    assert deployment.region == "swedencentral"
    assert deployment.deploymentName == "gpt-realtime-swedencentral"


def test_resolve_rejects_non_realtime_model():
    with pytest.raises(RealtimeResolutionError):
        resolve_realtime_deployment(_catalog(), "gpt-5.2", None)


def test_resolve_rejects_unknown_model():
    with pytest.raises(RealtimeResolutionError):
        resolve_realtime_deployment(_catalog(), "no-such-model", None)


def test_resolve_no_realtime_models_available():
    chat_only = ModelCatalog(
        models=[
            ModelEntry(
                id="gpt-5.2",
                displayName="GPT-5.2",
                category="chat",
                format="OpenAI",
                options=[_opt("eastus2", "gpt-5.2-eastus2")],
            )
        ]
    )
    with pytest.raises(RealtimeResolutionError):
        resolve_realtime_deployment(chat_only, None, None)


# --------------------------------------------------------------------------- #
# build_upstream_url
# --------------------------------------------------------------------------- #


def test_build_url_https_to_wss():
    url = build_upstream_url("https://apim.example.com/openai", "2025-04-01-preview", "dep-1")
    assert url == (
        "wss://apim.example.com/openai/realtime"
        "?api-version=2025-04-01-preview&deployment=dep-1"
    )


def test_build_url_http_to_ws():
    url = build_upstream_url("http://gateway.test/openai", "2025-04-01-preview", "dep-1")
    assert url.startswith("ws://gateway.test/openai/realtime")


def test_build_url_strips_trailing_slash():
    url = build_upstream_url("https://apim.example.com/openai/", "v1", "dep-1")
    assert "/openai/realtime" in url
    assert "/openai//realtime" not in url


def test_build_url_encodes_deployment_and_version():
    url = build_upstream_url("https://h/openai", "2025-04-01-preview", "dep name/special")
    assert "deployment=dep%20name%2Fspecial" in url


# --------------------------------------------------------------------------- #
# build_upstream_headers
# --------------------------------------------------------------------------- #


def test_headers_api_key_mode():
    headers = build_upstream_headers(GatewayAuthMode.api_key, "secret-key", "corr-1")
    assert headers["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert "Authorization" not in headers
    assert headers["x-correlation-id"] == "corr-1"


def test_headers_bearer_mode():
    headers = build_upstream_headers(GatewayAuthMode.bearer, "the-token", None)
    assert headers["Authorization"] == "Bearer the-token"
    assert "Ocp-Apim-Subscription-Key" not in headers
    assert "x-correlation-id" not in headers


def test_headers_none_mode_has_no_credential():
    headers = build_upstream_headers(GatewayAuthMode.none, None, "corr-2")
    assert "Authorization" not in headers
    assert "Ocp-Apim-Subscription-Key" not in headers
    assert headers["x-correlation-id"] == "corr-2"


def test_headers_api_key_mode_without_key_omits_header():
    headers = build_upstream_headers(GatewayAuthMode.api_key, None, None)
    assert headers == {}


# --------------------------------------------------------------------------- #
# authenticate_subprotocol (provider dispatch + dev-permission gate)
# --------------------------------------------------------------------------- #


class _DummyProvider:
    """Echoes the dev override or the bearer token into the user's subject."""

    async def authenticate(self, credentials: AuthCredentials) -> AuthenticatedUser:
        subject = credentials.header("X-Dev-User") or (credentials.token or "")
        return AuthenticatedUser(
            internal_user_id=f"id::{subject}",
            subject=subject,
            issuer="dummy",
            provider="dummy",
        )


def test_authenticate_dev_subprotocol_when_permitted():
    settings = make_settings(env="local")
    user = asyncio.run(
        authenticate_subprotocol(
            _DummyProvider(), settings, AuthSubprotocol(DEV_SUBPROTOCOL, "alice")
        )
    )
    assert user.subject == "alice"


def test_authenticate_dev_subprotocol_denied_when_not_permitted():
    settings = make_settings(env="dev", allow_dev_auth=False)
    with pytest.raises(AuthError):
        asyncio.run(
            authenticate_subprotocol(
                _DummyProvider(), settings, AuthSubprotocol(DEV_SUBPROTOCOL, "alice")
            )
        )


def test_authenticate_bearer_passes_token_to_provider():
    settings = make_settings(env="local")
    user = asyncio.run(
        authenticate_subprotocol(
            _DummyProvider(), settings, AuthSubprotocol(BEARER_SUBPROTOCOL, "tok-xyz")
        )
    )
    assert user.subject == "tok-xyz"


# --------------------------------------------------------------------------- #
# Governed tool calling: pure helpers (flatten / inject / parse / build).
# --------------------------------------------------------------------------- #


_NESTED_CALC = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate arithmetic.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def test_flatten_realtime_tools_lifts_function_body():
    flat = flatten_realtime_tools([_NESTED_CALC])
    assert flat == [
        {
            "type": "function",
            "name": "calculator",
            "description": "Evaluate arithmetic.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_flatten_realtime_tools_skips_entries_without_function():
    assert flatten_realtime_tools([{"type": "function"}, {"nope": 1}]) == []
    # A function block missing a name is unusable and skipped.
    assert flatten_realtime_tools([{"type": "function", "function": {}}]) == []


def test_inject_session_tools_merges_and_preserves_client_fields():
    frame = json.dumps(
        {"type": "session.update", "session": {"voice": "verse", "instructions": "hi"}}
    )
    tools = [{"type": "function", "name": "calculator"}]
    out = json.loads(inject_session_tools(frame, tools, "auto"))
    assert out["session"]["voice"] == "verse"  # client field preserved
    assert out["session"]["instructions"] == "hi"
    assert out["session"]["tools"] == tools  # relay owns tools
    assert out["session"]["tool_choice"] == "auto"


def test_inject_session_tools_adds_session_when_absent():
    frame = json.dumps({"type": "session.update"})
    out = json.loads(inject_session_tools(frame, [{"type": "function", "name": "x"}], "auto"))
    assert out["session"]["tools"] == [{"type": "function", "name": "x"}]


def test_inject_session_tools_passthrough_for_other_frames():
    frame = json.dumps({"type": "input_audio_buffer.append", "audio": "AAAA"})
    assert inject_session_tools(frame, [{"type": "function", "name": "x"}], "auto") == frame


def test_inject_session_tools_passthrough_when_no_tools():
    frame = json.dumps({"type": "session.update", "session": {"voice": "verse"}})
    assert inject_session_tools(frame, [], "auto") == frame


def test_inject_session_tools_malformed_frame_unchanged():
    # Contains the hint substring but is not valid JSON -> returned verbatim.
    frame = 'not json but "session.update"'
    assert inject_session_tools(frame, [{"type": "function", "name": "x"}], "auto") == frame


def test_parse_function_call_done_valid():
    frame = json.dumps(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "calculator",
            "arguments": '{"expression":"2+3"}',
        }
    )
    call = parse_function_call_done(frame)
    assert call == RealtimeFunctionCall("call_1", "calculator", '{"expression":"2+3"}')


def test_parse_function_call_done_defaults_missing_arguments():
    frame = json.dumps(
        {"type": "response.function_call_arguments.done", "call_id": "c", "name": "n"}
    )
    call = parse_function_call_done(frame)
    assert call is not None and call.arguments == "{}"


def test_parse_function_call_done_other_frame_is_none():
    assert parse_function_call_done(json.dumps({"type": "response.audio.delta"})) is None


@pytest.mark.parametrize(
    "frame",
    [
        'malformed "response.function_call_arguments.done"',  # hint but not JSON
        json.dumps(
            {"type": "response.function_call_arguments.done", "name": "n"}
        ),  # missing call_id
        json.dumps(
            {"type": "response.function_call_arguments.done", "call_id": "c"}
        ),  # missing name
    ],
)
def test_parse_function_call_done_malformed_is_none(frame):
    assert parse_function_call_done(frame) is None


def test_build_function_call_output_shape():
    out = json.loads(build_function_call_output("call_9", '{"result":5}'))
    assert out["type"] == "conversation.item.create"
    assert out["item"] == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": '{"result":5}',
    }


# --------------------------------------------------------------------------- #
# ToolBridge: governed execution round-trip (reuses the real builtins).
# --------------------------------------------------------------------------- #


def _calc_done_frame(call_id: str = "call_1", expression: str = "2+3") -> str:
    return json.dumps(
        {
            "type": "response.function_call_arguments.done",
            "call_id": call_id,
            "name": "calculator",
            "arguments": json.dumps({"expression": expression}),
        }
    )


def _enabled_bridge() -> ToolBridge:
    state = SimpleNamespace()
    settings = make_settings(realtime_tools_enabled=True)
    state.tool_registry, state.tool_executor = build_tools()
    return build_tool_bridge(state, settings, "corr-1")


def test_build_tool_bridge_inert_when_tools_disabled():
    state = SimpleNamespace()
    state.tool_registry, state.tool_executor = build_tools()
    bridge = build_tool_bridge(state, make_settings(realtime_tools_enabled=False), "c")
    assert bridge.enabled is False
    assert bridge.tools == []


def test_build_tool_bridge_advertises_builtins_when_enabled():
    bridge = _enabled_bridge()
    assert bridge.enabled is True
    names = {t["name"] for t in bridge.tools}
    assert {"calculator", "get_current_time"} <= names
    # Flat realtime schema: name at the top level, no nested "function" wrapper.
    assert all(t["type"] == "function" and "function" not in t for t in bridge.tools)


def test_tool_bridge_executes_calculator_round_trip():
    bridge = _enabled_bridge()
    frames = asyncio.run(bridge.handle_upstream_frame(_calc_done_frame()))
    assert len(frames) == 2
    output_frame = json.loads(frames[0])
    assert output_frame["item"]["call_id"] == "call_1"
    result = json.loads(output_frame["item"]["output"])
    assert result["result"] == 5
    # Second frame nudges the model to speak the tool result.
    assert json.loads(frames[1]) == {"type": "response.create"}


def test_tool_bridge_rewrites_session_update_with_tools():
    bridge = _enabled_bridge()
    out = json.loads(bridge.rewrite_client_frame(json.dumps({"type": "session.update"})))
    assert {t["name"] for t in out["session"]["tools"]} >= {"calculator"}
    assert out["session"]["tool_choice"] == "auto"


def test_tool_bridge_unknown_tool_returns_error_not_execution():
    bridge = _enabled_bridge()
    frame = json.dumps(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "c",
            "name": "definitely_not_a_tool",
            "arguments": "{}",
        }
    )
    frames = asyncio.run(bridge.handle_upstream_frame(frame))
    assert len(frames) == 2
    output = json.loads(json.loads(frames[0])["item"]["output"])
    assert "error" in output and "not permitted" in output["error"]


def test_tool_bridge_invalid_arguments_return_error():
    bridge = _enabled_bridge()
    frame = json.dumps(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "c",
            "name": "calculator",
            "arguments": "not-json",
        }
    )
    frames = asyncio.run(bridge.handle_upstream_frame(frame))
    output = json.loads(json.loads(frames[0])["item"]["output"])
    assert "error" in output


def test_tool_bridge_disabled_is_passthrough():
    state = SimpleNamespace()
    state.tool_registry, state.tool_executor = build_tools()
    bridge = build_tool_bridge(state, make_settings(realtime_tools_enabled=False), "c")
    frame = json.dumps({"type": "session.update", "session": {"voice": "verse"}})
    assert bridge.rewrite_client_frame(frame) == frame
    assert asyncio.run(bridge.handle_upstream_frame(_calc_done_frame())) == []


# --------------------------------------------------------------------------- #
# Agent-aware live voice: persona injection + per-agent tool scoping.
# --------------------------------------------------------------------------- #


class _FakeAgentService:
    """Returns a fixed composed catalog (the store layer is irrelevant to tests)."""

    def __init__(self, catalog: AgentCatalog) -> None:
        self._catalog = catalog

    async def catalog_for(self, user_id: str, curated: AgentCatalog) -> AgentCatalog:
        return self._catalog


class _BrokenAgentService:
    async def catalog_for(self, user_id: str, curated: AgentCatalog) -> AgentCatalog:
        raise RuntimeError("agent store down")


def _agent_state(*specs: AgentSpec, service=None) -> SimpleNamespace:
    state = SimpleNamespace()
    state.tool_registry, state.tool_executor = build_tools()
    catalog = AgentCatalog(agents=list(specs))
    state.agents = catalog
    state.agent_service = service if service is not None else _FakeAgentService(catalog)
    return state


def _spec(name: str, *, tools: list[str], prompt: str = "PERSONA", enabled: bool = True) -> AgentSpec:
    return AgentSpec(
        name=name,
        displayName=name.title(),
        description="d",
        systemPrompt=prompt,
        tools=tools,
        enabled=enabled,
    )


_USER = SimpleNamespace(internal_user_id="u1")


def test_inject_session_tools_injects_instructions_with_tools():
    frame = json.dumps({"type": "session.update", "session": {"voice": "verse"}})
    out = json.loads(
        inject_session_tools(
            frame, [{"type": "function", "name": "calculator"}], "auto", instructions="P"
        )
    )
    assert out["session"]["voice"] == "verse"
    assert out["session"]["instructions"] == "P"  # relay owns instructions when bound
    assert out["session"]["tool_choice"] == "auto"


def test_inject_session_tools_injects_instructions_only_when_no_tools():
    frame = json.dumps({"type": "session.update", "session": {"voice": "verse"}})
    out = json.loads(inject_session_tools(frame, [], "auto", instructions="P"))
    assert out["session"]["instructions"] == "P"
    # Persona-only: tools/tool_choice are NOT touched when no tools are advertised.
    assert "tools" not in out["session"]
    assert "tool_choice" not in out["session"]


def test_inject_session_tools_leaves_client_instructions_when_none():
    frame = json.dumps({"type": "session.update", "session": {"instructions": "client"}})
    out = json.loads(
        inject_session_tools(frame, [{"type": "function", "name": "x"}], "auto")
    )
    assert out["session"]["instructions"] == "client"  # untouched for generic sessions


def test_tool_bridge_persona_only_rewrites_instructions_without_tools():
    state = SimpleNamespace()
    state.tool_registry, state.tool_executor = build_tools()
    bridge = build_tool_bridge(
        state, make_settings(realtime_tools_enabled=False), "c", instructions="P"
    )
    assert bridge.enabled is False  # no tools -> no in-process execution
    out = json.loads(
        bridge.rewrite_client_frame(
            json.dumps({"type": "session.update", "session": {"voice": "x"}})
        )
    )
    assert out["session"]["instructions"] == "P"
    assert "tools" not in out["session"]


def test_build_tool_bridge_scopes_to_tool_names():
    state = SimpleNamespace()
    state.tool_registry, state.tool_executor = build_tools()
    bridge = build_tool_bridge(
        state, make_settings(realtime_tools_enabled=True), "c", tool_names=["calculator"]
    )
    assert {t["name"] for t in bridge.tools} == {"calculator"}


def test_build_session_bridge_agent_scopes_tools_and_persona():
    state = _agent_state(_spec("analyst", tools=["calculator"], prompt="ANALYST"))
    bridge = asyncio.run(
        build_session_bridge(
            state,
            make_settings(realtime_tools_enabled=True),
            "c",
            user=_USER,
            agent_name="analyst",
        )
    )
    assert bridge.instructions == "ANALYST"
    # Scoped to the agent's allowlist: calculator only, NOT get_current_time.
    assert {t["name"] for t in bridge.tools} == {"calculator"}


def test_build_session_bridge_agent_persona_without_tools_when_tools_disabled():
    state = _agent_state(_spec("coder", tools=[], prompt="CODER"))
    bridge = asyncio.run(
        build_session_bridge(
            state,
            make_settings(realtime_tools_enabled=False),
            "c",
            user=_USER,
            agent_name="coder",
        )
    )
    assert bridge.instructions == "CODER"
    assert bridge.tools == []  # persona-only when realtime tools are off


def test_build_session_bridge_unknown_agent_falls_back_to_generic():
    state = _agent_state(_spec("analyst", tools=["calculator"]))
    bridge = asyncio.run(
        build_session_bridge(
            state,
            make_settings(realtime_tools_enabled=True),
            "c",
            user=_USER,
            agent_name="nope",
        )
    )
    assert bridge.instructions is None
    assert {t["name"] for t in bridge.tools} >= {"calculator", "get_current_time"}


def test_build_session_bridge_disabled_agent_falls_back_to_generic():
    state = _agent_state(_spec("off", tools=["calculator"], enabled=False))
    bridge = asyncio.run(
        build_session_bridge(
            state,
            make_settings(realtime_tools_enabled=True),
            "c",
            user=_USER,
            agent_name="off",
        )
    )
    assert bridge.instructions is None
    assert {t["name"] for t in bridge.tools} >= {"get_current_time"}


def test_build_session_bridge_no_agent_is_generic():
    state = _agent_state(_spec("analyst", tools=["calculator"]))
    bridge = asyncio.run(
        build_session_bridge(
            state,
            make_settings(realtime_tools_enabled=True),
            "c",
            user=_USER,
            agent_name=None,
        )
    )
    assert bridge.instructions is None
    assert {t["name"] for t in bridge.tools} >= {"calculator", "get_current_time"}


def test_build_session_bridge_store_error_falls_back_to_generic():
    state = _agent_state(service=_BrokenAgentService())
    bridge = asyncio.run(
        build_session_bridge(
            state,
            make_settings(realtime_tools_enabled=True),
            "c",
            user=_USER,
            agent_name="analyst",
        )
    )
    assert bridge.instructions is None  # fail OPEN to the generic assistant
    assert bridge.tools  # builtins still offered
