"""Claude Opus 5.5's adaptive text-only profile at the actual API, runtime and transport seams.

Every refusal is paired with the same request on the thinking-disabled Opus 5 profile,
which does reach tools, so a refusal cannot pass because the fixture never worked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpResourceResult
from ai4ia_api.agents.mcp_servers import DiscoveredResource
from ai4ia_api.agents.official_mcp_service import OfficialMcpService
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.official_mcp_catalog import OfficialMcpCatalog, OfficialMcpServer
from tests.conftest import make_settings
from tests.test_auth_entra import BARE_GUID, ISSUER, KID, TENANT, _new_keypair, _provider
from tests.test_publication_execution_api import SUBJECTS

ADAPTIVE = "claude-opus-5-5"
DISABLED = "claude-opus-5"
NON_TOOL_SOURCE = "DeepSeek-V3.2"
THINKING = "PRIVATE-THINKING-7f3a91"
SIGNATURE = "PRIVATE-SIGNATURE-19c2d4"
REDACTED = "PRIVATE-REDACTED-55e1b0"
HIDDEN = (THINKING, SIGNATURE, REDACTED)
VISIBLE = "Visible adaptive answer."
_SKILL_URI = "skill://evidence-review/SKILL.md?version=7"


def _message(*, text: str | None = VISIBLE, stop_reason: str = "end_turn") -> dict:
    content: list[dict] = [
        {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
        {"type": "redacted_thinking", "data": REDACTED},
    ]
    if text is not None:
        content.append({"type": "text", "text": text})
    return {
        "id": "msg_synthetic", "type": "message", "role": "assistant", "content": content,
        "stop_reason": stop_reason, "usage": {"input_tokens": 12, "output_tokens": 30},
    }


def _stream(*, text: str | None = VISIBLE, stop_reason: str = "end_turn") -> str:
    events = [
        {"type": "message_start", "message": {
            "id": "msg_synthetic", "usage": {"input_tokens": 12, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "thinking", "thinking": THINKING, "signature": "",
        }},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": THINKING}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": SIGNATURE}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "redacted_thinking", "data": REDACTED}},
        {"type": "content_block_stop", "index": 1},
    ]
    if text is not None:
        events += [
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 2},
        ]
    events += [
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


class Provider:
    """Records Claude Messages bodies; any other model call gets an inert reply."""

    def __init__(self, *, text: str | None = VISIBLE, stop_reason: str = "end_turn") -> None:
        self.claude: list[dict] = []
        self.requests = 0
        self.text = text
        self.stop_reason = stop_reason

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        body = json.loads(request.content)
        if "/deployments/claude-" not in request.url.path:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "{}"}}]})
        self.claude.append(body)
        if body.get("stream"):
            return httpx.Response(
                200, text=_stream(text=self.text, stop_reason=self.stop_reason),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=_message(text=self.text, stop_reason=self.stop_reason))


@contextmanager
def claude_client(
    *, provider: Provider | None = None, **overrides,
) -> Iterator[tuple[TestClient, Provider]]:
    settings = make_settings(
        claude_enabled=True, claude_external_enabled=True,
        model_gateway_url="https://proxy.test/openai", **overrides,
    )
    provider = provider or Provider()
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    try:
        with TestClient(create_app(settings)) as client:
            client.app.state.gateway = ModelGatewayClient(settings, http_client=http)
            yield client, provider
    finally:
        asyncio.run(http.aclose())


@contextmanager
def captured_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Capture every record the running app emits, at DEBUG.

    ``create_app`` calls ``configure_logging``, which clears the root handlers
    (removing pytest's capture handler) and resets the root level to INFO. So
    capture is re-attached only after the app exists.
    """
    root = logging.getLogger()
    previous = root.level
    caplog.handler.setLevel(logging.DEBUG)
    root.addHandler(caplog.handler)
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.removeHandler(caplog.handler)
        root.setLevel(previous)


def _session(client: TestClient, model: str, headers: dict | None = None, **extra) -> str:
    response = client.post(
        "/api/sessions", json={"title": "Adaptive", "model": model, **extra}, headers=headers or {},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


@pytest.mark.parametrize("stream", [False, True])
def test_adaptive_turn_never_exposes_thinking_blocks(stream, caplog):
    with claude_client() as (client, provider), captured_logs(caplog):
        sid = _session(client, ADAPTIVE)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": stream, "params": {"reasoning_effort": "low"},
        })
        assert response.status_code == 200, response.text
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        logs = caplog.text

    [wire] = provider.claude
    assert wire["model"].startswith(f"{ADAPTIVE}-")
    assert wire["output_config"] == {"effort": "low"}
    assert not {"thinking", "tools", "tool_choice", "temperature", "top_p"} & wire.keys()
    assistant = messages[-1]
    receipt = assistant["executionReceipt"]
    call = receipt["runtime"]["modelCalls"][0]
    assert (call["api"], call["parameters"]["reasoningEffort"]) == ("anthropic", "low")
    assert (call["cost"]["priceInputPer1M"], call["cost"]["priceOutputPer1M"]) == (4.0, 20.0)
    assert call["cost"]["estCostMicroUsd"] == 12 * 4 + 30 * 20
    # Controls: the visible block reaches every surface the hidden blocks must
    # not, and the log capture demonstrably records the turn's provider request.
    assert VISIBLE in response.text
    assert assistant["content"] == VISIBLE
    assert f"HTTP Request: POST https://proxy.test/openai/deployments/{wire['model']}/chat/completions" in logs
    for surface in (response.text, json.dumps(messages), logs):
        for marker in HIDDEN:
            assert marker not in surface
    assert not {"thinking", "signature", "redacted_thinking"} & _keys(messages)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("text", "stop_reason"), [(None, "max_tokens"), (VISIBLE, "end_turn")])
def test_max_tokens_stop_is_an_incomplete_turn_and_end_turn_stays_complete(
    stream, text, stop_reason, monkeypatch,
):
    from unittest.mock import AsyncMock

    from ai4ia_api.routers._chat_streaming import INCOMPLETE_RESPONSE_FALLBACK

    incomplete = stop_reason == "max_tokens"
    with claude_client(provider=Provider(text=text, stop_reason=stop_reason)) as (client, provider):
        remember = AsyncMock()
        monkeypatch.setattr(client.app.state.memory, "remember", remember)
        sid = _session(client, ADAPTIVE)
        response = client.post("/api/chat", json={"sessionId": sid, "content": "Hello", "stream": stream})
        assert response.status_code == 200, response.text
        messages = client.get(f"/api/sessions/{sid}/messages").json()

    assert len(provider.claude) == 1
    assistant = messages[-1]
    receipt = assistant["executionReceipt"]
    # A thinking-only reply cut off by max_tokens matches the Responses-API
    # incomplete outcome: fallback text, an incomplete partial receipt and no
    # automatic memory write. The end_turn control keeps the complete outcome.
    assert assistant["content"] == (INCOMPLETE_RESPONSE_FALLBACK if incomplete else VISIBLE)
    assert (receipt["status"], receipt["partial"]) == (
        ("incomplete", True) if incomplete else ("complete", False)
    )
    assert (INCOMPLETE_RESPONSE_FALLBACK in response.text) is incomplete
    assert remember.await_count == (0 if incomplete else 1)
    for marker in HIDDEN:
        assert marker not in response.text + json.dumps(messages)


@pytest.mark.parametrize("model", [ADAPTIVE, DISABLED])
def test_tool_agent_reaches_tools_only_on_the_tool_capable_claude_profile(model):
    with claude_client() as (client, provider):
        created = client.post("/api/agents", json={
            "name": "adaptive-calc", "systemPrompt": "Use the calculator.", "tools": ["calculator"],
        })
        assert created.status_code == 201, created.text
        sid = _session(client, model)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@adaptive-calc what is 6*7?", "stream": False,
        })
        messages = client.get(f"/api/sessions/{sid}/messages").json()

    if model == ADAPTIVE:
        assert response.status_code == 422
        assert "does not support tool calling" in response.json()["detail"]
        assert provider.claude == [] and messages == []
    else:
        assert response.status_code == 200, response.text
        assert [tool["name"] for tool in provider.claude[0]["tools"]] == ["calculator"]
        assert provider.claude[0]["thinking"] == {"type": "disabled"}


def _skill_service() -> OfficialMcpService:
    connector = FakeMcpConnector(
        resources=[DiscoveredResource(
            uri=_SKILL_URI, name="evidence-review",
            description="Review evidence transparently.", mimeType="text/markdown",
        )],
        resource_results={_SKILL_URI: McpResourceResult(
            uri=_SKILL_URI, text="# Evidence review", mime_type="text/markdown",
        )},
    )
    return OfficialMcpService(
        OfficialMcpCatalog(servers=[OfficialMcpServer(
            id="ai4ia-toolbox", displayName="AI4IA Toolbox",
            path="ai4ia-toolbox/mcp", resourcesEnabled=True,
        )]),
        gateway_url="https://mcp.example.com", subscription_key="subscription-key",
        connector=connector, resolver=lambda _host: ["93.184.216.34"],
    )


@pytest.mark.parametrize("model", [ADAPTIVE, DISABLED])
def test_injected_skill_loader_is_offered_only_to_the_tool_capable_profile(model):
    with claude_client(
        official_mcp_enabled=True, official_mcp_gateway_url="https://mcp.example.com",
        official_mcp_subscription_key="subscription-key",
    ) as (client, provider):
        client.app.state.official_mcp_service = _skill_service()
        created = client.post("/api/agents", json={
            "name": "reviewer", "systemPrompt": "Review evidence.", "tools": [],
        })
        assert created.status_code == 201, created.text
        sid = _session(client, model)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@reviewer inspect this", "stream": False,
        })

    assert response.status_code == 200, response.text
    [wire] = provider.claude
    offered = [tool["name"] for tool in wire.get("tools", [])]
    assert offered == ([] if model == ADAPTIVE else ["load_skill"])
    assert response.json()["message"]["content"] == VISIBLE


@pytest.mark.parametrize("model", [ADAPTIVE, DISABLED])
def test_workflow_steps_run_only_on_the_tool_capable_claude_profile(model):
    with claude_client() as (client, provider):
        for name in ("drafter", "editor"):
            created = client.post("/api/agents", json={"name": name, "systemPrompt": f"You are {name}."})
            assert created.status_code == 201, created.text
        workflow = client.post("/api/workflows", json={"name": "summarize", "steps": [
            {"agent": "drafter", "instruction": "Draft about {input}"},
            {"agent": "editor", "instruction": "Polish: {previous}"},
        ]})
        assert workflow.status_code == 201, workflow.text
        sid = _session(client, model)
        response = client.post("/api/workflows/summarize/run", json={
            "sessionId": sid, "input": "hi", "model": model,
        })

    if model == ADAPTIVE:
        assert response.status_code == 422
        assert "does not support tool calling" in response.json()["detail"]
        assert provider.claude == []
    else:
        assert response.status_code == 200, response.text
        assert len(provider.claude) == 2


@pytest.fixture
def published_claude():
    config = {"domains": {"publication": {
        "default": {"allow": ["consume"]},
        "mappings": [
            {"claim": "roles", "value": "Author", "allow": ["submit"]},
            {"claim": "roles", "value": "Reviewer", "allow": ["review"]},
        ],
    }}}
    settings = make_settings(
        auth_provider="entra", entra_tenant_id=TENANT, entra_audience=BARE_GUID,
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        asset_publishing_enabled=True, claude_enabled=True, claude_external_enabled=True,
        model_gateway_url="https://proxy.test/openai",
    )
    private, jwks = _new_keypair(KID)
    provider = Provider()

    def headers(role: str) -> dict:
        now = int(time.time())
        token = jwt.encode({
            "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": SUBJECTS[role],
            "iat": now, "exp": now + 3600, "roles": [role],
            "preferred_username": f"{role.lower()}@example.com",
        }, private, algorithm="RS256", headers={"kid": KID})
        return {"Authorization": f"Bearer {token}"}

    app = create_app(settings)
    http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    try:
        with TestClient(app) as client:
            app.state.catalog = app.state.catalog.model_copy(deep=True)
            app.state.policy.catalog = app.state.catalog
            app.state.agent_service._catalog = app.state.catalog
            app.state.auth_provider = _provider(jwks, audience=BARE_GUID)
            app.state.gateway = ModelGatewayClient(settings, http_client=http)
            yield client, headers, provider
    finally:
        asyncio.run(http.aclose())


def _publish(
    client: TestClient, headers, name: str, *, tools: list[str], skill_mode: str,
    model_ids: tuple[str, ...] = (DISABLED, ADAPTIVE),
) -> str:
    created = client.post("/api/agents", json={
        "name": name, "systemPrompt": "Use arithmetic carefully.", "tools": tools,
    }, headers=headers("Author"))
    assert created.status_code == 201, created.text
    path = f"/api/publications/agent/{name}"
    submitted = client.post(f"{path}/submit", json={
        "expectedRevision": created.json()["revision"], "audience": {"visibility": "public"},
        "modelIds": list(model_ids), "modes": ["chat"], "reviewConsent": True, "skillMode": skill_mode,
    }, headers=headers("Author"))
    assert submitted.status_code == 200, submitted.text
    owner = client.get(path, headers=headers("Author")).json()
    decision = client.post("/api/publication-reviews/decision", json={
        "source": owner["pendingSource"], "expectedHeadRevision": owner["revision"], "decision": "approved",
    }, headers=headers("Reviewer"))
    assert decision.status_code == 200, decision.text
    activated = client.post(f"{path}/activate", json={
        "source": owner["pendingSource"], "expectedHeadRevision": decision.json()["headRevision"],
    }, headers=headers("Author"))
    assert activated.status_code == 200, activated.text
    return activated.json()["handle"]


def _consume(
    client: TestClient, headers, provider: Provider, handle: str,
    models: tuple[str, ...] = (ADAPTIVE, DISABLED), **request,
) -> dict:
    results = {}
    for model in models:
        provider.claude.clear()
        before = provider.requests
        sid = _session(client, model, headers("Consumer"), agentName=handle, libraryDocumentIds=[])
        body = {"sessionId": sid, "content": "Calculate two plus two.", "stream": False, **request}
        response = client.post("/api/chat", json=body, headers=headers("Consumer"))
        persisted = client.get(f"/api/sessions/{sid}/messages", headers=headers("Consumer")).json()
        results[model] = (response, list(provider.claude), persisted, provider.requests - before)
    return results


def test_published_tool_agent_reaches_tools_only_on_the_tool_capable_claude_profile(published_claude):
    client, headers, provider = published_claude
    handle = _publish(client, headers, "shared-calc", tools=["calculator"], skill_mode="excluded")
    results = _consume(client, headers, provider, handle)

    refused, sent, persisted, _ = results[ADAPTIVE]
    assert refused.status_code == 422, refused.text
    assert "does not support tool calling" in refused.json()["detail"]
    assert sent == [] and persisted == []
    # Control: the same reviewed source reaches its tool on the tool-capable profile.
    allowed, sent, _, _ = results[DISABLED]
    assert allowed.status_code == 200, allowed.text
    assert [tool["name"] for tool in sent[0]["tools"]] == ["calculator"]


@pytest.mark.parametrize("stream", [False, True])
def test_published_skill_profile_refuses_before_persisting_on_models_without_tool_calling(
    published_claude, stream,
):
    client, headers, provider = published_claude
    client.app.state.official_mcp_service = _skill_service()
    handle = _publish(
        client, headers, "shared-reviewer", tools=[], skill_mode="versioned",
        model_ids=(DISABLED, ADAPTIVE, NON_TOOL_SOURCE),
    )
    results = _consume(
        client, headers, provider, handle, models=(ADAPTIVE, NON_TOOL_SOURCE, DISABLED), stream=stream,
    )

    # The reviewed loader is optional only for a request-level tool denial. A model
    # without tool calling is not that reason, so the turn refuses before the user
    # message is saved, with a stable, non-retryable capability code.
    for model in (ADAPTIVE, NON_TOOL_SOURCE):
        refused, sent, persisted, dispatched = results[model]
        assert refused.status_code == 422, (model, refused.text)
        assert refused.json()["code"] == "publication_model_tools_unsupported"
        assert (sent, persisted, dispatched) == ([], [], 0), model
    # Control: the same reviewed source on a tool-capable model offers the loader
    # and saves the user and assistant messages.
    allowed, sent, persisted, _ = results[DISABLED]
    assert allowed.status_code == 200, allowed.text
    assert [tool["name"] for tool in sent[0]["tools"]] == ["load_skill"]
    assert [message["role"] for message in persisted] == ["user", "assistant"]


def test_published_skill_profile_narrows_for_a_declared_request_tool_denial(published_claude):
    client, headers, provider = published_claude
    client.app.state.official_mcp_service = _skill_service()
    handle = _publish(client, headers, "shared-tool-free", tools=[], skill_mode="versioned")
    # The reviewed profile declares request_tools_disabled as the loader's only
    # narrowing, so a tool-free request runs on the adaptive profile and says so.
    [(response, sent, persisted, _)] = _consume(
        client, headers, provider, handle, models=(ADAPTIVE,), allowTools=False,
    ).values()
    assert response.status_code == 200, response.text
    assert len(sent) == 1 and not {"tools", "tool_choice"} & sent[0].keys()
    assert [message["role"] for message in persisted] == ["user", "assistant"]
    evidence = response.json()["message"]["executionReceipt"]["runtime"]["publication"]
    assert evidence["narrowing"] == ["request_tools_disabled"]


def test_published_webiq_profile_refuses_early_only_where_no_tool_can_be_offered(published_claude):
    from ai4ia_api.websearch.factory import build_web_search_service
    from tests.test_chat_websearch_api import FakeWebClient

    client, headers, provider = published_claude
    state = client.app.state
    state.web_search = build_web_search_service(
        make_settings(web_search_enabled=True), entitlements=state.entitlements,
        metering=state.usage, client=FakeWebClient(),
    )
    tool_model = next(
        entry.id for entry in state.catalog.models
        if entry.supportsTools and entry.api == "chat" and not entry.reasoningEffortOptions
    )
    # Web IQ tools are optional contracts of every chat profile while web search
    # is on, so the same early refusal covers them on a model without tools.
    handle = _publish(
        client, headers, "shared-search", tools=[], skill_mode="excluded",
        model_ids=(tool_model, NON_TOOL_SOURCE),
    )
    results = _consume(client, headers, provider, handle, models=(NON_TOOL_SOURCE, tool_model))
    refused, _, persisted, dispatched = results[NON_TOOL_SOURCE]
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "publication_model_tools_unsupported"
    assert (persisted, dispatched) == ([], 0)
    # Control: a tool-capable model offers the reviewed Web IQ contracts and runs.
    allowed, _, persisted, dispatched = results[tool_model]
    assert allowed.status_code == 200, allowed.text
    assert [message["role"] for message in persisted] == ["user", "assistant"]
    assert dispatched >= 1
