"""Image editing is gated by ONE predicate at every seam, not just the flag.

Mirrors ``test_video_model_availability``: the same catalog fixture runs with only
the editing rows' ``runtimeEnabled`` flipped (and, separately, the flag), and each
seam -- tool catalog, conversation policy and inspector, consent and publication
snapshots, the ``/edit_image`` command, agent attachment and the handler itself --
must agree. The enabled control proves the seam really offers and runs the edit.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.consent_service import _chat_schemas, session_snapshot
from ai4ia_api.agents.tool_exec import (
    CHAT_ONLY_SYNTHETIC_TOOL_NAMES,
    SELECTABLE_SYNTHETIC_TOOL_NAMES,
    ToolContext,
)
from ai4ia_api.agents.synthetic_governance import synthetic_spec
from ai4ia_api.agents.tools import ToolRisk
from ai4ia_api.entitlements.models import EntitlementDecision
from ai4ia_api.images.availability import (
    NO_IMAGE_EDIT_MODEL_DETAIL,
    available_image_edit_model_ids,
    default_image_edit_model_id,
    image_editing_availability,
    state_image_edit_availability,
)
from ai4ia_api.images.edit_capability import build_image_edit_capability
from ai4ia_api.images.editing import ImageEditService
from ai4ia_api.main import create_app
from ai4ia_api.sessions.models import MessageAttachment
from tests.conftest import make_settings
from tests.image_edit_fixtures import (
    EDIT_TOOL,
    SOURCE_PNG,
    SUNBURST,
    FakeEditGateway,
    editing_model_ids,
    image_edit_catalog_path,
    png_bytes,
    seed_generated_image,
)

ARTIFACT = "d" * 32
UNAVAILABLE_REPLY = f"/{EDIT_TOOL} is unavailable. {NO_IMAGE_EDIT_MODEL_DETAIL}"


def _app(tmp_path, *, runtime_enabled: bool = True, editing: bool = True):
    disabled = () if runtime_enabled else tuple(editing_model_ids())
    return create_app(make_settings(
        image_generation_enabled=True, image_editing_enabled=editing,
        model_catalog_path=image_edit_catalog_path(tmp_path, disabled=disabled),
    ))


@pytest.fixture(params=[True, False], ids=["models-enabled", "models-disabled"])
def edit_app(request, tmp_path):
    app = _app(tmp_path, runtime_enabled=request.param)
    with TestClient(app) as client:
        client.app.state.gateway = FakeEditGateway()
        yield client, request.param


def _tool_item(client: TestClient, query: str = "") -> dict:
    tools = client.get(f"/api/tools{query}").json()["tools"]
    return next(item for item in tools if item["name"] == EDIT_TOOL)


def _session_with_tool(client: TestClient) -> tuple[str, str]:
    created = client.post(
        "/api/sessions", json={"model": "gpt-5.2", "toolOverrides": {"added": [EDIT_TOOL]}},
    )
    assert created.status_code == 201, created.text
    return created.json()["userId"], created.json()["id"]


def test_edit_image_is_governed_and_chat_only():
    assert EDIT_TOOL in SELECTABLE_SYNTHETIC_TOOL_NAMES
    assert EDIT_TOOL in CHAT_ONLY_SYNTHETIC_TOOL_NAMES
    spec = synthetic_spec(EDIT_TOOL)
    generate = synthetic_spec("generate_image")
    assert spec is not None and generate is not None
    # The same posture as generate_image: a fixed destination and caller-owned data.
    assert (spec.risk, spec.injection_only_risk) == (generate.risk, generate.injection_only_risk)
    assert spec.risk is ToolRisk.external


def test_predicate_needs_both_flags_the_store_and_a_routable_editing_model(tmp_path):
    app = _app(tmp_path)
    with TestClient(app):
        catalog, store = app.state.catalog, app.state.image_artifacts
        cases = {
            (True, True, True): "available",
            (False, True, True): "disabled",
            (True, False, True): "disabled",
            (True, True, False): "storage_unavailable",
        }
        for (editing, generation, has_store), expected in cases.items():
            assert image_editing_availability(
                editing_enabled=editing, generation_enabled=generation,
                artifact_store=store if has_store else None, catalog=catalog,
            ) == expected
        assert available_image_edit_model_ids(catalog) == editing_model_ids()
        assert default_image_edit_model_id(catalog) == SUNBURST
        for entry in catalog.models:
            if entry.imageEditing:
                entry.runtimeEnabled = False
        assert image_editing_availability(
            editing_enabled=True, generation_enabled=True, artifact_store=store, catalog=catalog,
        ) == "no_model"
        assert default_image_edit_model_id(catalog) is None


def test_shared_predicate_reports_the_runtime_models(edit_app):
    client, enabled = edit_app
    state = client.app.state
    expected = "available" if enabled else "no_model"
    assert state_image_edit_availability(state) == expected
    assert state_image_edit_availability(state, policy_filter=False) == expected


def test_tool_catalog_follows_the_runtime_models(edit_app):
    client, enabled = edit_app
    item = _tool_item(client)
    assert item["available"] is enabled
    assert item["selectable"] is True
    assert item["detail"] == (None if enabled else NO_IMAGE_EDIT_MODEL_DETAIL)


def test_conversation_policy_and_inspector_follow_the_runtime_models(edit_app):
    client, enabled = edit_app
    _, session_id = _session_with_tool(client)
    inspector = client.get(f"/api/sessions/{session_id}/inspector").json()
    assert EDIT_TOOL in inspector["tools"]["added"]
    assert (EDIT_TOOL in inspector["tools"]["effective"]) is enabled
    assert _tool_item(client, f"?sessionId={session_id}")["available"] is enabled


def test_consent_snapshot_follows_the_runtime_models(edit_app):
    client, enabled = edit_app
    state = client.app.state
    user_id, session_id = _session_with_tool(client)

    async def snapshot():
        session = await state.session_repo.get_session(user_id, session_id)
        return await session_snapshot(state, user_id=user_id, session=session)

    assert (EDIT_TOOL in asyncio.run(snapshot()).contracts) is enabled


def test_publication_metadata_follows_the_runtime_models(edit_app):
    client, enabled = edit_app
    state = client.app.state
    user_id, session_id = _session_with_tool(client)

    async def published():
        session = await state.session_repo.get_session(user_id, session_id)
        return await _chat_schemas(
            state, user_id=user_id, session=session, tool_names=[EDIT_TOOL], email=None,
            include_attachments=False, publication_metadata=True,
        )

    schemas = asyncio.run(published())
    assert [schema["function"]["name"] for schema in schemas] == ([EDIT_TOOL] if enabled else [])
    if enabled:
        description = schemas[0]["function"]["description"]
        assert f"Default model: {SUNBURST}." in description
        assert "FLUX" not in description and "MAI" not in description


def test_schema_is_stable_across_conversations_for_consent_digests(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        state = client.app.state
        first_user, first = _session_with_tool(client)
        second_user, second = _session_with_tool(client)

        async def schema(user_id, session_id):
            session = await state.session_repo.get_session(user_id, session_id)
            return await _chat_schemas(
                state, user_id=user_id, session=session, tool_names=[EDIT_TOOL], email=None,
                include_attachments=False,
            )

        asyncio.run(seed_generated_image(app, first_user, first, ARTIFACT))
        assert asyncio.run(schema(first_user, first)) == asyncio.run(schema(second_user, second))


@pytest.mark.parametrize("runtime_enabled", [True, False])
@pytest.mark.parametrize("invocation", ["command", "agent"])
def test_chat_offers_and_runs_the_edit_only_with_a_runtime_model(tmp_path, runtime_enabled, invocation):
    app = _app(tmp_path, runtime_enabled=runtime_enabled)
    gateway = FakeEditGateway()
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        asyncio.run(seed_generated_image(app, session["userId"], session["id"], ARTIFACT))
        if invocation == "agent":
            agent = client.post(
                "/api/agents",
                json={"name": "retoucher", "systemPrompt": "Edit images.", "tools": [EDIT_TOOL]},
            )
            assert agent.status_code == 201, agent.text
            content = "@retoucher make the sky purple"
        else:
            content = f"/{EDIT_TOOL} make the sky purple"
        response = client.post(
            "/api/chat", json={"sessionId": session["id"], "content": content, "stream": False},
        )
        assert response.status_code == 200, response.text
        message = response.json()["message"]
        assert (EDIT_TOOL in gateway.offered) is runtime_enabled
        assert len(gateway.edit_calls) == (1 if runtime_enabled else 0)
        if runtime_enabled:
            (attachment,) = message["attachments"]
            assert attachment["kind"] == "image"
            assert (attachment["sourceKind"], attachment["sourceId"]) == ("generated", ARTIFACT)
            assert attachment["model"] == SUNBURST
            steps = [step["label"] for step in message.get("steps") or []]
            assert "Edited an image" in steps
        elif invocation == "command":
            assert gateway.model_calls == 0
            assert message["content"] == UNAVAILABLE_REPLY
        else:
            assert gateway.model_calls == 1
            assert not message.get("attachments")


@pytest.mark.parametrize("editing", [True, False])
def test_the_flag_hides_the_command_and_the_tool(tmp_path, editing):
    app = _app(tmp_path, editing=editing)
    gateway = FakeEditGateway()
    with TestClient(app) as client:
        app.state.gateway = gateway
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        asyncio.run(seed_generated_image(app, session["userId"], session["id"], ARTIFACT))
        response = client.post("/api/chat", json={
            "sessionId": session["id"], "content": f"/{EDIT_TOOL} make it blue", "stream": False,
        })
        assert response.status_code == 200, response.text
        assert len(gateway.edit_calls) == (1 if editing else 0)
        if not editing:
            assert gateway.model_calls == 0
            assert "isn't enabled" in response.json()["message"]["content"]
        assert _tool_item(client)["available"] is editing


def test_command_usage_when_empty(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        app.state.gateway = FakeEditGateway()
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        response = client.post("/api/chat", json={
            "sessionId": session["id"], "content": f"/{EDIT_TOOL}", "stream": False,
        })
        assert response.json()["message"]["content"].startswith("Usage: /edit_image")
        assert app.state.gateway.model_calls == 0


def test_the_handler_defaults_to_this_turns_pending_image_and_is_budgeted(tmp_path):
    app = _app(tmp_path)
    gateway = FakeEditGateway()
    with TestClient(app) as client:
        state = app.state
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        user_id, session_id = session["userId"], session["id"]
        asyncio.run(seed_generated_image(app, user_id, session_id, ARTIFACT))
        pending_id = "e" * 32
        pending_bytes = png_bytes(12, 12, seed=0x22)
        asyncio.run(state.image_artifacts.put(user_id, pending_id, pending_bytes))
        # An image generated earlier in this same turn is newer than anything stored.
        sink = [MessageAttachment(id=pending_id, kind="image", status="complete")]
        _, handlers = build_image_edit_capability(
            edit_service=ImageEditService(settings=state.settings, catalog=state.catalog, gateway=gateway),
            artifact_store=state.image_artifacts, entitlements=state.entitlements,
            metering=state.usage, catalog=state.catalog, user_id=user_id,
            session_id=session_id, sink=sink, repo=state.session_repo,
        )
        edit = handlers[EDIT_TOOL]
        first = asyncio.run(edit({"prompt": "add a moon"}, ToolContext()))
        assert first["status"] == "edited", first
        assert first["source"] == {"kind": "generated", "id": pending_id}
        assert gateway.edit_calls[0]["image"] == pending_bytes
        second = asyncio.run(edit({"prompt": "warmer", "image_artifact_id": ARTIFACT}, ToolContext()))
        assert second["source"] == {"kind": "generated", "id": ARTIFACT}
        assert gateway.edit_calls[1]["image"] == SOURCE_PNG
        assert [a.kind for a in sink] == ["image", "image", "image"]
        assert sink[1].sourceId == pending_id and sink[2].sourceId == ARTIFACT
        refused = asyncio.run(edit({"prompt": "again"}, ToolContext()))
        assert "at most 2 images" in refused["error"]
        assert len(gateway.edit_calls) == 2


def test_the_handler_refuses_foreign_or_unknown_sources_before_any_provider_call(tmp_path):
    app = _app(tmp_path)
    gateway = FakeEditGateway()
    with TestClient(app) as client:
        state = app.state
        owner = client.post("/api/sessions", json={"model": "gpt-5.2"},
                            headers={"X-Dev-User": "owner"}).json()
        other = client.post("/api/sessions", json={"model": "gpt-5.2"},
                            headers={"X-Dev-User": "owner"}).json()
        asyncio.run(seed_generated_image(app, owner["userId"], other["id"], ARTIFACT))

        def handler(user_id, session_id):
            _, handlers = build_image_edit_capability(
                edit_service=ImageEditService(
                    settings=state.settings, catalog=state.catalog, gateway=gateway,
                ),
                artifact_store=state.image_artifacts, entitlements=state.entitlements,
                metering=state.usage, catalog=state.catalog, user_id=user_id,
                session_id=session_id, sink=[], repo=state.session_repo,
            )
            return handlers[EDIT_TOOL]

        cases = [
            ({"prompt": "x"}, "no image in this conversation"),
            ({"prompt": "x", "image_artifact_id": ARTIFACT}, "not part of this conversation"),
            ({"prompt": "x", "library_document_id": "doc-1"}, "library is not available"),
            ({"prompt": "x", "image_artifact_id": ARTIFACT, "library_document_id": "d"},
             "at most one"),
            ({"prompt": ""}, "non-empty"),
        ]
        edit = handler(owner["userId"], owner["id"])
        for args, message in cases:
            result = asyncio.run(edit(args, ToolContext()))
            assert message in result["error"], (args, result)
        # Another user naming the owner's conversation cannot even load it.
        intruder = handler("intruder-id", other["id"])
        assert "no longer available" in asyncio.run(
            intruder({"prompt": "x", "image_artifact_id": ARTIFACT}, ToolContext()),
        )["error"]
        assert gateway.edit_calls == []
        # Paired control: the owner's own conversation holding the image succeeds.
        ok = asyncio.run(handler(owner["userId"], other["id"])(
            {"prompt": "x", "image_artifact_id": ARTIFACT}, ToolContext(),
        ))
        assert ok["status"] == "edited" and len(gateway.edit_calls) == 1


@pytest.mark.parametrize("disable_at", [None, "flag_before_handler", "model_before_handler",
                                        "model_during_entitlement"])
def test_a_stale_offer_rechecks_before_any_provider_call(tmp_path, disable_at):
    app = _app(tmp_path)
    gateway = FakeEditGateway()
    with TestClient(app) as client:
        state = app.state
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        asyncio.run(seed_generated_image(app, session["userId"], session["id"], ARTIFACT))
        catalog = state.catalog

        def disable_models():
            for entry in catalog.models:
                if entry.imageEditing:
                    entry.runtimeEnabled = False

        checks: list[str] = []

        class Entitlements:
            async def check(self, user_id):
                checks.append(user_id)
                if disable_at == "model_during_entitlement":
                    disable_models()
                return EntitlementDecision.allow()

        args = {
            "edit_service": ImageEditService(
                settings=state.settings, catalog=catalog, gateway=gateway,
            ),
            "artifact_store": state.image_artifacts, "entitlements": Entitlements(),
            "metering": state.usage, "catalog": catalog, "user_id": session["userId"],
            "session_id": session["id"], "sink": [], "repo": state.session_repo,
        }
        tools, handlers = build_image_edit_capability(**args)
        assert [tool["function"]["name"] for tool in tools] == [EDIT_TOOL]
        if disable_at == "flag_before_handler":
            state.settings.image_editing_enabled = False
        elif disable_at == "model_before_handler":
            disable_models()
        result = asyncio.run(handlers[EDIT_TOOL]({"prompt": "make it blue"}, ToolContext()))
        if disable_at is None:
            assert result["status"] == "edited"
            assert len(gateway.edit_calls) == 1
            assert checks == [session["userId"]]
            return
        assert gateway.edit_calls == []
        if disable_at == "flag_before_handler":
            # The handler's own re-check refuses before entitlement or source IO.
            assert result == {"error": "Image editing is disabled."}
            assert checks == []
        elif disable_at == "model_before_handler":
            assert result == {"error": NO_IMAGE_EDIT_MODEL_DETAIL}
            assert checks == []
        else:
            # Gone during the entitlement await: the service's resolution is the backstop.
            assert result == {"error": "No image editing models are available."}
            assert checks == [session["userId"]]
        assert build_image_edit_capability(**args) == ([], {})


def test_media_settings_default_off_and_validate_runtime_requires_generation(monkeypatch):
    monkeypatch.delenv("AI4IA_IMAGE_EDITING_ENABLED", raising=False)
    assert make_settings().image_editing_enabled is False
    assert make_settings().gateway_image_edit_api_version == "2025-04-01-preview"
    monkeypatch.setenv("AI4IA_IMAGE_EDITING_ENABLED", "true")
    assert make_settings().image_editing_enabled is True
    for env in ("local", "prod"):
        extra = {} if env == "local" else {
            "model_gateway_url": "https://proxy.test/openai",
            "model_gateway_allowed_hosts": "proxy.test",
            "model_gateway_auth_mode": "api_key",
            "model_gateway_api_key": "test-proxy-key",
            "model_gateway_api_key_header": "S7P-KEY",
            "image_blob_account_url": "https://media.blob.core.windows.net",
        }
        refused = make_settings(env=env, image_editing_enabled=True, **extra)
        with pytest.raises(RuntimeError, match="requires AI4IA_IMAGE_GENERATION_ENABLED"):
            refused.validate_runtime()
        make_settings(
            env=env, image_editing_enabled=True, image_generation_enabled=True, **extra,
        ).validate_runtime()
