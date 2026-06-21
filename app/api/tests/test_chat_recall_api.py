"""End-to-end ``/recall_memory`` behavior through POST /api/chat.

The slash command becomes an ephemeral single-tool agent (mirroring
``/generate_image``). It only lights up when memory is enabled; otherwise it
returns a friendly "not enabled here" reply, so the tool is inert by default.
"""
from __future__ import annotations

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.memory.models import MemoryRecord


class _CapturingGateway:
    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.last_messages = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": self.text}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        yield ChatChunk(delta=self.text, raw="{}")
        yield ChatChunk(done=True, raw="[DONE]")


class _FakeMemory:
    enabled = True

    def __init__(self) -> None:
        self.recalled_for = []

    async def recall(self, user_id, query):
        self.recalled_for.append((user_id, query))
        return [MemoryRecord(user_id=user_id, text="alice prefers dark mode")]

    def format_context(self, records):
        return None

    async def remember(self, user_id, session_id, text):
        return None


def _create_session(client, model="gpt-5.2"):
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_recall_slash_command_disabled_by_default(client):
    """Default memory is the no-op service, so /recall_memory is inert."""
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/recall_memory my favorites", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert "isn't enabled" in resp.json()["message"]["content"]


def test_recall_slash_command_routes_through_model_when_enabled(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    client.app.state.memory = _FakeMemory()
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/recall_memory my preferences", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    # The ephemeral recall agent persona was sent, instructing a recall_memory call.
    assert gw.last_messages is not None
    assert gw.last_messages[0]["role"] == "system"
    assert "recall_memory" in gw.last_messages[0]["content"]
    # The stripped request text (not the slash command) is what the model sees.
    assert {"role": "user", "content": "my preferences"} in gw.last_messages


def test_recall_slash_command_usage_when_empty(client):
    client.app.state.memory = _FakeMemory()
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/recall_memory", "stream": False}
    )
    assert resp.status_code == 200
    assert "Usage:" in resp.json()["message"]["content"]
