"""Durable tool denials compose with requests without changing memory or grants."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.routers.chat import _chat_session_binding
from ai4ia_api.agents.consent import contract_hash
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Session, ToolOverrides
from ai4ia_api.sessions.repository import SessionConflictError, SessionNotFoundError
from tests.conftest import make_settings
from tests.cosmos_deletion_fake import CosmosState
from tests.test_chat_continuation import resume
from tests.test_tool_approval_gate import _bootstrap, _client, _connector, _hold, _InjectedModelGateway
from tests.test_chat_websearch_api import FakeWebClient, ScriptedWebGateway, _inject_web
from tests.test_chat_memory_api import CapturingGateway
from tests.test_memory_preference import RecordingEmbedder
from ai4ia_api.memory.in_memory import InMemoryVectorStore
from ai4ia_api.memory.service import MemoryService


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("request_allowed", [False, True])
def test_off_on_retains_selections_and_controls_actual_ambient_offers_and_dispatch(stream, request_allowed):
    app = create_app(make_settings(tool_approval_mode="off"))
    with TestClient(app) as client:
        web = FakeWebClient()
        _inject_web(client, web)
        session = client.post("/api/sessions", json={
            "title": "Synthetic", "model": "gpt-5.2", "libraryDocumentIds": [],
            "toolsEnabled": False, "toolOverrides": {"added": ["calculator"], "removed": ["browse_url"]},
        }).json()
        path = f"/api/sessions/{session['id']}"
        for enabled in (False, True, False):
            assert client.patch(path, json={"toolsEnabled": enabled}).status_code == 200
            gateway = ScriptedWebGateway()
            app.state.gateway = gateway
            web.calls.clear()
            result = client.post("/api/chat", json={
                "sessionId": session["id"], "content": "A synthetic question",
                "stream": stream, "allowTools": request_allowed, "allowAutomaticMemory": False,
            })
            assert result.status_code == 200, result.text
            allowed = enabled and request_allowed
            assert bool(web.calls) is allowed
            offered = {item["function"]["name"] for item in gateway.first_tools}
            assert ("web_search" in offered) is allowed
            assert ("calculator" in offered) is allowed
            assert "browse_url" not in offered
            receipt = client.get(path + "/messages").json()[-1]["executionReceipt"]
            assert bool(receipt["toolsOffered"]) is allowed
            assert bool(receipt["toolCalls"]) is allowed
            saved = client.get(path).json()
            assert saved["toolsEnabled"] is enabled
            assert saved["toolOverrides"] == session["toolOverrides"]
            assert saved["toolConsent"] is None
            snapshot = client.get(path + "/inspector").json()["tools"]
            assert snapshot["enabled"] is enabled
            assert snapshot["overrides"] == session["toolOverrides"]
            assert "calculator" in snapshot["selected"]
            assert bool(snapshot["effective"]) is enabled
            assert snapshot["voiceEffective"] == ["calculator"]


def test_durable_off_reaches_adapted_provider_schema():
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Synthetic"}}],
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Synthetic"}]}],
        })

    app = create_app(make_settings())
    with TestClient(app) as client:
        _inject_web(client, FakeWebClient())
        app.state.gateway = ModelGatewayClient(
            make_settings(model_gateway_url="https://gateway.example"),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
        )
        session = client.post("/api/sessions", json={
            "title": "Synthetic", "model": "gpt-5.2", "libraryDocumentIds": [],
        }).json()
        for enabled in (True, False):
            client.patch(f"/api/sessions/{session['id']}", json={"toolsEnabled": enabled})
            response = client.post("/api/chat", json={
                "sessionId": session["id"], "content": "Synthetic", "stream": False,
                "allowTools": True, "allowAutomaticMemory": False,
            })
            assert response.status_code == 200, response.text
            assert bool(requests[-1].get("tools")) is enabled
            if not enabled:
                assert not {"tools", "tool_choice", "parallel_tool_calls"} & requests[-1].keys()


@pytest.mark.parametrize("stream", [False, True])
def test_tools_off_does_not_disable_or_reenable_automatic_memory(stream):
    app = create_app(make_settings())
    with TestClient(app) as client:
        embedder = RecordingEmbedder()
        app.state.memory = MemoryService(store=InMemoryVectorStore(), embedder=embedder)
        app.state.gateway = CapturingGateway()
        session = client.post("/api/sessions", json={
            "title": "Synthetic", "model": "gpt-5.2", "toolsEnabled": False, "libraryDocumentIds": [],
        }).json()
        for memory_enabled in (True, False):
            preference = client.get("/api/memories/preference").json()
            if not memory_enabled:
                response = client.patch("/api/memories/preference",
                                        headers={"If-Match": preference["etag"]},
                                        json={"automaticMemoryEnabled": False})
                assert response.status_code == 200, response.text
                preference = response.json()
            for tools_enabled in (False, True):
                client.patch(f"/api/sessions/{session['id']}", json={"toolsEnabled": tools_enabled})
                embedder.calls.clear()
                response = client.post("/api/chat", json={
                    "sessionId": session["id"], "content": "Synthetic durable memory fact",
                    "stream": stream,
                })
                assert response.status_code == 200, response.text
                assert bool(embedder.calls) is memory_enabled
                assert client.get("/api/memories/preference").json() == preference


@pytest.mark.parametrize("backend", ["local", "cosmos"])
@pytest.mark.parametrize("v1", [False, True])
async def test_persistence_ownership_and_off_on_revision(backend, v1):
    state = CosmosState()
    repo = state.repo(enabled=v1) if backend == "cosmos" else InMemorySessionRepository(deletion_enabled=v1)
    created = await repo.create_session(Session(
        userId="owner", toolsEnabled=False, toolOverrides=ToolOverrides(added=["calculator"]),
    ))
    assert not (await repo.get_session("owner", created.id)).toolsEnabled
    with pytest.raises(SessionNotFoundError):
        await repo.patch_session("other", created.id, {"toolsEnabled": True})
    before = await repo.patch_session("owner", created.id, {"toolsEnabled": True})
    await repo.patch_session("owner", created.id, {"toolsEnabled": False})
    after = await repo.patch_session("owner", created.id, {"toolsEnabled": True})
    assert before.toolsRevision != after.toolsRevision
    assert _chat_session_binding(before) != _chat_session_binding(after)
    assert "toolsRevision" not in after.model_dump()
    await repo.patch_session("owner", created.id, {"title": "Unrelated"})
    saved = await repo.get_session("owner", created.id)
    assert saved.toolsRevision == after.toolsRevision and saved.toolsEnabled
    assert saved.toolOverrides == created.toolOverrides
    if backend == "cosmos":
        restarted = state.repo(enabled=v1)
        assert await restarted.get_session("owner", created.id) == saved
        assert repo._to_doc(saved)["toolsRevision"] == saved.toolsRevision
    with pytest.raises(ValueError):
        await repo.patch_session("owner", created.id, {"toolsRevision": None})


def test_legacy_binding_and_default_are_unchanged():
    session = Session(userId="owner")
    old = session.model_dump(mode="json", exclude={"title", "createdAt", "updatedAt", "toolsEnabled"})
    assert session.toolsEnabled
    assert _chat_session_binding(session) == contract_hash(old)


@pytest.mark.parametrize("race", [False, True])
async def test_full_cosmos_settings_update_is_atomic_and_retains_private_fields(race):
    state = CosmosState()
    repo = state.repo(enabled=False)
    session = await repo.create_session(Session(userId="owner", freshTurnClaimed=True))
    key = ("owner", session.id)
    state.sessions.items[key]["futureField"] = {"keep": True}

    async def race_replace(_item, _body):
        state.sessions.before_replace = None
        await repo.patch_session("owner", session.id, {"title": "Concurrent"})

    if race:
        state.sessions.before_replace = race_replace
    original_patch = state.sessions.patch_item

    async def bounded_patch(**kwargs):
        assert len(kwargs["patch_operations"]) <= 10
        return await original_patch(**kwargs)

    state.sessions.patch_item = bounded_patch
    changes = {
        "title": "Synthetic", "model": "gpt-5.2", "systemPrompt": "Synthetic",
        "agentName": None, "agentVersion": None, "toolsEnabled": False,
        "toolOverrides": {"added": ["calculator"], "removed": []},
        "libraryDocumentIds": [], "imagePreferences": {},
    }
    if race:
        with pytest.raises(SessionConflictError):
            await repo.patch_session("owner", session.id, changes)
    else:
        await repo.patch_session("owner", session.id, changes)
    saved = await repo.get_session("owner", session.id)
    assert saved.freshTurnClaimed
    assert saved.toolsEnabled is race
    assert (saved.toolsRevision is not None) is not race
    assert state.sessions.items[key]["futureField"] == {"keep": True}
    assert saved.title == ("Concurrent" if race else "Synthetic")


def test_durable_off_refuses_required_agent_and_commands_without_rewriting_source(client):
    agent = client.post("/api/agents", json={
        "name": "required", "systemPrompt": "Calculate.", "tools": ["calculator"],
    })
    assert agent.status_code == 201
    session = client.post("/api/sessions", json={
        "model": "gpt-5.2", "agentName": "required", "toolsEnabled": False,
    }).json()
    for content in ("Synthetic question", "/calculator 1+1"):
        response = client.post("/api/chat", json={"sessionId": session["id"], "content": content})
        assert response.status_code == 422, response.text
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []
    assert client.get(f"/api/sessions/{session['id']}/inspector").json()["tools"]["requiresTools"]
    assert client.get(f"/api/sessions/{session['id']}").json()["agentName"] == "required"


@pytest.mark.parametrize("value", [None, 0, 1, "false", {}, []])
def test_switch_strict_create_patch_and_unrelated_fields(client, value):
    assert client.post("/api/sessions", json={"toolsEnabled": value}).status_code == 422
    session = client.post("/api/sessions", json={}).json()
    assert session["toolsEnabled"] is True
    assert client.patch(f"/api/sessions/{session['id']}", json={"toolsEnabled": value}).status_code == 422
    for field in ("toolsRevision", "allowTools", "allowAutomaticMemory"):
        assert client.patch(f"/api/sessions/{session['id']}", json={field: False}).status_code == 422


@pytest.mark.parametrize("restore", [False, True])
def test_pending_original_operation_cannot_execute_after_tools_off_even_if_restored(restore):
    connector = _connector()
    client = _client(connector)
    try:
        session = _bootstrap(client)
        gateway = _InjectedModelGateway()
        client.app.state.gateway = gateway
        prompt = _hold(client, session)
        assert gateway.calls == 1
        assert client.patch(f"/api/sessions/{session}", json={"toolsEnabled": False}).status_code == 200
        if restore:
            assert client.patch(f"/api/sessions/{session}", json={"toolsEnabled": True}).status_code == 200
        result = resume(client, session, prompt)
        assert result.status_code == 409, result.text
        assert connector.tool_calls == [] and gateway.calls == 1
        assert resume(client, session, prompt).status_code == 409
        assert len(client.get(f"/api/sessions/{session}/messages").json()) == 2
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("change", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_tools_turned_off_while_model_runs_refuse_actual_synthetic_dispatch(change, stream):
    app = create_app(make_settings(tool_approval_mode="off"))
    with TestClient(app) as client:
        web = FakeWebClient()
        _inject_web(client, web)
        session = client.post("/api/sessions", json={"title": "Synthetic", "model": "gpt-5.2"}).json()

        class ChangingGateway(ScriptedWebGateway):
            async def complete(self, **kwargs):
                response = await super().complete(**kwargs)
                if change and self.calls == 1:
                    await app.state.session_repo.patch_session(session["userId"], session["id"], {"toolsEnabled": False})
                return response

        gateway = ChangingGateway()
        app.state.gateway = gateway
        response = client.post("/api/chat", json={
            "sessionId": session["id"], "content": "Synthetic", "stream": stream, "allowAutomaticMemory": False,
        })
        assert response.status_code == (502 if change and not stream else 200), response.text
        assert bool(web.calls) is not change
        assert gateway.calls == (1 if change else 2)
        assert gateway.tools_offered_first_call
