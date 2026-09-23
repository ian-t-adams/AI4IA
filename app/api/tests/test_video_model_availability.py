"""Runtime model availability gates ``generate_video`` at every seam, not just the flag.

Each case runs with ``AI4IA_VIDEO_GENERATION_ENABLED`` on and the durable artifact
store present, against the same catalog fixture with only the video model's
``runtimeEnabled`` flipped. The enabled control proves every seam really offers and
runs the tool; the disabled case proves it disappears everywhere, that a direct
execution attempt is refused before any provider call, and that previously
generated clips stay readable through the authenticated endpoint. Turning the flag
off instead would also drop the deployed blob account setting and hide those clips.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.consent_service import _chat_schemas, session_snapshot
from ai4ia_api.agents.tool_exec import ToolContext
from ai4ia_api.agents.user_agents import UserAgentCreate
from ai4ia_api.entitlements.models import EntitlementDecision
from ai4ia_api.main import create_app
from ai4ia_api.policy.context import clear_policy_context
from ai4ia_api.publishing.models import PublicationError, PublicationSubmit, PublicationVersion
from ai4ia_api.publishing.service import PublicationService
from ai4ia_api.publishing.store import RecordQuery
from ai4ia_api.videos.availability import NO_VIDEO_MODEL_DETAIL, state_video_availability
from ai4ia_api.videos.capability import build_video_capability
from ai4ia_api.videos.service import VideoGenerationError, VideoGenerationService
from ai4ia_api.workflows.record_types import PUBLICATION_VERSION_KIND
from tests.conftest import make_settings
from tests.test_media_feature_gates import MediaGateway
from tests.test_publication_service import actor
from tests.test_video_tool import FAKE_MP4, FakeVideoGateway
from tests.video_catalog import video_catalog_path

TOOL = "generate_video"
RETAINED_ARTIFACT = "c" * 32
UNAVAILABLE_REPLY = f"/{TOOL} is unavailable. {NO_VIDEO_MODEL_DETAIL}"


def _app(tmp_path, runtime_enabled: bool):
    return create_app(make_settings(
        video_generation_enabled=True,
        model_catalog_path=video_catalog_path(tmp_path, runtime_enabled=runtime_enabled),
    ))


def _tool_item(client: TestClient, query: str = "") -> dict:
    tools = client.get(f"/api/tools{query}").json()["tools"]
    return next(item for item in tools if item["name"] == TOOL)


async def _noop_sleep(_seconds: float) -> None:
    return None


class ForcedVideoCallGateway(MediaGateway):
    """Emits a generate_video call whether or not the tool was offered."""

    async def complete(self, *, messages, params=None, **kwargs):
        self.model_calls += 1
        self.offered.extend(tool["function"]["name"] for tool in (params or {}).get("tools", []))
        if any(message["role"] == "tool" for message in messages):
            return {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}
        return {"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "forced-video", "type": "function",
                "function": {"name": TOOL, "arguments": '{"prompt":"a red bird","model":"sora-2"}'},
            }],
        }}]}


@pytest.fixture(params=[True, False], ids=["model-enabled", "model-disabled"])
def video_app(request, tmp_path):
    """Flag on and store present; only the video model's ``runtimeEnabled`` differs."""
    app = _app(tmp_path, request.param)
    with TestClient(app) as client:
        assert app.state.settings.video_generation_enabled is True
        assert app.state.video_artifacts is not None
        yield client, request.param


def _session_with_tool(client: TestClient) -> tuple[str, str]:
    created = client.post(
        "/api/sessions", json={"model": "gpt-5.2", "toolOverrides": {"added": [TOOL]}},
    )
    assert created.status_code == 201, created.text
    return created.json()["userId"], created.json()["id"]


def test_shared_predicate_reports_the_runtime_model(video_app):
    client, enabled = video_app
    state = client.app.state
    assert state_video_availability(state) == ("available" if enabled else "no_model")
    assert state_video_availability(state, policy_filter=False) == (
        "available" if enabled else "no_model"
    )


def test_model_listing_follows_the_runtime_model(video_app):
    client, enabled = video_app
    models = {model["id"] for model in client.get("/api/models").json()["models"]}
    assert ("sora-2" in models) is enabled
    assert "gpt-5.2" in models


def test_tool_catalog_follows_the_runtime_model(video_app):
    client, enabled = video_app
    item = _tool_item(client)
    assert item["available"] is enabled
    assert item["selectable"] is True
    assert item["detail"] == (None if enabled else NO_VIDEO_MODEL_DETAIL)


def test_conversation_policy_and_inspector_follow_the_runtime_model(video_app):
    client, enabled = video_app
    _, session_id = _session_with_tool(client)
    inspector = client.get(f"/api/sessions/{session_id}/inspector").json()
    assert TOOL in inspector["tools"]["added"]
    assert (TOOL in inspector["tools"]["effective"]) is enabled
    assert _tool_item(client, f"?sessionId={session_id}")["available"] is enabled


def test_consent_snapshot_follows_the_runtime_model(video_app):
    client, enabled = video_app
    state = client.app.state
    user_id, session_id = _session_with_tool(client)

    async def snapshot():
        session = await state.session_repo.get_session(user_id, session_id)
        return await session_snapshot(state, user_id=user_id, session=session)

    assert (TOOL in asyncio.run(snapshot()).contracts) is enabled


def test_publication_metadata_follows_the_runtime_model(video_app):
    client, enabled = video_app
    state = client.app.state
    user_id, session_id = _session_with_tool(client)

    async def published():
        session = await state.session_repo.get_session(user_id, session_id)
        return await _chat_schemas(
            state, user_id=user_id, session=session, tool_names=[TOOL], email=None,
            include_attachments=False, publication_metadata=True,
        )

    names = [schema["function"]["name"] for schema in asyncio.run(published())]
    assert names == ([TOOL] if enabled else [])


@pytest.mark.parametrize("runtime_enabled", [True, False])
def test_publication_compiles_the_tool_only_with_a_runtime_model(tmp_path, runtime_enabled):
    config = {"domains": {"publication": {
        "default": {"allow": ["consume"]},
        "mappings": [{"claim": "roles", "value": "Author", "allow": ["submit"]}],
    }}}
    app = create_app(make_settings(
        auth_provider="entra", entra_tenant_id="tenant", entra_audience="api://test",
        group_policy_enabled=True, asset_publishing_enabled=True,
        group_policy_json=json.dumps(config), video_generation_enabled=True,
        model_catalog_path=video_catalog_path(tmp_path, runtime_enabled=runtime_enabled),
    ))
    try:
        with TestClient(app):
            state = app.state
            service = PublicationService(
                state, agents=state.agent_service._store.records,
                workflows=state.workflow_service._store.records,
            )

            async def submit():
                author = await actor(service, "author", "Author")
                source = await state.agent_service.create(
                    author.owner_id,
                    UserAgentCreate(name="clip-maker", systemPrompt="Make clips.", tools=[TOOL]),
                    reserved_names=set(),
                )
                model = next(
                    entry.id for entry in state.catalog.models
                    if entry.supportsTools and all(option.modelVersion for option in entry.options)
                )
                await service.submit(author, "agent", source.name, PublicationSubmit(
                    expectedRevision=source.revision,
                    audience={"visibility": "shared", "acl": ["consumer@example.com"]},
                    modelIds=[model], modes=["chat"], reviewConsent=True,
                ))
                rows = await service._store("agent").query(RecordQuery(
                    PUBLICATION_VERSION_KIND, owner_id=author.owner_id,
                ))
                return PublicationVersion.model_validate(rows[0].body)

            if not runtime_enabled:
                with pytest.raises(PublicationError, match="publication_tool_unavailable"):
                    asyncio.run(submit())
                return
            version = asyncio.run(submit())
            assert TOOL in {tool.name for tool in version.profiles["chat"].tools}
    finally:
        clear_policy_context()


@pytest.mark.parametrize("runtime_enabled", [True, False])
@pytest.mark.parametrize("invocation", ["command", "agent"])
def test_chat_offers_and_runs_the_tool_only_with_a_runtime_model(tmp_path, runtime_enabled, invocation):
    app = _app(tmp_path, runtime_enabled)
    gateway = MediaGateway("video")
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        if invocation == "agent":
            # Attaching stays allowed: a saved agent is not rewritten by a retirement.
            agent = client.post(
                "/api/agents",
                json={"name": "creator", "systemPrompt": "Create media.", "tools": [TOOL]},
            )
            assert agent.status_code == 201, agent.text
            content = "@creator a red bird"
        else:
            content = f"/{TOOL} a red bird"
        response = client.post(
            "/api/chat", json={"sessionId": session["id"], "content": content, "stream": False},
        )
        assert response.status_code == 200, response.text
        message = response.json()["message"]
        assert (TOOL in gateway.offered) is runtime_enabled
        assert gateway.paid_calls == (["video"] if runtime_enabled else [])
        if runtime_enabled:
            assert message["attachments"][0]["kind"] == "video"
            assert message["attachments"][0]["model"] == "sora-2"
        elif invocation == "command":
            assert gateway.model_calls == 0
            assert message["content"] == UNAVAILABLE_REPLY
        else:
            assert gateway.model_calls == 1
            assert not message.get("attachments")


@pytest.mark.parametrize("runtime_enabled", [True, False])
def test_an_unoffered_model_tool_call_never_reaches_the_provider(tmp_path, runtime_enabled):
    app = _app(tmp_path, runtime_enabled)
    gateway = ForcedVideoCallGateway("video")
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        # A second tool keeps the agent loop running when generate_video is withheld,
        # so the forced call reaches dispatch instead of a tool-free completion.
        agent = client.post(
            "/api/agents",
            json={"name": "creator", "systemPrompt": "Create media.", "tools": [TOOL, "calculator"]},
        )
        assert agent.status_code == 201, agent.text
        response = client.post(
            "/api/chat",
            json={"sessionId": session["id"], "content": "@creator a red bird", "stream": False},
        )
        assert response.status_code == 200, response.text
        assert gateway.model_calls == 2
        assert "calculator" in gateway.offered
        assert (TOOL in gateway.offered) is runtime_enabled
        assert gateway.paid_calls == (["video"] if runtime_enabled else [])
        assert bool(response.json()["message"].get("attachments")) is runtime_enabled


@pytest.mark.parametrize("runtime_enabled", [True, False])
@pytest.mark.parametrize("model", [None, "sora-2"])
def test_direct_generation_is_refused_before_any_provider_call(tmp_path, runtime_enabled, model):
    app = _app(tmp_path, runtime_enabled)
    gateway = FakeVideoGateway()
    with TestClient(app):
        service = VideoGenerationService(
            settings=app.state.settings, catalog=app.state.catalog, gateway=gateway,
            poll_interval_seconds=0.0, max_wait_seconds=1.0, sleep=_noop_sleep,
        )
        if runtime_enabled:
            result = asyncio.run(service.generate(prompt="a red bird", model=model, size=None))
            assert result.model_id == "sora-2"
            assert result.video_bytes == FAKE_MP4
            assert gateway.calls
            return
        with pytest.raises(VideoGenerationError) as refused:
            asyncio.run(service.generate(prompt="a red bird", model=model, size=None))
        assert refused.value.status_code == 400
        assert refused.value.detail == (
            NO_VIDEO_MODEL_DETAIL if model is None else "Unknown or unavailable model: sora-2"
        )
        assert refused.value.provider_completion is None
        assert gateway.calls == []


@pytest.mark.parametrize("disable_at", [None, "before_handler", "during_entitlement"])
def test_a_stale_offer_rechecks_the_model_before_any_provider_call(tmp_path, disable_at):
    app = _app(tmp_path, True)
    gateway = MediaGateway("video")
    with TestClient(app):
        catalog = app.state.catalog
        entry = catalog.get("sora-2")
        assert entry is not None
        checks: list[str] = []

        class Entitlements:
            async def check(self, user_id):
                checks.append(user_id)
                if disable_at == "during_entitlement":
                    entry.runtimeEnabled = False
                return EntitlementDecision.allow()

        args = {
            "video_service": VideoGenerationService(
                settings=app.state.settings, catalog=catalog, gateway=gateway,
            ),
            "artifact_store": app.state.video_artifacts, "entitlements": Entitlements(),
            "metering": app.state.usage, "catalog": catalog, "user_id": "u1",
            "session_id": "s1", "sink": [],
        }
        tools, handlers = build_video_capability(**args)
        assert [tool["function"]["name"] for tool in tools] == [TOOL]
        assert "Available video models: sora-2." in tools[0]["function"]["description"]
        if disable_at == "before_handler":
            entry.runtimeEnabled = False
        result = asyncio.run(handlers[TOOL]({"prompt": "a red bird"}, ToolContext()))
        if disable_at is None:
            assert result["status"] == "generated"
            assert gateway.paid_calls == ["video"]
            assert checks == ["u1"]
            return
        assert result == {"error": NO_VIDEO_MODEL_DETAIL}
        assert gateway.paid_calls == []
        # Gone before the handler: its own re-check refuses first. Gone during the
        # entitlement await: the service's resolution is the backstop.
        assert checks == ([] if disable_at == "before_handler" else ["u1"])
        assert build_video_capability(**args) == ([], {})


@pytest.mark.parametrize("runtime_enabled", [True, False])
def test_previously_generated_clips_stay_viewable(tmp_path, runtime_enabled):
    app = _app(tmp_path, runtime_enabled)
    with TestClient(app) as client:
        owner = {"X-Dev-User": "owner"}
        owner_id = client.get("/api/entitlement", headers=owner).json()["userId"]
        asyncio.run(app.state.video_artifacts.put(owner_id, RETAINED_ARTIFACT, FAKE_MP4))
        path = f"/api/videos/artifacts/{RETAINED_ARTIFACT}"
        served = client.get(path, headers=owner)
        assert served.status_code == 200
        assert served.headers["content-type"] == "video/mp4"
        assert served.content == FAKE_MP4
        assert client.get(path, headers={"X-Dev-User": "intruder"}).status_code == 404


def test_retiring_the_model_hides_the_tool_but_not_the_clip_it_made(tmp_path):
    app = _app(tmp_path, True)
    gateway = MediaGateway("video")
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        made = client.post(
            "/api/chat",
            json={"sessionId": session["id"], "content": f"/{TOOL} a red bird", "stream": False},
        )
        assert made.status_code == 200, made.text
        clip = made.json()["message"]["attachments"][0]
        assert gateway.paid_calls == ["video"]
        assert _tool_item(client)["available"] is True

        # The retirement: same running deployment and flag, model runtime-disabled.
        entry = app.state.catalog.get("sora-2")
        assert entry is not None
        entry.runtimeEnabled = False

        served = client.get(f"/api/videos/artifacts/{clip['id']}")
        assert served.status_code == 200
        assert served.content == FAKE_MP4
        history = client.get(f"/api/sessions/{session['id']}/messages").json()
        assert any(
            attachment["id"] == clip["id"] and attachment["kind"] == "video"
            for message in history for attachment in message.get("attachments") or []
        )
        assert _tool_item(client)["available"] is False
        again = client.post(
            "/api/chat",
            json={"sessionId": session["id"], "content": f"/{TOOL} a blue bird", "stream": False},
        )
        assert again.status_code == 200, again.text
        assert again.json()["message"]["content"] == UNAVAILABLE_REPLY
        assert gateway.paid_calls == ["video"]
