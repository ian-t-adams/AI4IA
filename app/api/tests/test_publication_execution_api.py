"""Published sources reach real API/runtime/transport seams under consumer authority."""
from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.catalog import load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from tests.conftest import make_settings
from tests.test_auth_entra import BARE_GUID, ISSUER, KID, TENANT, _new_keypair, _provider

SUBJECTS = {
    "Author": "00000000-0000-0000-0000-000000000001",
    "Reviewer": "00000000-0000-0000-0000-000000000002",
    "Consumer": "00000000-0000-0000-0000-000000000003",
}


@pytest.fixture
def published_api():
    model = next(
        entry for entry in load_catalog().models
        if entry.supportsTools and entry.api == "chat" and not entry.reasoningEffortOptions
    )
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
        asset_publishing_enabled=True,
    )
    private, jwks = _new_keypair(KID)
    calls = []

    def response(request):
        body = json.loads(request.content)
        calls.append(body)
        tools = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "calculator" in tools and not any(message["role"] == "tool" for message in body["messages"]):
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-calc", "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'},
            }]}
        else:
            message = {"role": "assistant", "content": "Four."}
        return httpx.Response(200, json={
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        })

    def headers(role):
        now = int(time.time())
        token = jwt.encode({
            "aud": BARE_GUID, "iss": ISSUER, "tid": TENANT, "oid": SUBJECTS[role],
            "iat": now, "exp": now + 3600, "roles": [role],
            "preferred_username": f"{role.lower()}@example.com",
        }, private, algorithm="RS256", headers={"kid": KID})
        return {"Authorization": f"Bearer {token}"}

    app = create_app(settings)
    with TestClient(app) as client:
        app.state.catalog = app.state.catalog.model_copy(deep=True)
        app.state.policy.catalog = app.state.catalog
        app.state.agent_service._catalog = app.state.catalog
        app.state.auth_provider = _provider(jwks, audience=BARE_GUID)
        app.state.gateway = ModelGatewayClient(
            settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        )
        yield client, model.id, headers, calls


def publish(client, model, headers, kind, name, *, tools=True, modes=None):
    body = (
        {"name": name, "systemPrompt": "Use arithmetic carefully.",
         "tools": ["calculator"] if tools else []}
        if kind == "agent" else
        {"name": name, "steps": [{"agent": "general", "instruction": "{input}", "extraTools": ["calculator"]}]}
    )
    collection = "agents" if kind == "agent" else "workflows"
    created = client.post(f"/api/{collection}", json=body, headers=headers("Author"))
    assert created.status_code == 201, created.text
    submitted = client.post(f"/api/publications/{kind}/{name}/submit", json={
        "expectedRevision": created.json()["revision"], "audience": {"visibility": "public"},
        "modelIds": [model], "modes": modes or (["chat", "workflow", "delegation"] if kind == "agent" else ["workflow", "workflow_tool"]),
        "reviewConsent": True, "skillMode": "excluded",
    }, headers=headers("Author"))
    assert submitted.status_code == 200, submitted.text
    owner = client.get(f"/api/publications/{kind}/{name}", headers=headers("Author")).json()
    source = owner["pendingSource"]
    decision = client.post("/api/publication-reviews/decision", json={
        "source": source, "expectedHeadRevision": owner["revision"], "decision": "approved",
    }, headers=headers("Reviewer"))
    assert decision.status_code == 200, decision.text
    activated = client.post(f"/api/publications/{kind}/{name}/activate", json={
        "source": source, "expectedHeadRevision": decision.json()["headRevision"],
    }, headers=headers("Author"))
    assert activated.status_code == 200, activated.text
    return activated.json(), source


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_review_reads_and_mutation_acknowledgements_keep_immutable_status(published_api, decision):
    client, model, headers, calls = published_api
    created = client.post("/api/agents", json={
        "name": "review-status", "systemPrompt": "Use this reviewed source.", "tools": [],
    }, headers=headers("Author"))
    assert created.status_code == 201
    path = "/api/publications/agent/review-status"
    submitted = client.post(f"{path}/submit", json={
        "expectedRevision": created.json()["revision"], "audience": {"visibility": "public"},
        "modelIds": [model], "modes": ["chat"], "reviewConsent": True, "skillMode": "excluded",
    }, headers=headers("Author"))
    assert submitted.status_code == 200, submitted.text
    head = submitted.json()
    source = head["pendingSource"]
    assert head["pendingDraftRevision"] == created.json()["revision"]
    assert source["ownerId"] == created.json()["userId"]
    assert source["assetId"] == head["assetId"]
    assert source["version"] == head["pendingVersion"] == head["versionCount"]
    assert head["reviewDecision"] is None and head["reviewerId"] is None
    detail_path = (
        f"/api/publication-reviews/agent/{quote(source['ownerId'], safe='')}/"
        f"{source['assetId']}/{source['version']}"
    )

    def read_reviews():
        inbox = client.get("/api/publication-reviews?kind=agent", headers=headers("Reviewer"))
        detail = client.get(detail_path, params={"digest": source["digest"]}, headers=headers("Reviewer"))
        assert inbox.status_code == detail.status_code == 200
        assert len(inbox.json()["items"]) == 1
        return inbox.json()["items"][0], detail.json()

    before_inbox, before_detail = read_reviews()
    assert before_inbox["reviewDecision"] is None and before_detail["reviewDecision"] is None
    assert client.get(
        detail_path, params={"digest": source["digest"]}, headers=headers("Consumer"),
    ).status_code == 404
    reviewed = client.post("/api/publication-reviews/decision", json={
        "source": source, "expectedHeadRevision": head["revision"], "decision": decision,
    }, headers=headers("Reviewer"))
    assert reviewed.status_code == 200, reviewed.text
    acknowledged = reviewed.json()
    assert acknowledged["source"] == source
    assert acknowledged["headRevision"] == head["revision"] + 1
    assert acknowledged["reviewDecision"] == decision
    assert acknowledged["reviewerId"] and acknowledged["reviewerId"] != source["ownerId"]
    for read in (*read_reviews(), client.get(path, headers=headers("Author")).json()):
        assert read["reviewDecision"] == decision
        assert read["reviewerId"] == acknowledged["reviewerId"]
    repeated = client.post("/api/publication-reviews/decision", json={
        "source": source, "expectedHeadRevision": acknowledged["headRevision"],
        "decision": "rejected" if decision == "approved" else "approved",
    }, headers=headers("Reviewer"))
    assert repeated.status_code == 409

    activated = client.post(f"{path}/activate", json={
        "source": source, "expectedHeadRevision": acknowledged["headRevision"],
    }, headers=headers("Author"))
    if decision == "approved":
        assert activated.status_code == 200, activated.text
        current = activated.json()
        assert current["activeSource"] == source and current["pendingSource"] is None
        assert current["pendingDraftRevision"] is None
        assert current["revision"] == acknowledged["headRevision"] + 1
    else:
        assert activated.status_code == 409
        current = client.get(path, headers=headers("Author")).json()
    withdrawn = client.post(f"{path}/withdraw", json={
        "expectedHeadRevision": current["revision"],
    }, headers=headers("Author"))
    assert withdrawn.status_code == 200, withdrawn.text
    final = withdrawn.json()
    assert final["revision"] == current["revision"] + 1
    assert final["assetId"] == source["assetId"]
    assert final["activeSource"] is None and final["pendingSource"] is None
    assert final["pendingDraftRevision"] is None and final["reviewDecision"] is None
    assert final["visibility"] == "private" and final["acl"] == [] and final["groupAcl"] == []
    assert calls == []


@pytest.mark.parametrize("tools", [False, True])
def test_published_chat_pins_source_and_actual_subset_then_refuses_withdrawal(published_api, tools):
    client, model, headers, calls = published_api
    head, source = publish(client, model, headers, "agent", "shared-agent", tools=tools)
    session = client.post("/api/sessions", json={
        "model": model, "agentName": head["handle"], "libraryDocumentIds": [],
    }, headers=headers("Consumer"))
    assert session.status_code == 201, session.text
    assert session.json()["agentVersion"] == source
    body = {"sessionId": session.json()["id"], "content": "Calculate two plus two.",
            "stream": False, "params": {"max_tokens": 64}}
    result = client.post("/api/chat", json=body, headers=headers("Consumer"))
    assert result.status_code == 200, result.text
    assert len(calls) == (2 if tools else 1)
    evidence = result.json()["message"]["executionReceipt"]["runtime"]["publication"]
    assert evidence["source"] == source
    assert evidence["effectiveSubsetDigest"]
    assert evidence["exclusions"] == ["skills_excluded_by_author"]
    withdrawn = client.post("/api/publications/agent/shared-agent/withdraw", json={
        "expectedHeadRevision": head["revision"],
    }, headers=headers("Author"))
    assert withdrawn.status_code == 200, withdrawn.text
    calls.clear()
    assert client.post("/api/chat", json=body, headers=headers("Consumer")).status_code >= 400
    assert calls == []


def test_published_workflow_executes_consumer_steps_and_keeps_subset_receipts(published_api):
    client, model, headers, calls = published_api
    head, source = publish(client, model, headers, "workflow", "shared-flow")
    sid = client.post("/api/sessions", json={
        "model": model, "libraryDocumentIds": [],
    }, headers=headers("Consumer")).json()["id"]
    result = client.post(f"/api/workflows/{head['handle']}/run", json={
        "sessionId": sid, "input": "Calculate two plus two.", "sourceVersion": source,
    }, headers=headers("Consumer"))
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True, result.text
    assert len(calls) == 2
    message = result.json()["message"]
    assert message["executionReceipt"]["runtime"]["publication"]["source"] == source
    assert message["workflowStepReceipts"][0]["runtime"]["publication"]["effectiveSubsetDigest"]


def test_published_voice_rechecks_frames_and_keeps_server_source_receipt(published_api):
    from ai4ia_api.routers.realtime import BEARER_SUBPROTOCOL
    from tests.test_realtime_api import FakeRealtimeConnector

    client, _, headers, _ = published_api
    state = client.app.state
    state.settings.realtime_enabled = True
    state.settings.realtime_base_url = "https://realtime-gateway.test/openai"
    state.settings.realtime_gateway_api_key = "realtime-key"
    model = next(entry.id for entry in state.catalog.models if entry.category == "realtime")
    head, source = publish(
        client, model, headers, "agent", "spoken", tools=False, modes=["voice"],
    )
    consumer = headers("Consumer")
    created = client.post("/api/sessions", json={
        "model": model, "agentName": head["handle"], "libraryDocumentIds": [],
    }, headers=consumer)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    connector = FakeRealtimeConnector()
    state.realtime_connector = connector
    with client.websocket_connect(
        f"/api/voice/live?session={sid}&model={model}",
        headers={"origin": "http://localhost:3000"},
        subprotocols=[BEARER_SUBPROTOCOL, consumer["Authorization"].split(" ", 1)[1]],
    ) as ws:
        ws.send_text('{"type":"session.update","session":{}}')
        echoed = ws.receive_text()
        assert "Use arithmetic carefully." in echoed
        ws.close()
    assert len(connector.connects) == 1
    messages = client.get(f"/api/sessions/{sid}/messages", headers=consumer)
    assert messages.status_code == 200, messages.text
    event = next(message for message in messages.json() if message.get("executionReceipt"))
    assert event["fromCommand"] is True
    assert event["executionReceipt"]["runtime"]["publication"]["source"] == source
    assert event["executionReceipt"]["runtime"]["publication"]["effectiveSubsetDigest"]
    assert event["executionReceipt"]["usage"]["known"] is False


def test_explicit_empty_document_scope_records_supported_optional_narrowing(published_api):
    from ai4ia_api.library.retrieval import RetrievalContext

    client, model, headers, calls = published_api

    class Retrieval:
        async def context(self, *_args, **_kwargs):
            return RetrievalContext()

    client.app.state.document_retrieval = Retrieval()
    head, _ = publish(client, model, headers, "agent", "optional-docs")
    sid = client.post("/api/sessions", json={
        "model": model, "agentName": head["handle"], "libraryDocumentIds": [],
    }, headers=headers("Consumer")).json()["id"]
    response = client.post("/api/chat", json={
        "sessionId": sid, "content": "Calculate 2+2.", "stream": False,
        "params": {"max_tokens": 64},
    }, headers=headers("Consumer"))
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    evidence = response.json()["message"]["executionReceipt"]["runtime"]["publication"]
    assert evidence["narrowing"] == ["empty_document_scope"]
    assert evidence["effectiveSubsetDigest"] != evidence["approvedProfileDigest"]


@pytest.mark.parametrize("change", [
    None, "required", "required_offer", "schema", "offered_schema", "extra", "discovery",
    "model_version", "model_disabled", "required_protocol",
])
def test_publication_changes_refuse_before_real_model_dispatch(published_api, monkeypatch, change):
    from dataclasses import replace

    from ai4ia_api.agents.tool_exec import ToolExecutor
    from ai4ia_api.library.retrieval import RetrievalContext

    client, model, headers, calls = published_api

    class Retrieval:
        async def context(self, *_args, **_kwargs):
            return RetrievalContext()

    if change == "discovery":
        client.app.state.document_retrieval = Retrieval()
    head, _ = publish(client, model, headers, "agent", "changing-source")
    sid = client.post("/api/sessions", json={
        "model": model, "agentName": head["handle"], "libraryDocumentIds": [],
    }, headers=headers("Consumer")).json()["id"]
    if change == "required":
        registry = client.app.state.tool_registry
        registry._tools["calculator"] = replace(registry.get("calculator"), enabled=False)
    elif change == "schema":
        client.app.state.tool_executor.get("calculator").parameters["additionalProperties"] = False
    elif change == "extra":
        original = ToolExecutor.schema_for

        def extra(self, names, **kwargs):
            return original(self, names, **kwargs) + original(self, ["get_current_time"], **kwargs)
        monkeypatch.setattr(ToolExecutor, "schema_for", extra)
    elif change == "required_offer":
        monkeypatch.setattr(ToolExecutor, "schema_for", lambda self, names, **kwargs: [])
    elif change == "offered_schema":
        import copy

        original = ToolExecutor.schema_for

        def changed_schema(self, names, **kwargs):
            schemas = copy.deepcopy(original(self, names, **kwargs))
            for schema in schemas:
                schema["function"]["parameters"]["additionalProperties"] = False
            return schemas
        monkeypatch.setattr(ToolExecutor, "schema_for", changed_schema)
    elif change == "discovery":
        def unavailable(**_kwargs):
            raise RuntimeError("discovery unavailable")
        monkeypatch.setattr("ai4ia_api.agents.capabilities.build_document_capability", unavailable)
    elif change in {"model_version", "model_disabled", "required_protocol"}:
        catalog = client.app.state.catalog
        entry = catalog.get(model)
        if change == "model_version":
            updated = entry.model_copy(update={
                "options": [option.model_copy(update={"modelVersion": "changed"}) for option in entry.options],
            })
        else:
            updated = entry.model_copy(update={
                "runtimeEnabled" if change == "model_disabled" else "requiredRealtimeProtocol":
                False if change == "model_disabled" else "ga",
            })
        catalog.models = [updated if item.id == model else item for item in catalog.models]
    response = client.post("/api/chat", json={
        "sessionId": sid, "content": "Calculate 2+2.", "stream": False,
        "params": {"max_tokens": 64},
    }, headers=headers("Consumer"))
    if change is None:
        assert response.status_code == 200, response.text
        assert len(calls) == 2
    else:
        assert response.status_code >= 400, response.text
        assert calls == []


@pytest.mark.parametrize("value", ["t", "y", "T", "Y", "true", "1", "on", "yes"])
def test_admin_boolean_aliases_require_the_same_additional_operation(published_api, value):
    client, _, headers, _ = published_api
    state = client.app.state
    config = json.loads(state.settings.group_policy_json)
    operations = ["admin.usage.read", "admin.mcp.inspect"]
    config["domains"]["admin"] = {"default": {"allow": operations}}
    config["adminCeiling"] = operations
    state.settings.group_policy_json = json.dumps(config)

    class Official:
        refreshes = 0

        def refresh(self):
            self.refreshes += 1

        async def list_all(self):
            return []

        async def close(self):
            return None

    official = Official()
    state.official_mcp_service = official
    auth = headers("Consumer")
    assert client.get("/api/admin/usage/user-agents?identify=false", headers=auth).status_code == 200
    assert client.get(f"/api/admin/usage/user-agents?identify={value}", headers=auth).status_code == 403
    assert client.get("/api/admin/metrics/official-mcp?refresh=false", headers=auth).status_code == 200
    assert client.get(f"/api/admin/metrics/official-mcp?refresh={value}", headers=auth).status_code == 403
    assert official.refreshes == 0
    config["adminCeiling"] = [*operations, "admin.mcp.refresh", "admin.directory.read"]
    config["domains"]["admin"]["default"]["allow"] = list(config["adminCeiling"])
    state.settings.group_policy_json = json.dumps(config)
    assert client.get(f"/api/admin/metrics/official-mcp?refresh={value}", headers=auth).status_code == 200
    assert official.refreshes == 1
    assert client.get(f"/api/admin/usage/user-agents?identify={value}", headers=auth).status_code == 200


def test_session_document_context_cannot_bypass_current_document_policy(published_api):
    client, model, headers, calls = published_api
    auth = headers("Consumer")
    sid = client.post("/api/sessions", json={"model": model}, headers=auth).json()["id"]
    marker = "synthetic-document-marker-not-in-the-request"
    uploaded = client.post(
        f"/api/sessions/{sid}/documents", headers=auth,
        files={"file": ("source.txt", marker.encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    body = {"sessionId": sid, "content": "Read the attached source.", "stream": False,
            "params": {"max_tokens": 64}}
    first = client.post("/api/chat", json=body, headers=auth)
    assert first.status_code == 200, first.text
    assert marker in json.dumps(calls)
    calls.clear()
    config = json.loads(client.app.state.settings.group_policy_json)
    config["domains"]["documents"] = {"default": {"allow": []}}
    client.app.state.settings.group_policy_json = json.dumps(config)
    assert client.get(f"/api/sessions/{sid}/documents", headers=auth).status_code == 403
    second = client.post("/api/chat", json=body, headers=auth)
    assert second.status_code == 200, second.text
    assert len(calls) == 1
    assert marker not in json.dumps(calls)
    assert "document context is unavailable" in json.dumps(calls)


def test_voice_profile_never_drops_an_unsupported_declared_tool(published_api):
    client, _, headers, _ = published_api
    model = next(entry.id for entry in client.app.state.catalog.models if entry.category == "realtime")
    created = client.post("/api/agents", headers=headers("Author"), json={
        "name": "voice-required", "systemPrompt": "Use the declared document tool.",
        "tools": ["process_document"],
    })
    assert created.status_code == 201, created.text
    body = {
        "expectedRevision": created.json()["revision"], "audience": {"visibility": "public"},
        "modelIds": [model], "modes": ["voice"], "reviewConsent": True, "skillMode": "excluded",
    }
    refused = client.post("/api/publications/agent/voice-required/submit", headers=headers("Author"), json=body)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "publication_voice_required_tool_unavailable"
    updated = client.put("/api/agents/voice-required", headers=headers("Author"), json={
        "systemPrompt": "Use the declared document tool.", "tools": [],
        "expectedRevision": created.json()["revision"],
    })
    assert updated.status_code == 200
    body["expectedRevision"] = updated.json()["revision"]
    allowed = client.post("/api/publications/agent/voice-required/submit", headers=headers("Author"), json=body)
    assert allowed.status_code == 200, allowed.text
