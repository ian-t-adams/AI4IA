"""Video generation and clip playback are retired; stored records stay untouched."""
from __future__ import annotations

import asyncio
import importlib.util
import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.consent_service import session_snapshot
from ai4ia_api.agents.synthetic_governance import synthetic_spec
from ai4ia_api.agents.tool_exec import (
    CHAT_ONLY_SYNTHETIC_TOOL_NAMES,
    SELECTABLE_SYNTHETIC_TOOL_NAMES,
    attachable_tool_names,
    build_tools,
)
from ai4ia_api.agents.user_agents import UserAgent
from ai4ia_api.catalog import DeploymentOption, ModelEntry
from ai4ia_api.config import Settings
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.sessions.models import MessageAttachment
from ai4ia_api.workflows.models import Workflow, WorkflowStep
from tests.conftest import make_settings

TOOL = "generate_video"
ARTIFACT = "a" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42legacy-clip"


class RetirementGateway:
    def __init__(self):
        self.offered = []
        self.tool_results = []
        self.calls = 0

    async def complete(self, *, messages, params=None, **kwargs):
        self.calls += 1
        self.offered.extend(
            item["function"]["name"] for item in (params or {}).get("tools", [])
        )
        self.tool_results = [item for item in messages if item["role"] == "tool"]
        message = (
            {"role": "assistant", "content": "The requested tool is unavailable."}
            if self.tool_results else
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "retired-call", "type": "function",
                "function": {"name": TOOL, "arguments": '{"prompt":"private prompt"}'},
            }]}
        )
        return {"choices": [{"message": message}]}


def test_generation_code_settings_and_transport_are_absent(monkeypatch):
    monkeypatch.setenv("AI4IA_VIDEO_GENERATION_ENABLED", "true")
    monkeypatch.setenv("AI4IA_GATEWAY_VIDEO_API_VERSION", "preview")
    settings = make_settings()
    assert not hasattr(settings, "video_generation_enabled")
    assert not any(name.startswith("gateway_video_") for name in Settings.model_fields)
    assert importlib.util.find_spec("ai4ia_api.videos") is None
    gateway = ModelGatewayClient(settings)
    for name in ("_video_url", "create_video_job", "get_video_job", "get_video_content"):
        assert not hasattr(gateway, name)
    for name in ("generate_image", "edit_image", "synthesize_speech", "transcribe"):
        assert callable(getattr(gateway, name))


def test_video_has_no_tool_registration_or_consent_contract():
    registry, executor = build_tools()
    assert registry.get(TOOL) is None
    assert executor.get(TOOL) is None
    assert synthetic_spec(TOOL) is None
    assert TOOL not in SELECTABLE_SYNTHETIC_TOOL_NAMES
    assert TOOL not in CHAT_ONLY_SYNTHETIC_TOOL_NAMES
    assert TOOL not in attachable_tool_names(registry, executor)
    assert "calculator" in attachable_tool_names(registry, executor)
    assert synthetic_spec("generate_image") is not None


@pytest.mark.parametrize("runtime_enabled", [False, True])
def test_old_catalog_or_environment_cannot_restore_tool_or_slash_command(
    monkeypatch, runtime_enabled,
):
    monkeypatch.setenv("AI4IA_VIDEO_GENERATION_ENABLED", "true")
    app = create_app(make_settings())
    gateway = RetirementGateway()
    with TestClient(app) as client:
        app.state.gateway = gateway
        app.state.catalog = app.state.catalog.model_copy(deep=True)
        app.state.catalog.models.append(ModelEntry(
            id="retained-video", displayName="Retained video", category="video", format="OpenAI",
            runtimeEnabled=runtime_enabled, options=[DeploymentOption(
                region="test", sku="GlobalStandard", deploymentName="retained-video-deployment",
            )],
        ))
        tools = client.get("/api/tools")
        assert tools.status_code == 200
        offered = {item["name"] for item in tools.json()["tools"]}
        assert TOOL not in offered
        assert "generate_image" in offered
        assert client.post("/api/agents", json={
            "name": "retired", "systemPrompt": "Create a clip.", "tools": [TOOL],
        }).status_code == 422
        assert client.post("/api/sessions", json={
            "model": "gpt-5.2", "toolOverrides": {"added": [TOOL]},
        }).status_code == 422
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        response = client.post("/api/chat", json={
            "sessionId": session["id"], "content": f"/{TOOL} a clip", "stream": False,
        })
        assert response.status_code == 200, response.text
        assert "Unknown" in response.json()["message"]["content"]
        assert gateway.calls == 0


@pytest.mark.parametrize("reference", ["agent", "conversation"])
def test_persisted_tool_reference_is_unoffered_and_refuses_before_inference(reference):
    app = create_app(make_settings())
    gateway = RetirementGateway()
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        source = UserAgent(
            id="legacy-creator", name="legacy-creator", displayName="Legacy creator",
            userId=session["userId"], systemPrompt="Use the requested tool.",
            tools=[TOOL, "calculator"],
        )
        if reference == "agent":
            asyncio.run(app.state.agent_service._store.put(source))
        else:
            asyncio.run(app.state.session_repo.patch_session(
                session["userId"], session["id"], {"toolOverrides": {"added": source.tools}},
            ))

        async def consent():
            saved = await app.state.session_repo.get_session(session["userId"], session["id"])
            if reference == "agent":
                saved.agentName = source.name
            return await session_snapshot(
                app.state, user_id=session["userId"], session=saved,
            )

        snapshot = asyncio.run(consent())
        assert TOOL not in snapshot.contracts
        assert "calculator" in snapshot.contracts
        response = client.post("/api/chat", json={
            "sessionId": session["id"], "stream": False,
            "content": "@legacy-creator make a clip" if reference == "agent" else "make a clip",
        })
        assert response.status_code == 422, response.text
        assert "generate_video is retired" in response.json()["detail"]
        assert gateway.calls == 0 and gateway.offered == []
        assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


def test_unoffered_retired_model_tool_call_still_fails_safely(client):
    gateway = RetirementGateway()
    client.app.state.gateway = gateway
    session = client.post("/api/sessions", json={
        "model": "gpt-5.2", "toolOverrides": {"added": ["calculator"]},
    }).json()
    response = client.post("/api/chat", json={
        "sessionId": session["id"], "content": "Answer a synthetic question.", "stream": False,
    })
    assert response.status_code == 200, response.text
    assert gateway.calls == 2 and TOOL not in gateway.offered
    assert "calculator" in gateway.offered
    assert "unknown_tool" in json.dumps(gateway.tool_results)
    assert not response.json()["message"].get("attachments")


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_legacy_builder_save_requires_explicit_retired_tool_removal(kind):
    app = create_app(make_settings())
    with TestClient(app) as client:
        owner = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()["userId"]
        common = dict(
            id="legacy-creator", name="legacy-creator", displayName="Legacy creator",
            userId=owner, description="Preserve this description.",
        )
        if kind == "agent":
            source = UserAgent(
                **common, systemPrompt="Use the requested tool.", tools=[TOOL, "calculator"],
            )
            service = app.state.agent_service
            list_path, items_key = "/api/agents/mine", "agents"
            save_path = f"/api/agents/{source.name}"
            payload = dict(
                systemPrompt=source.systemPrompt, tools=list(source.tools),
                displayName=source.displayName, description=source.description,
                expectedRevision=source.revision,
            )
        else:
            source = Workflow(**common, steps=[WorkflowStep(
                agent="general", instruction="Calculate {input}.",
                extraTools=[TOOL, "calculator"],
            )])
            service = app.state.workflow_service
            list_path, items_key = "/api/workflows", "workflows"
            save_path = f"/api/workflows/{source.name}"
            payload = dict(
                steps=[step.model_dump(mode="json") for step in source.steps],
                displayName=source.displayName, description=source.description,
                expectedRevision=source.revision,
            )
        asyncio.run(service._store.put(source))
        original = asyncio.run(service._store.get(owner, source.name)).model_dump(mode="json")

        loaded = client.get(list_path)
        assert loaded.status_code == 200, loaded.text
        record = next(item for item in loaded.json()[items_key] if item["name"] == source.name)
        tools = record["tools"] if kind == "agent" else record["steps"][0]["extraTools"]
        assert tools == [TOOL, "calculator"]
        assert asyncio.run(service._store.get(owner, source.name)).model_dump(mode="json") == original

        denied = client.put(save_path, json=payload)
        assert denied.status_code == 422, denied.text
        assert TOOL in denied.json()["detail"]
        assert asyncio.run(service._store.get(owner, source.name)).model_dump(mode="json") == original

        if kind == "agent":
            payload["tools"] = ["calculator"]
        else:
            payload["steps"][0]["extraTools"] = ["calculator"]
        saved = client.put(save_path, json=payload)
        assert saved.status_code == 200, saved.text
        stored = asyncio.run(service._store.get(owner, source.name))
        assert stored.revision == source.revision + 1
        assert stored.createdAt == source.createdAt
        assert stored.description == source.description
        if kind == "agent":
            assert saved.json()["tools"] == stored.tools == ["calculator"]
            assert stored.systemPrompt == source.systemPrompt
        else:
            assert saved.json()["steps"][0]["extraTools"] == stored.steps[0].extraTools == ["calculator"]
            assert stored.steps[0].agent == source.steps[0].agent
            assert stored.steps[0].instruction == source.steps[0].instruction


@pytest.mark.parametrize("tools_enabled", [False, True])
def test_legacy_conversation_selection_requires_explicit_retired_tool_removal(tools_enabled):
    app = create_app(make_settings())
    with TestClient(app) as client:
        session = client.post("/api/sessions", json={"model": "gpt-5.4"}).json()
        owner, identifier = session["userId"], session["id"]
        original = asyncio.run(app.state.session_repo.patch_session(
            owner, identifier, {
                "toolsEnabled": tools_enabled, "toolOverrides": {"added": [TOOL, "calculator"]},
            },
        ))
        for path in (f"/api/tools?sessionId={identifier}", f"/api/sessions/{identifier}/inspector"):
            loaded = client.get(path)
            assert loaded.status_code == 200, loaded.text
        assert asyncio.run(app.state.session_repo.get_session(owner, identifier)) == original
        denied = client.patch(f"/api/sessions/{identifier}", json={
            "toolOverrides": {"added": [TOOL, "calculator"]},
        })
        assert denied.status_code == 422, denied.text
        assert TOOL in denied.text
        assert asyncio.run(app.state.session_repo.get_session(owner, identifier)) == original
        saved = client.patch(f"/api/sessions/{identifier}", json={
            "toolOverrides": {"added": ["calculator"]},
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["toolOverrides"]["added"] == ["calculator"]
        stored = asyncio.run(app.state.session_repo.get_session(owner, identifier))
        assert stored.toolOverrides.added == ["calculator"]
        assert stored.toolsEnabled == original.toolsEnabled
        assert stored.model == original.model


@pytest.mark.parametrize("tools_enabled", [False, True])
def test_owned_inherited_retired_tool_can_be_excluded_without_editing_agent(tools_enabled):
    app = create_app(make_settings())
    with TestClient(app) as client:
        session = client.post("/api/sessions", json={"model": "gpt-5.4"}).json()
        owner, identifier = session["userId"], session["id"]
        agent = UserAgent(
            id="legacy-creator", name="legacy-creator", displayName="Legacy creator",
            userId=owner, systemPrompt="Calculate.", tools=[TOOL, "calculator"],
        )
        asyncio.run(app.state.agent_service._store.put(agent))
        original = asyncio.run(app.state.session_repo.patch_session(
            owner, identifier, {"agentName": agent.name, "toolsEnabled": tools_enabled},
        ))
        response = client.patch(f"/api/sessions/{identifier}", json={
            "toolOverrides": {"added": [], "removed": [TOOL]},
        })
        assert response.status_code == 200, response.text
        stored = asyncio.run(app.state.session_repo.get_session(owner, identifier))
        assert stored.agentName == original.agentName
        assert stored.toolsEnabled == original.toolsEnabled
        assert stored.toolOverrides.added == []
        assert stored.toolOverrides.removed == [TOOL]
        inspector = client.get(f"/api/sessions/{identifier}/inspector").json()
        assert TOOL not in inspector["tools"]["effective"]
        assert ("calculator" in inspector["tools"]["effective"]) is tools_enabled
        assert asyncio.run(app.state.agent_service._store.get(owner, agent.name)) == agent


def test_playback_route_store_and_settings_are_removed(monkeypatch):
    monkeypatch.setenv("AI4IA_VIDEO_BLOB_ACCOUNT_URL", "https://retained.blob.core.windows.net")
    monkeypatch.setenv("AI4IA_VIDEO_BLOB_CONTAINER", "legacy-videos")
    settings = make_settings()
    assert not any(name.startswith("video_blob_") for name in Settings.model_fields)
    assert importlib.util.find_spec("ai4ia_api.videos") is None
    assert importlib.util.find_spec("ai4ia_api.routers.videos") is None
    app = create_app(settings)
    gateway = RetirementGateway()
    with TestClient(app) as client:
        app.state.gateway = gateway
        assert not hasattr(app.state, "video_artifacts")
        assert not any(
            getattr(route, "path", "").startswith("/api/videos") for route in app.routes
        )
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            assert client.request(method, f"/api/videos/artifacts/{ARTIFACT}").status_code == 404
        assert client.post("/api/videos/generations", json={"prompt": "clip"}).status_code == 404
        assert client.get("/health/live").status_code == 200
        assert gateway.calls == 0


def test_stored_video_attachment_history_round_trips_unchanged():
    stored = {
        "id": ARTIFACT, "kind": "video", "mimeType": "video/mp4",
        "prompt": "private prompt", "durationSeconds": 4, "status": "succeeded",
    }
    attachment = MessageAttachment.model_validate(stored)
    dumped = attachment.model_dump(exclude_none=True)
    assert {key: dumped[key] for key in stored} == stored
