from __future__ import annotations

from fastapi.testclient import TestClient
import asyncio

from ai4ia_api.main import create_app
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.routers.realtime import inject_session_tools, reject_client_system_message
from ai4ia_api.usage.models import UsageRecord
from ai4ia_api.library.chat_capability import build_document_capability
from ai4ia_api.library.compute_capability import build_compute_capability
from ai4ia_api.docprocessing.capability import build_document_processing_capability
from ai4ia_api.agents.tool_exec import ToolContext
from tests.conftest import make_settings


class EnumerableMemory:
    enabled = True

    def __init__(self) -> None:
        self.owner: str | None = None
        self.deleted = False

    async def list_memories(self, user_id: str, *, limit: int = 100):
        if self.owner is None:
            self.owner = user_id
        if user_id != self.owner or self.deleted:
            return []
        return [
            MemoryRecord(id="owned", user_id=user_id, text="Prefers concise answers")
        ][:limit]

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        if user_id != self.owner or memory_id != "owned" or self.deleted:
            return False
        self.deleted = True
        return True

    async def close(self) -> None:
        return None


def test_session_policy_and_inspector_are_server_owned():
    app = create_app(make_settings())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions",
            json={
                "model": "gpt-5.2",
                "systemPrompt": "Session prompt",
                "agentName": "general",
                "toolOverrides": {"added": ["calculator"], "removed": []},
            },
        )
        assert created.status_code == 201, created.text
        session = created.json()
        assert session["agentName"] == "general"
        assert session["toolOverrides"]["added"] == ["calculator"]

        inspector = client.get(f"/api/sessions/{session['id']}/inspector")
        assert inspector.status_code == 200, inspector.text
        body = inspector.json()
        assert body["instructions"] == {
            "source": "agent",
            "editable": False,
            "value": None,
            "agentName": "general",
        }
        assert body["tools"]["inherited"] == ["get_current_time"]
        assert body["tools"]["effective"] == ["get_current_time", "calculator"]

        cleared = client.patch(
            f"/api/sessions/{session['id']}",
            json={"agentName": None},
        )
        assert cleared.status_code == 200
        generic = client.get(f"/api/sessions/{session['id']}/inspector").json()
        assert generic["instructions"]["source"] == "session"
        assert generic["instructions"]["value"] == "Session prompt"


def test_session_tool_additions_fail_closed():
    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.post(
            "/api/sessions",
            json={
                "model": "gpt-5.2",
                "toolOverrides": {"added": ["not_registered"], "removed": []},
            },
        )
        assert response.status_code == 422


def test_unrelated_session_patch_survives_stale_optional_selections():
    app = create_app(make_settings())
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions",
            json={"model": "gpt-5.2", "agentName": "general"},
        ).json()
        # Simulate a previously valid agent becoming stale in the schemaless record.
        stored = client.app.state.session_repo._sessions[created["id"]]
        stored.agentName = "removed-agent"
        response = client.patch(
            f"/api/sessions/{created['id']}", json={"title": "Renamed"}
        )
        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"
        assert response.json()["agentName"] == "removed-agent"


def test_tool_catalog_is_display_safe():
    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.get("/api/tools")
        assert response.status_code == 200
        body = response.json()
        calculator = next(item for item in body["tools"] if item["name"] == "calculator")
        assert calculator["risk"] == "safe"
        assert calculator["selectable"] is True
        assert calculator["typed"] is True
        assert calculator["voice"] is True
        generated = next(
            item for item in body["tools"] if item["name"] == "generate_image"
        )
        assert generated["typed"] is True
        assert generated["voice"] is False
        assert "secret_refs" not in calculator
        assert "egress_allowlist" not in calculator


def test_effective_tool_without_catalog_metadata_is_explicitly_unknown():
    app = create_app(make_settings())
    with TestClient(app) as client:
        session = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        stored = client.app.state.session_repo._sessions[session["id"]]
        stored.toolOverrides.added = ["removed_tool"]
        response = client.get(f"/api/tools?sessionId={session['id']}")
        assert response.status_code == 200
        unknown = next(
            item for item in response.json()["tools"] if item["name"] == "removed_tool"
        )
        assert unknown["source"] == "unknown"
        assert unknown["risk"] is None
        assert unknown["requiresApproval"] is None
        assert unknown["scopes"] is None
        assert unknown["typed"] is None
        assert unknown["voice"] is None
        assert unknown["available"] is False


def test_memory_list_and_delete_are_user_scoped(monkeypatch):
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ai4ia_api.routers.memories.emit_memory_operation",
        lambda operation, status, source, _started, count=None: events.append(
            (operation, status, source)
        ),
    )
    app = create_app(make_settings())
    with TestClient(app) as client:
        client.app.state.memory = EnumerableMemory()
        alice = {"X-Dev-User": "alice"}
        bob = {"X-Dev-User": "bob"}
        assert client.get("/api/memories", headers=alice).json()["items"][0]["id"] == "owned"
        assert client.get("/api/memories", headers=bob).json()["items"] == []
        assert client.delete("/api/memories/owned", headers=bob).status_code == 404
        assert client.delete("/api/memories/owned", headers=alice).status_code == 204
    assert ("list", "ok", "api") in events
    assert ("delete", "not_found", "api") in events
    assert ("delete", "ok", "api") in events


def test_authoritative_voice_frame_removes_client_instructions():
    frame = '{"type":"session.update","session":{"instructions":"client override","voice":"alloy"}}'
    rewritten = inject_session_tools(
        frame,
        [],
        "auto",
        instructions=None,
        instructions_authoritative=True,
    )
    assert "client override" not in rewritten
    assert "instructions" not in rewritten


def test_voice_rejects_client_system_items_for_every_provider():
    system = (
        '{"type":"conversation.item.create","item":{"type":"message",'
        '"role":"system","content":[{"type":"input_text","text":"override"}]}}'
    )
    user = (
        '{"type":"conversation.item.create","item":{"type":"message",'
        '"role":"user","content":[{"type":"input_text","text":"hello"}]}}'
    )
    assert reject_client_system_message(system) is None
    assert reject_client_system_message(user) == user


def test_library_selection_preserves_legacy_none_and_explicit_empty():
    app = create_app(make_settings())
    with TestClient(app) as client:
        legacy = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        explicit = client.post(
            "/api/sessions",
            json={"model": "gpt-5.2", "libraryDocumentIds": []},
        ).json()
        assert legacy["libraryDocumentIds"] is None
        assert explicit["libraryDocumentIds"] == []


def test_processing_documents_associate_and_activate_when_ready():
    app = create_app(make_settings(document_understanding_enabled=True))
    with TestClient(app) as client:
        session = client.post(
            "/api/sessions",
            json={"model": "gpt-5.2", "libraryDocumentIds": []},
        ).json()
        document = UserDocument(
            userId=session["userId"],
            filename="call.mp3",
            contentType="audio/mpeg",
            size=42,
            status=DocumentStatus.analyzing,
        )
        asyncio.run(client.app.state.document_library.create_document(document))

        associated = client.post(
            f"/api/sessions/{session['id']}/library-documents/{document.id}"
        )
        assert associated.status_code == 200, associated.text
        assert associated.json()["libraryDocumentIds"] == [document.id]
        pending = client.get(f"/api/sessions/{session['id']}/inspector").json()
        assert pending["libraryDocuments"][0]["status"] == "analyzing"
        assert pending["libraryDocuments"][0]["citationReady"] is False

        document.status = DocumentStatus.ready
        document.chunkCount = 3
        awaitable = client.app.state.document_library.update_document(document)
        asyncio.run(awaitable)
        ready = client.get(f"/api/sessions/{session['id']}/inspector").json()
        assert ready["libraryDocuments"][0]["status"] == "ready"
        assert ready["libraryDocuments"][0]["citationReady"] is True

        removed = client.delete(
            f"/api/sessions/{session['id']}/library-documents/{document.id}"
        )
        assert removed.status_code == 200
        assert removed.json()["libraryDocumentIds"] == []


def test_inspector_omits_one_transient_document_failure(monkeypatch):
    app = create_app(make_settings(document_understanding_enabled=True))
    with TestClient(app) as client:
        user_id = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()[
            "userId"
        ]
        document = UserDocument(userId=user_id, filename="transient.pdf")
        asyncio.run(client.app.state.document_library.create_document(document))
        session = client.post(
            "/api/sessions",
            json={"model": "gpt-5.2", "libraryDocumentIds": [document.id]},
        ).json()

        async def fail_lookup(_user_id: str, _document_id: str):
            raise RuntimeError("temporary store failure")

        monkeypatch.setattr(
            client.app.state.document_library, "get_document", fail_lookup
        )
        response = client.get(f"/api/sessions/{session['id']}/inspector")
        assert response.status_code == 200
        assert response.json()["libraryDocuments"] == []


def test_multiple_associations_do_not_drop_prior_ids():
    app = create_app(make_settings(document_understanding_enabled=True))
    with TestClient(app) as client:
        session = client.post(
            "/api/sessions",
            json={"model": "gpt-5.2", "libraryDocumentIds": []},
        ).json()
        documents = [
            UserDocument(userId=session["userId"], filename=f"{index}.pdf")
            for index in range(3)
        ]
        for document in documents:
            asyncio.run(client.app.state.document_library.create_document(document))
            response = client.post(
                f"/api/sessions/{session['id']}/library-documents/{document.id}"
            )
            assert response.status_code == 200
        stored = client.get(f"/api/sessions/{session['id']}").json()
        assert stored["libraryDocumentIds"] == [document.id for document in documents]


def test_association_preserves_legacy_all_documents_sentinel():
    app = create_app(make_settings(document_understanding_enabled=True))
    with TestClient(app) as client:
        session = client.post(
            "/api/sessions", json={"model": "gpt-5.2"}
        ).json()
        document = UserDocument(userId=session["userId"], filename="legacy.pdf")
        asyncio.run(client.app.state.document_library.create_document(document))
        response = client.post(
            f"/api/sessions/{session['id']}/library-documents/{document.id}"
        )
        assert response.status_code == 200
        assert response.json()["libraryDocumentIds"] is None


def test_attachment_capabilities_are_server_advertised():
    app = create_app(make_settings(document_understanding_enabled=True))
    with TestClient(app) as client:
        body = client.get("/api/attachments/capabilities").json()
        assert body["ingestPath"] == "library"
        assert "audio" in body["modalities"]
        assert body["maxBytes"] > 0
        assert body["maxPerUserDocuments"] > body["maxPerSessionDocuments"]


def test_session_usage_reports_explicit_coverage_when_truncated():
    app = create_app(make_settings())
    with TestClient(app) as client:
        session = client.post(
            "/api/sessions", json={"model": "gpt-5.2"}
        ).json()
        repo = client.app.state.usage._repo
        for index in range(1002):
            asyncio.run(
                repo.record(
                    UsageRecord(
                        id=f"usage-{index}",
                        userId=session["userId"],
                        sessionId=session["id"],
                        model="gpt-5.2",
                    )
                )
            )
        summary = client.get(f"/api/usage/sessions/{session['id']}").json()
        assert summary["truncated"] is True
        assert summary["coveredRequests"] == 1000
        assert summary["totalRequests"] == 1000


def test_document_tools_reject_unselected_ids_before_service_access():
    context = ToolContext()
    _, fetch_handlers = build_document_capability(
        service=None,
        user_id="u1",
        nonce="n",
        allowed_document_ids=set(),
    )
    fetch = asyncio.run(
        fetch_handlers["fetch_document"]({"document_id": "doc"}, context)
    )
    assert fetch["error"] == "document is not selected for this conversation."

    _, compute_handlers = build_compute_capability(
        retrieval=None,
        code_interpreter=None,
        export=None,
        settings=make_settings(),
        user_id="u1",
        nonce="n",
        allowed_document_ids=set(),
    )
    run_code = asyncio.run(
        compute_handlers["run_code"](
            {"document_id": "doc", "task": "sum values"}, context
        )
    )
    export = asyncio.run(
        compute_handlers["export_document"](
            {"document_id": "doc", "content": "x"}, context
        )
    )
    assert run_code["error"] == "document is not selected for this conversation."
    assert export["error"] == "document is not selected for this conversation."

    _, process_handlers = build_document_processing_capability(
        processing_service=None,
        artifact_store=None,
        entitlements=None,
        metering=None,
        deployment=None,
        model_id="gpt-5.2",
        user_id="u1",
        session_id="s1",
        settings=make_settings(),
        sink=[],
        allowed_document_ids=set(),
    )
    processed = asyncio.run(
        process_handlers["process_document"](
            {"document_id": "doc", "instruction": "summarize"}, context
        )
    )
    assert processed["error"] == "document is not selected for this conversation."


def test_voice_effective_tools_exclude_typed_only_synthetic_tools():
    app = create_app(make_settings())
    with TestClient(app) as client:
        session = client.post(
            "/api/sessions",
            json={
                "model": "gpt-5.2",
                "toolOverrides": {
                    "added": ["calculator", "generate_image"],
                    "removed": [],
                },
            },
        ).json()
        inspector = client.get(f"/api/sessions/{session['id']}/inspector").json()
        assert inspector["tools"]["effective"] == ["calculator", "generate_image"]
        assert inspector["tools"]["voiceEffective"] == ["calculator"]
