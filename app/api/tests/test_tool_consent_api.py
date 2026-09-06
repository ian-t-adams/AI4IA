"""Ownership and revocation controls for session-scoped tool consent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpToolResult
from ai4ia_api.main import create_app
from tests.conftest import make_settings
from tests.test_tool_approval_gate import (
    _SEND, _InjectedModelGateway, _bootstrap, _client, _turn,
)
from tests.test_agent_runtime import ScriptedGateway, _assistant_text, _assistant_tool_call


def test_session_consent_is_explicit_server_owned_and_revocable():
    with TestClient(create_app(make_settings(tool_auto_approve_enabled=True))) as client:
        session = client.post(
            "/api/sessions",
            json={"model": "gpt-5.4", "toolOverrides": {"added": ["calculator"]}},
        ).json()
        sid = session["id"]
        assert session.get("toolConsent") is None
        granted = client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": True}
        )
        assert granted.status_code == 200, granted.text
        consent = granted.json()["toolConsent"]
        assert consent["scope"] == "session"
        assert consent["toolCount"] == 1
        assert consent["expiresAt"] > consent["grantedAt"]
        assert "toolConsentState" not in granted.json()
        assert client.get("/api/tools").json()["toolAutoApproveAvailable"] is True
        assert client.get(f"/api/sessions/{sid}/inspector").json()[
            "toolAutoApproveAvailable"
        ] is True

        assert client.patch(
            f"/api/sessions/{sid}",
            json={"toolConsent": {**consent, "expiresAt": "2099-01-01T00:00:00Z"}},
        ).status_code == 422
        denied = client.post(
            f"/api/sessions/{sid}/tool-consent",
            json={"enabled": False},
            headers={"X-Dev-User": "someone-else"},
        )
        assert denied.status_code == 404
        revoked = client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": False}
        )
        assert revoked.status_code == 200
        assert revoked.json()["toolConsent"] is None
        assert client.get(f"/api/sessions/{sid}").json()["toolConsent"] is None


def test_disabled_auto_approval_refuses_grants_but_allows_revocation(client):
    sid = client.post("/api/sessions", json={}).json()["id"]
    assert client.get("/api/tools").json().get("toolAutoApproveAvailable", False) is False
    response = client.post(
        f"/api/sessions/{sid}/tool-consent", json={"enabled": True}
    )
    assert response.status_code == 409
    assert client.post(
        f"/api/sessions/{sid}/tool-consent", json={"enabled": False}
    ).status_code == 200
    assert client.post(
        f"/api/sessions/{sid}/tool-consent", json={"enabled": "true"}
    ).status_code == 422


def test_plain_chat_uses_synthetic_consent_without_an_agent():
    calls = []

    class Web:
        def build_capability(self, **_kwargs):
            async def browse(args, _ctx):
                calls.append(args)
                return {"content": "page"}
            return (
                [{"type": "function", "function": {
                    "name": "browse_url", "parameters": {"type": "object"},
                }}],
                {"browse_url": browse},
            )

    with TestClient(create_app(make_settings(tool_auto_approve_enabled=True))) as client:
        client.app.state.web_search = Web()
        sid = client.post("/api/sessions", json={"model": "gpt-5.4"}).json()["id"]
        for enabled in (False, True):
            if enabled:
                assert client.post(
                    f"/api/sessions/{sid}/tool-consent", json={"enabled": True},
                ).status_code == 200
            client.app.state.gateway = ScriptedGateway([
                _assistant_tool_call("c1", "browse_url", '{"url":"https://example.org"}'),
                _assistant_text("done"),
            ])
            response = client.post("/api/chat", json={
                "sessionId": sid, "content": "Read this page", "stream": False,
            })
            assert response.status_code == 200, response.text
            assert len(calls) == int(enabled)
            receipt = response.json()["message"]["executionReceipt"]
            assert receipt["toolCalls"][0]["outcome"] == ("delegate" if enabled else "denied")
            if enabled:
                assert receipt["toolCalls"][0]["approval"] == "session"


@pytest.mark.parametrize("trusted", [False, True])
def test_consent_exposes_only_attached_mcp_contracts_and_retains_receipts(trusted):
    connector = FakeMcpConnector(
        [_SEND], call_results={"send": McpToolResult(content={"api_key": "private-value", "sent": True})},
    )
    client = _client(connector, tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        client.patch(f"/api/sessions/{sid}", json={"agentName": "courierbot"})
        assert client.put(
            "/api/agents/mcp-servers/courier",
            json={"endpoint": "https://courier.example.com/rpc", "trusted": trusted},
        ).status_code == 200
        client.app.state.gateway = _InjectedModelGateway(repeat=2)
        without = _turn(client, sid)
        assert without.status_code == 200
        assert connector.tool_calls == []

        grant_response = client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": True},
        )
        assert grant_response.status_code == 200, grant_response.text
        grant = grant_response.json()["toolConsent"]
        assert grant["toolCount"] == 1
        client.app.state.gateway = _InjectedModelGateway(repeat=2)
        response = _turn(client, sid)
        assert response.status_code == 200, response.text
        assert len(connector.tool_calls) == 2
        message = response.json()["message"]
        assert message["pendingApprovals"] is None
        assert len(message["steps"]) == 2
        receipt = message["executionReceipt"]
        assert receipt["toolCallCount"] == 2
        assert receipt["autoApprovedToolCalls"] == 2
        assert len(receipt["prompt"]) > 0
        assert receipt["modelRequests"]
        assert "private-value" not in str(receipt)
        for call in receipt["toolCalls"]:
            assert call["approval"] == "session"
            assert call["consentId"] == grant["id"]
            assert call["arguments"]["text"]
            assert call["result"]["text"]
        saved = client.get(f"/api/sessions/{sid}/messages").json()[-1]
        assert saved["executionReceipt"] == receipt
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("change", [
    None, "revoke", "disable", "endpoint", "schema", "permission", "selection", "expire", "entitlement",
])
def test_live_consent_is_rechecked_between_calls_in_one_model_iteration(change):
    class ChangingConnector(FakeMcpConnector):
        after_first = None

        async def call_tool(self, **kwargs):
            result = await super().call_tool(**kwargs)
            if len(self.tool_calls) == 1 and self.after_first is not None:
                await self.after_first()
            return result

    connector = ChangingConnector(
        [_SEND], call_results={"send": McpToolResult(content="delivered")},
    )
    client = _client(connector, tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        client.patch(f"/api/sessions/{sid}", json={"agentName": "courierbot"})
        granted = client.post(f"/api/sessions/{sid}/tool-consent", json={"enabled": True})
        assert granted.status_code == 200, granted.text
        repo = client.app.state.session_repo
        uid = client.get(f"/api/sessions/{sid}").json()["userId"]

        async def mutate():
            if change == "revoke":
                await repo.set_tool_consent(uid, sid, None)
            elif change == "disable":
                client.app.state.settings.tool_auto_approve_enabled = False
            elif change in {"endpoint", "schema", "permission"}:
                service = client.app.state.mcp_service
                server = await service.get(uid, "courier")
                if change == "endpoint":
                    server.endpoint = "https://different.example.org/rpc"
                    server.host = "different.example.org"
                elif change == "schema":
                    server.discoveredTools[0].inputSchema["required"] = ["to"]
                else:
                    server.trusted = False
                await service._store.upsert(server)
            elif change == "selection":
                await repo.patch_session(uid, sid, {"toolOverrides": {"added": ["calculator"]}})
            elif change == "expire":
                stored = repo._sessions[sid]
                old = stored.toolConsentState
                aged = old.grant.model_copy(update={
                    "expiresAt": datetime.now(timezone.utc) - timedelta(seconds=1),
                })
                stored.toolConsentState = old.model_copy(update={"grant": aged})
                stored.toolConsent = aged
            elif change == "entitlement":
                from ai4ia_api.entitlements.models import EntitlementLimits

                await client.app.state.entitlements.set(
                    uid, EntitlementLimits(disabled=True), updated_by="test",
                )

        connector.after_first = mutate
        client.app.state.gateway = _InjectedModelGateway(repeat=2)
        response = _turn(client, sid)
        assert response.status_code == 200, response.text
        assert len(connector.tool_calls) == (2 if change is None else 1)
        calls = response.json()["message"]["executionReceipt"]["toolCalls"]
        assert len(calls) == 2
        assert calls[0]["approval"] == "session"
        if change is not None:
            assert calls[1]["outcome"] == "denied"
            assert calls[1]["detail"].startswith("consent_") or calls[1]["detail"] == "entitlement_denied"
            assert calls[1]["arguments"]["text"]
    finally:
        client.__exit__(None, None, None)


def test_consent_does_not_bypass_mcp_ssrf_revalidation():
    connector = FakeMcpConnector(
        [_SEND], call_results={"send": McpToolResult(content="delivered")},
    )
    client = _client(connector, tool_auto_approve_enabled=True)
    try:
        sid = _bootstrap(client)
        client.patch(f"/api/sessions/{sid}", json={"agentName": "courierbot"})
        assert client.post(
            f"/api/sessions/{sid}/tool-consent", json={"enabled": True},
        ).status_code == 200
        for address in ("93.184.216.34", "127.0.0.1", "93.184.216.34"):
            connector.tool_calls.clear()
            client.app.state.mcp_service._resolver = lambda _host: [address]
            client.app.state.gateway = _InjectedModelGateway()
            response = _turn(client, sid)
            assert response.status_code == 200, response.text
            assert len(connector.tool_calls) == int(address != "127.0.0.1")
            call = response.json()["message"]["executionReceipt"]["toolCalls"][0]
            assert call["approval"] == "session"
            if address == "127.0.0.1":
                assert call["outcome"] == "error"
    finally:
        client.__exit__(None, None, None)
