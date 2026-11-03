"""Video generation is retired; existing clips remain private and read-only."""
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
from ai4ia_api.auth.base import AuthError
from ai4ia_api.catalog import DeploymentOption, ModelCatalog, ModelEntry
from ai4ia_api.config import Settings
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.main import create_app
from ai4ia_api.videos.artifacts import (
    VIDEO_CONTENT_TYPE,
    VideoArtifactStore,
    artifact_path,
    build_video_blob_store,
)
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
    for module in ("service", "capability", "availability"):
        assert importlib.util.find_spec(f"ai4ia_api.videos.{module}") is None
    gateway = ModelGatewayClient(settings)
    for name in ("_video_url", "create_video_job", "get_video_job", "get_video_content"):
        assert not hasattr(gateway, name)
    for name in ("generate_image", "edit_image", "synthesize_speech", "transcribe"):
        assert callable(getattr(gateway, name))
    assert not hasattr(VideoArtifactStore, "put")
    assert callable(VideoArtifactStore.get)


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
def test_persisted_tool_reference_is_unoffered_and_unknown_calls_fail_safely(reference):
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
        assert response.status_code == 200, response.text
        assert TOOL not in gateway.offered
        assert "calculator" in gateway.offered
        assert gateway.calls == 2
        assert "unknown_tool" in json.dumps(gateway.tool_results)
        assert not response.json()["message"].get("attachments")


def test_legacy_playback_owner_auth_and_read_only_lifecycle(monkeypatch):
    class Blob(InMemoryBlobStore):
        closed = False

        async def close(self):
            self.closed = True
            await super().close()

    blob = Blob()
    monkeypatch.setattr("ai4ia_api.main.build_video_blob_store", lambda _: blob)
    app = create_app(make_settings())
    gateway = RetirementGateway()
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        asyncio.run(blob.put(artifact_path(session["userId"], ARTIFACT), MP4, VIDEO_CONTENT_TYPE))
        app.state.catalog = ModelCatalog(models=[])
        path = f"/api/videos/artifacts/{ARTIFACT}"
        response = client.get(path)
        assert response.status_code == 200
        assert response.content == MP4
        assert response.headers["content-type"] == VIDEO_CONTENT_TYPE
        assert response.headers["cache-control"].startswith("private")
        assert client.get(path, headers={"X-Dev-User": "other-owner"}).status_code == 404
        for invalid in ("missing", "b" * 32, "A" * 32, "." * 32):
            assert client.get(f"/api/videos/artifacts/{invalid}").status_code == 404
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            assert client.request(method, path).status_code == 405
        assert client.post("/api/videos/generations", json={"prompt": "clip"}).status_code == 404
        assert gateway.calls == 0

        class NoAuth:
            async def authenticate(self, credentials):
                raise AuthError("No credentials.")

        app.state.auth_provider = NoAuth()
        assert client.get(path).status_code == 401
    assert blob.closed


def test_legacy_blob_factory_preserves_configured_account_and_container(monkeypatch):
    blob = InMemoryBlobStore()
    calls = []

    def factory(account, container):
        calls.append((account, container))
        return blob

    monkeypatch.setattr("ai4ia_api.videos.artifacts.AzureBlobStore", factory)
    settings = make_settings(
        video_blob_account_url="https://retained.blob.core.windows.net",
        video_blob_container="legacy-videos",
    )
    assert build_video_blob_store(settings) is blob
    assert calls == [(settings.video_blob_account_url, "legacy-videos")]
    assert isinstance(build_video_blob_store(make_settings()), InMemoryBlobStore)
