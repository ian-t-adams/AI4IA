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


def test_chat_rejects_capability_model(client):
    """A non-conversational model (image/tts/etc.) can't be used as a chat target."""
    sid = _create_session(client, model="gpt-5.2")["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "draw a cat", "model": "gpt-image-1.5",
              "stream": False},
    )
    assert resp.status_code == 422, resp.text
    assert "image" in resp.json()["detail"].lower()
    # The refused turn left no dangling message and didn't rebind the session model.
    assert client.get(f"/api/sessions/{sid}/messages").json() == []
    assert client.get(f"/api/sessions/{sid}").json()["model"] == "gpt-5.2"



class _CapturingGateway:
    """Records the messages handed to the model so tests can assert on context."""

    def __init__(self) -> None:
        self.last_messages = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
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
    assert "Conversation instructions updated." not in contents
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


# --- Slash-command tools ------------------------------------------------------


def test_calculator_slash_command_runs_locally(client):
    """A direct tool runs in the command path (no model) and returns its result."""
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/calculator (2 + 3) * 4", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert "20" in resp.json()["message"]["content"]
    # A direct tool never invokes the model.
    assert gw.last_messages is None

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "20" in messages[-1]["content"]


def test_calculator_slash_command_usage_when_empty(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/calculator", "stream": False}
    )
    assert resp.status_code == 200
    assert "Usage:" in resp.json()["message"]["content"]


def test_calculator_slash_command_reports_bad_expression(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/calculator open(1)", "stream": False},
    )
    assert resp.status_code == 200
    # A handler error is surfaced as a friendly per-tool reply, not a 500.
    assert resp.json()["message"]["content"].startswith("/calculator:")


def test_get_current_time_slash_command_runs_locally(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/get_current_time", "stream": False},
    )
    assert resp.status_code == 200
    assert "Current time (UTC):" in resp.json()["message"]["content"]


def test_generate_image_slash_command_routes_through_model(client):
    """A capability tool becomes an ephemeral single-tool agent run via the model."""
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/generate_image a red bicycle", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    # The model WAS invoked, and its system prompt instructs it to call the tool.
    assert gw.last_messages is not None
    assert gw.last_messages[0]["role"] == "system"
    assert "generate_image" in gw.last_messages[0]["content"]
    # The stripped request text (not the slash command) is what the model sees.
    assert {"role": "user", "content": "a red bicycle"} in gw.last_messages


def test_generate_image_slash_command_usage_when_empty(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/generate_image", "stream": False}
    )
    assert resp.status_code == 200
    assert "Usage:" in resp.json()["message"]["content"]


def test_process_document_slash_command_not_enabled(client):
    """A capability tool whose services are absent gives a friendly local reply."""
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/process_document summarize it", "stream": False},
    )
    assert resp.status_code == 200
    assert "isn't enabled" in resp.json()["message"]["content"]


def test_unknown_slash_command_still_unknown(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/bogus do a thing", "stream": False}
    )
    assert resp.status_code == 200
    assert "Unknown command" in resp.json()["message"]["content"]
