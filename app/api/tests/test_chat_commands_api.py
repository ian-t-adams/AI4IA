"""End-to-end tests for slash commands routed through POST /api/chat."""
from __future__ import annotations


def _create_session(client, model="gpt-5.2"):
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_help_command_streams_without_hitting_model(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/help", "stream": True}
    )
    assert resp.status_code == 200
    assert "Available commands" in resp.text
    assert "[DONE]" in resp.text

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "Available commands" in messages[-1]["content"]


def test_help_command_non_streaming_returns_message(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/help", "stream": False}
    )
    assert resp.status_code == 200, resp.text
    assert "Available commands" in resp.json()["message"]["content"]


def test_clear_command_wipes_history(client):
    sid = _create_session(client)["id"]
    client.post("/api/chat", json={"sessionId": sid, "content": "hi", "stream": False})
    # Two messages now exist (user + assistant) from the real chat turn.
    assert len(client.get(f"/api/sessions/{sid}/messages").json()) == 2

    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/clear", "stream": False}
    )
    assert resp.status_code == 200
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["assistant"]
    assert messages[0]["content"] == "Conversation cleared."


def test_model_command_switches_session_model(client):
    sid = _create_session(client, model="gpt-5.2")["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/model gpt-5.1", "stream": False},
    )
    assert resp.status_code == 200
    assert "gpt-5.1" in resp.json()["message"]["content"]
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["model"] == "gpt-5.1"


class _CapturingGateway:
    """Records the messages handed to the model so tests can assert on context."""

    def __init__(self) -> None:
        self.last_messages = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        from ai4ia_api.gateway.client import ChatChunk

        self.last_messages = messages
        yield ChatChunk(done=True, raw="[DONE]")


def test_command_messages_excluded_from_model_context(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    sid = _create_session(client, model="gpt-5.2")["id"]

    client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/system Be very terse.", "stream": False},
    )
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "hello", "stream": False}
    )
    assert resp.status_code == 200

    contents = [m["content"] for m in gw.last_messages]
    # The command echo + its reply must never be replayed to the model...
    assert "/system Be very terse." not in contents
    assert "System prompt updated." not in contents
    # ...but the system prompt the command set must be applied.
    assert gw.last_messages[0] == {"role": "system", "content": "Be very terse."}
    assert {"role": "user", "content": "hello"} in gw.last_messages


def test_command_first_turn_does_not_block_auto_title(client):
    # Default title is "New chat" when omitted.
    created = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
    sid = created["id"]
    assert created["title"] == "New chat"

    client.post("/api/chat", json={"sessionId": sid, "content": "/help", "stream": False})
    client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "What is the capital of France?", "stream": False},
    )
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["title"] == "What is the capital of France?"
