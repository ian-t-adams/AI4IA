"""Consent prerequisites and dynamically advertised tool contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai4ia_api.agents.consent import tool_contract_hash
from ai4ia_api.agents.consent_service import tool_auto_approve_available
from ai4ia_api.agents.synthetic_governance import synthetic_spec
from ai4ia_api.routers.chat import _ephemeral_tool_agent
from ai4ia_api.websearch.capability import build_web_search_capability
from ai4ia_api.websearch.contracts import CLASSIC_ANSWER_TYPES, WEBIQ_TOOL_NAMES, tool_schema
from tests.test_custom_tools_config import _settings


def test_nonlocal_consent_requires_entra_even_when_dev_auth_is_allowed():
    settings = _settings(
        env="dev", tool_auto_approve_enabled=True,
        session_store="cosmos", cosmos_endpoint="https://cosmos.example/",
    )
    with pytest.raises(RuntimeError, match="AI4IA_TOOL_AUTO_APPROVE_ENABLED.*Entra"):
        settings.validate_runtime()


def test_nonlocal_consent_requires_durable_session_state():
    settings = _settings(
        env="dev", tool_auto_approve_enabled=True,
        auth_provider="entra", entra_tenant_id="t1", entra_audience="api://ai4ia",
    )
    with pytest.raises(RuntimeError, match="AI4IA_TOOL_AUTO_APPROVE_ENABLED.*Cosmos"):
        settings.validate_runtime()


def test_consent_prerequisite_positive_and_disabled_controls():
    _settings(tool_auto_approve_enabled=True).validate_runtime()
    _settings(env="dev", tool_auto_approve_enabled=False).validate_runtime()
    _settings(
        env="dev", tool_auto_approve_enabled=True,
        auth_provider="entra", entra_tenant_id="t1", entra_audience="api://ai4ia",
        session_store="cosmos", cosmos_endpoint="https://cosmos.example/",
    ).validate_runtime()


def test_runtime_availability_remains_fail_closed_after_configuration_changes(client):
    client.app.state.settings.tool_auto_approve_enabled = True
    assert tool_auto_approve_available(client.app.state)
    client.app.state.settings.env = "dev"
    assert not tool_auto_approve_available(client.app.state)
    assert client.get("/api/tools").json()["toolAutoApproveAvailable"] is False


def test_research_metadata_tracks_the_entire_webiq_contract_catalog():
    agent = _ephemeral_tool_agent("research")
    assert set(agent.tools) == WEBIQ_TOOL_NAMES
    assert all(name in agent.systemPrompt for name in WEBIQ_TOOL_NAMES)
    assert f"all {len(CLASSIC_ANSWER_TYPES)} advertised" in agent.systemPrompt
    assert "weather" in agent.systemPrompt.lower()
    assert "structured" in agent.systemPrompt.lower()
    assert "beta" in agent.systemPrompt.lower()
    assert "sonic blended" in agent.systemPrompt.lower()


@pytest.mark.parametrize("enabled", [False, True])
def test_tool_catalog_describes_every_webiq_capability_without_granting_it(client, enabled):
    class Service:
        async def close(self):
            return None

    client.app.state.settings.web_search_enabled = enabled
    client.app.state.web_search = Service() if enabled else None
    response = client.get("/api/tools")
    assert response.status_code == 200, response.text
    items = {item["name"]: item for item in response.json()["tools"]}
    assert WEBIQ_TOOL_NAMES <= items.keys()
    for name in WEBIQ_TOOL_NAMES:
        assert items[name]["available"] is enabled
        assert items[name]["risk"] == "external"
        assert items[name]["requiresApproval"] is True
        assert items[name]["selectable"] is False
        assert items[name]["description"]


def test_session_consent_snapshots_every_enabled_webiq_contract(client):
    class Service:
        def build_capability(self, **_kwargs):
            async def never_execute(_args, _ctx):
                raise AssertionError("Consent metadata discovery must not execute a tool.")
            return (
                [
                    tool_schema(name, results_cap=5, content_cap=1000)
                    for name in sorted(WEBIQ_TOOL_NAMES)
                ],
                {name: never_execute for name in WEBIQ_TOOL_NAMES},
            )

        async def close(self):
            return None

    client.app.state.settings.tool_auto_approve_enabled = True
    client.app.state.settings.web_search_enabled = True
    client.app.state.web_search = Service()
    sid = client.post("/api/sessions", json={"model": "gpt-5.4"}).json()["id"]
    response = client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["toolConsent"]["toolCount"] == len(WEBIQ_TOOL_NAMES)
    stored = client.app.state.session_repo._sessions[sid].toolConsentState
    assert set(stored.contracts) == WEBIQ_TOOL_NAMES


@pytest.mark.parametrize("change,status", [
    (None, "active"), ("expire", "expired"), ("revoke", "revoked"),
    ("selection", "changed"), ("operator", "disabled"), ("schema", "changed"),
])
def test_inspector_reports_live_consent_not_just_persisted_summary(client, change, status):
    client.app.state.settings.tool_auto_approve_enabled = True
    sid = client.post("/api/sessions", json={
        "model": "gpt-5.4", "toolOverrides": {"added": ["calculator"]},
    }).json()["id"]
    granted = client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": True}).json()
    summary = granted["toolConsent"]
    if change == "expire":
        stored = client.app.state.session_repo._sessions[sid]
        aged = stored.toolConsentState.grant.model_copy(update={
            "expiresAt": datetime.now(timezone.utc) - timedelta(seconds=1),
        })
        stored.toolConsentState = stored.toolConsentState.model_copy(update={"grant": aged})
        stored.toolConsent = aged
    elif change == "revoke":
        client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": False})
    elif change == "selection":
        client.patch(f"/api/sessions/{sid}", json={
            "toolOverrides": {"added": ["calculator", "get_current_time"]},
        })
    elif change == "operator":
        client.app.state.settings.tool_auto_approve_enabled = False
    elif change == "schema":
        client.app.state.tool_executor.get("calculator").parameters["required"] = []
    response = client.get(f"/api/sessions/{sid}/inspector")
    assert response.status_code == 200, response.text
    inspected = response.json()
    assert inspected["toolConsentActive"] is (change is None)
    assert inspected["toolConsentStatus"] == status
    if change != "revoke":
        assert inspected["toolConsent"]["id"] == summary["id"]
        assert inspected["toolConsent"]["expiresAt"]
    if change in {"expire", "selection", "schema", "revoke"}:
        renewed = client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": True},
        )
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["toolConsent"]["id"] != summary["id"]
        assert client.get(f"/api/sessions/{sid}/inspector").json()["toolConsentActive"] is True


def test_inspector_never_claims_active_when_contract_verification_fails(client, monkeypatch, caplog):
    from ai4ia_api.agents import consent_service

    client.app.state.settings.tool_auto_approve_enabled = True
    sid = client.post("/api/sessions", json={
        "toolOverrides": {"added": ["calculator"]},
    }).json()["id"]
    client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": True})
    assert client.get(f"/api/sessions/{sid}/inspector").json()["toolConsentActive"] is True

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider-credential-detail")

    monkeypatch.setattr(consent_service, "session_snapshot", unavailable)
    response = client.get(f"/api/sessions/{sid}/inspector")
    assert response.status_code == 200, response.text
    assert response.json()["toolConsentActive"] is False
    assert response.json()["toolConsentStatus"] == "unavailable"
    assert "provider-credential-detail" not in caplog.text


def test_webiq_per_turn_nonce_is_not_part_of_the_consent_contract(client):
    settings = client.app.state.settings
    settings.web_search_enabled = True

    def hashes(nonce):
        schemas, _ = build_web_search_capability(
            client=object(), entitlements=client.app.state.entitlements,
            metering=client.app.state.usage, settings=settings,
            user_id="user", session_id="session", nonce=nonce,
        )
        return {
            schema["function"]["name"]: tool_contract_hash(
                synthetic_spec(schema["function"]["name"]),
                schema["function"]["parameters"],
                description=schema["function"]["description"],
            )
            for schema in schemas
        }

    initial = hashes("nonce-one")
    assert set(initial) == WEBIQ_TOOL_NAMES
    assert initial == hashes("completely-different-nonce")
    settings.web_search_max_results += 1
    assert initial != hashes("nonce-one")


@pytest.mark.parametrize("change", [None, "add", "description", "version", "endpoint", "mime"])
def test_official_skill_contract_changes_require_renewed_consent_before_egress(client, change):
    from ai4ia_api.agents.mcp_client import McpResourceResult
    from tests.test_agent_runtime import ScriptedGateway, _assistant_text, _assistant_tool_call
    from tests.test_mcp_skills import _resource, _server

    server = _server(*([] if change == "add" else [_resource()]))
    reads = []
    browsed = []

    class Skills:
        async def list_all(self):
            return [server]

        async def read_resource(self, selected, uri):
            reads.append((selected.name, uri))
            return McpResourceResult(uri=uri, text="# Evidence review", mime_type="text/markdown")

        async def close(self):
            return None

    class Web:
        def build_capability(self, **_kwargs):
            async def browse(args, _ctx):
                browsed.append(args)
                return {"content": "source"}

            return (
                [{"type": "function", "function": {
                    "name": "browse_url", "parameters": {
                        "type": "object", "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                }}],
                {"browse_url": browse},
            )

        async def close(self):
            return None

    client.app.state.settings.tool_auto_approve_enabled = True
    client.app.state.official_mcp_service = Skills()
    client.app.state.web_search = Web()
    sid = client.post("/api/sessions", json={
        "model": "gpt-5.4", "agentName": "general",
    }).json()["id"]
    granted = client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": True})
    assert granted.status_code == 200, granted.text
    assert not reads  # Snapshotting contracts must not load skill instructions.

    if change == "add":
        server.discoveredResources = [_resource()]
    elif change == "description":
        server.discoveredResources = [_resource(description="Changed advertised instructions")]
    elif change == "version":
        server.discoveredResources = [_resource("skill://evidence-review/SKILL.md?version=2")]
    elif change == "endpoint":
        server.endpoint = "https://changed.example.com/toolbox/mcp"
        server.host = "changed.example.com"
    elif change == "mime":
        server.discoveredResources = [_resource(mime_type="text/plain")]

    inspected = client.get(f"/api/sessions/{sid}/inspector").json()
    client.app.state.gateway = ScriptedGateway([
        _assistant_tool_call("skill-1", "load_skill", '{"name":"evidence-review"}'),
        _assistant_tool_call("browse-1", "browse_url", '{"url":"https://example.com"}'),
        _assistant_text("Finished."),
    ])
    response = client.post("/api/chat", json={
        "sessionId": sid, "content": "Review the evidence and read its source.", "stream": False,
    })
    assert response.status_code == 200, response.text
    assert len(reads) == int(change is None)
    assert len(browsed) == int(change is None)
    assert inspected["toolConsentActive"] is (change is None)
    assert inspected["toolConsentStatus"] == ("active" if change is None else "changed")
    calls = response.json()["message"]["executionReceipt"]["toolCalls"]
    assert calls[-1]["outcome"] == ("delegate" if change is None else "denied")
    if change is None:
        assert calls[-1]["approval"] == "session"
        assert calls[-1]["consentId"] == granted.json()["toolConsent"]["id"]
    else:
        renewed = client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": True})
        assert renewed.status_code == 200, renewed.text
        assert client.get(f"/api/sessions/{sid}/inspector").json()["toolConsentActive"] is True
        client.app.state.gateway = ScriptedGateway([
            _assistant_tool_call("skill-2", "load_skill", '{"name":"evidence-review"}'),
            _assistant_tool_call("browse-2", "browse_url", '{"url":"https://example.com"}'),
            _assistant_text("Finished after renewal."),
        ])
        retried = client.post("/api/chat", json={
            "sessionId": sid, "content": "Retry with renewed consent.", "stream": False,
        })
        assert retried.status_code == 200, retried.text
        assert len(reads) == len(browsed) == 1
        retried_call = retried.json()["message"]["executionReceipt"]["toolCalls"][-1]
        assert retried_call["approval"] == "session"
        assert retried_call["consentId"] == renewed.json()["toolConsent"]["id"]
    assert "load_skill" in client.app.state.session_repo._sessions[sid].toolConsentState.contracts
