"""Media generation is opt-in; retained artifacts remain owner-readable."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.consent_service import session_snapshot
from ai4ia_api.agents.tool_exec import ToolContext
from ai4ia_api.entitlements.models import EntitlementDecision
from ai4ia_api.images.capability import build_image_capability
from ai4ia_api.images.service import ImageGenerationService
from ai4ia_api.main import create_app
from ai4ia_api.videos.capability import build_video_capability
from ai4ia_api.videos.service import VideoGenerationService
from tests.conftest import make_settings
from tests.test_image_api import TINY_PNG_B64
from tests.test_video_tool import FAKE_MP4


class MediaGateway:
    def __init__(self, kind):
        self.tool = f"generate_{kind}"
        self.offered: list[str] = []
        self.model_calls = 0
        self.paid_calls: list[str] = []

    async def complete(self, *, messages, params=None, **kwargs):
        self.model_calls += 1
        offered = [tool["function"]["name"] for tool in (params or {}).get("tools", [])]
        self.offered.extend(offered)
        if self.tool in offered and not any(message["role"] == "tool" for message in messages):
            message = {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "media-call", "type": "function",
                    "function": {"name": self.tool, "arguments": '{"prompt":"a red bird"}'},
                }],
            }
        else:
            message = {"role": "assistant", "content": "Done."}
        return {"choices": [{"message": message}]}

    async def generate_image(self, **kwargs):
        self.paid_calls.append("image")
        return {"data": [{"b64_json": TINY_PNG_B64}]}

    async def create_video_job(self, **kwargs):
        self.paid_calls.append("video")
        return {"id": "job", "status": "completed"}

    async def get_video_content(self, **kwargs):
        return FAKE_MP4


def test_media_settings_are_default_off_and_read_explicit_env(monkeypatch):
    monkeypatch.delenv("AI4IA_IMAGE_GENERATION_ENABLED", raising=False)
    monkeypatch.delenv("AI4IA_VIDEO_GENERATION_ENABLED", raising=False)
    settings = make_settings()
    assert settings.image_generation_enabled is False
    assert settings.video_generation_enabled is False
    monkeypatch.setenv("AI4IA_IMAGE_GENERATION_ENABLED", "true")
    monkeypatch.setenv("AI4IA_VIDEO_GENERATION_ENABLED", "false")
    settings = make_settings()
    assert settings.image_generation_enabled is True
    assert settings.video_generation_enabled is False


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("env", ["local", "prod"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("blob_url", [None, "https://media.blob.core.windows.net"])
def test_enabled_media_requires_durable_blob_outside_local(kind, env, enabled, blob_url):
    settings = make_settings(
        env=env,
        model_gateway_url="https://proxy.test/openai",
        model_gateway_allowed_hosts="proxy.test",
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="test-proxy-key",
        model_gateway_api_key_header="S7P-KEY",
        **{f"{kind}_generation_enabled": enabled, f"{kind}_blob_account_url": blob_url},
    )
    if enabled and env != "local" and blob_url is None:
        with pytest.raises(RuntimeError, match=f"AI4IA_{kind.upper()}_BLOB_ACCOUNT_URL"):
            settings.validate_runtime()
    else:
        settings.validate_runtime()


@pytest.mark.parametrize("enabled", [False, True])
def test_direct_image_creation_and_options_follow_generation_flag(enabled):
    app = create_app(make_settings(image_generation_enabled=enabled))
    gateway = MediaGateway("image")
    with TestClient(app) as client:
        app.state.gateway = gateway
        assert app.state.image_artifacts is not None
        options = client.get("/api/images/options")
        response = client.post("/api/images/generations", json={"prompt": "a red bird"})
        assert response.status_code == (200 if enabled else 404), response.text
        assert gateway.paid_calls == (["image"] if enabled else [])
        assert options.status_code == 200
        assert options.json()["enabled"] is enabled
        assert bool(options.json()["models"]) is enabled


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("enabled", [False, True])
def test_tool_catalog_inspector_and_consent_follow_media_flag(kind, enabled):
    tool = f"generate_{kind}"
    app = create_app(make_settings(**{f"{kind}_generation_enabled": enabled}))
    with TestClient(app) as client:
        assert getattr(app.state, f"{kind}_artifacts") is not None
        catalog = client.get("/api/tools").json()
        item = next(item for item in catalog["tools"] if item["name"] == tool)
        assert item["available"] is enabled
        created = client.post(
            "/api/sessions", json={"model": "gpt-5.2", "toolOverrides": {"added": [tool]}},
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        inspector = client.get(f"/api/sessions/{session_id}/inspector").json()
        assert (tool in inspector["tools"]["effective"]) is enabled
        assert tool in inspector["tools"]["added"]

        async def snapshot():
            session = await app.state.session_repo.get_session(created.json()["userId"], session_id)
            return await session_snapshot(app.state, user_id=session.userId, session=session)

        assert (tool in asyncio.run(snapshot()).contracts) is enabled


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("invocation", ["command", "agent"])
def test_chat_only_advertises_and_executes_enabled_media(kind, enabled, invocation):
    tool = f"generate_{kind}"
    settings = make_settings(**{f"{kind}_generation_enabled": enabled})
    app = create_app(settings)
    gateway = MediaGateway(kind)
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        if invocation == "agent":
            agent = client.post(
                "/api/agents",
                json={"name": "creator", "systemPrompt": "Create media.", "tools": [tool]},
            )
            assert agent.status_code == 201, agent.text
            content = "@creator a red bird"
        else:
            content = f"/{tool} a red bird"
        response = client.post(
            "/api/chat", json={"sessionId": session["id"], "content": content, "stream": False},
        )
        assert response.status_code == 200, response.text
        assert (tool in gateway.offered) is enabled
        assert gateway.paid_calls == ([kind] if enabled else [])
        if not enabled and invocation == "command":
            assert gateway.model_calls == 0
            assert "isn't enabled" in response.json()["message"]["content"]
        if enabled:
            attachment = response.json()["message"]["attachments"][0]
            setattr(settings, f"{kind}_generation_enabled", False)
            path = f"/api/{kind}s/artifacts/{attachment['id']}"
            assert client.get(path).status_code == 200
            assert client.get(path, headers={"X-Dev-User": "another-user"}).status_code == 404


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("disable_at", [None, "before_handler", "during_entitlement"])
def test_stale_media_handler_rechecks_enabled_flag(kind, disable_at):
    settings = make_settings(**{f"{kind}_generation_enabled": True})
    app = create_app(settings)
    gateway = MediaGateway(kind)
    with TestClient(app):
        class Entitlements:
            async def check(self, user_id):
                if disable_at == "during_entitlement":
                    setattr(settings, f"{kind}_generation_enabled", False)
                return EntitlementDecision.allow()

        if kind == "image":
            service = ImageGenerationService(
                settings=settings, catalog=app.state.catalog, gateway=gateway,
            )
            build = build_image_capability
        else:
            service = VideoGenerationService(
                settings=settings, catalog=app.state.catalog, gateway=gateway,
            )
            build = build_video_capability
        args = {
            f"{kind}_service": service,
            "artifact_store": getattr(app.state, f"{kind}_artifacts"),
            "entitlements": Entitlements(), "metering": app.state.usage,
            "catalog": app.state.catalog, "user_id": "u1", "session_id": "s1", "sink": [],
        }
        tools, handlers = build(**args)
        assert [tool["function"]["name"] for tool in tools] == [f"generate_{kind}"]
        if disable_at == "before_handler":
            setattr(settings, f"{kind}_generation_enabled", False)
        result = asyncio.run(handlers[f"generate_{kind}"]({"prompt": "a red bird"}, ToolContext()))
        if disable_at is None:
            assert result["status"] == "generated"
            assert gateway.paid_calls == [kind]
        else:
            assert "disabled" in json.dumps(result).lower()
            assert gateway.paid_calls == []
            assert build(**args) == ([], {})
