from __future__ import annotations

from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from ai4ia_api.memory.models import MemoryRecord
from ai4ia_api.routers.realtime import inject_session_tools, reject_client_system_message
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
        assert "secret_refs" not in calculator
        assert "egress_allowlist" not in calculator


def test_memory_list_and_delete_are_user_scoped():
    app = create_app(make_settings())
    with TestClient(app) as client:
        client.app.state.memory = EnumerableMemory()
        alice = {"X-Dev-User": "alice"}
        bob = {"X-Dev-User": "bob"}
        assert client.get("/api/memories", headers=alice).json()["items"][0]["id"] == "owned"
        assert client.get("/api/memories", headers=bob).json()["items"] == []
        assert client.delete("/api/memories/owned", headers=bob).status_code == 404
        assert client.delete("/api/memories/owned", headers=alice).status_code == 204


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
