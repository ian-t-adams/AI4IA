"""End-to-end tests for @agent routing through POST /api/chat."""
from __future__ import annotations

from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec, load_agent_catalog
from ai4ia_api.gateway.client import ChatChunk


def _create_session(client, model="gpt-5.2"):
    body = {"title": "Chat"}
    if model is not None:
        body["model"] = model
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


class _CapturingGateway:
    """Records the messages handed to the model, or stays None if never called."""

    def __init__(self) -> None:
        self.last_messages = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        self.last_messages = messages
        yield ChatChunk(done=True, raw="[DONE]")


def test_agent_mention_applies_persona_and_strips_mention(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@coder write a function", "stream": False},
    )
    assert resp.status_code == 200, resp.text

    coder_prompt = load_agent_catalog().get("coder").systemPrompt
    assert gw.last_messages[0] == {"role": "system", "content": coder_prompt}
    # The mention is stripped from what the model sees.
    assert gw.last_messages[-1] == {"role": "user", "content": "write a function"}


def test_agent_attribution_and_stripped_content_persisted(client):
    sid = _create_session(client)["id"]
    client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@coder hello", "stream": False},
    )
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    # Stored user content is the stripped text (mention is structural metadata).
    assert messages[0]["content"] == "hello"
    assert messages[0]["agent"] == "coder"
    assert messages[1]["agent"] == "coder"


def test_unknown_agent_returns_friendly_reply_without_calling_model(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@nobody hello there", "stream": False},
    )
    assert resp.status_code == 200
    assert "Unknown agent" in resp.json()["message"]["content"]
    # The model must never be reached for an invalid mention.
    assert gw.last_messages is None


def test_unknown_agent_with_command_does_not_execute_command(client):
    """@badagent /clear must be blocked by agent validation, not clear history."""
    sid = _create_session(client)["id"]
    client.post("/api/chat", json={"sessionId": sid, "content": "hi", "stream": False})
    assert len(client.get(f"/api/sessions/{sid}/messages").json()) == 2

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@badagent /clear", "stream": False},
    )
    assert resp.status_code == 200
    assert "Unknown agent" in resp.json()["message"]["content"]
    # The real chat turn is still present (plus the blocked attempt's echo+reply).
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert any(m["content"] == "hi" for m in messages)


def test_known_agent_with_command_runs_command(client):
    sid = _create_session(client, model="gpt-5.2")["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@coder /model gpt-5.1", "stream": False},
    )
    assert resp.status_code == 200
    assert "gpt-5.1" in resp.json()["message"]["content"]
    assert client.get(f"/api/sessions/{sid}").json()["model"] == "gpt-5.1"


def test_mention_without_message_prompts_for_input(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "@coder", "stream": False}
    )
    assert resp.status_code == 200
    assert "didn't include a message" in resp.json()["message"]["content"]
    assert gw.last_messages is None


def test_agents_slash_command_lists_mentionable_agents(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/agents", "stream": False}
    )
    assert resp.status_code == 200
    content = resp.json()["message"]["content"]
    assert "@coder" in content
    assert "@researcher" in content


def test_agent_default_model_is_not_persisted_to_session(client):
    # A session with no standing model lets the agent's default resolve the turn.
    sid = _create_session(client, model=None)["id"]
    client.app.state.agents = AgentCatalog(
        agents=[
            AgentSpec(
                name="speedy",
                displayName="Speedy",
                description="prefers a fast model",
                systemPrompt="Be fast.",
                defaultModel="gpt-5.1",
            )
        ]
    )

    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "@speedy hi", "stream": False}
    )
    assert resp.status_code == 200, resp.text
    # The per-turn agent default must NOT rebind the session's model.
    assert client.get(f"/api/sessions/{sid}").json()["model"] is None


def test_agent_streaming_records_attribution(client):
    sid = _create_session(client)["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@writer draft an intro", "stream": True},
    )
    assert resp.status_code == 200
    assert "[DONE]" in resp.text
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert messages[-1]["agent"] == "writer"
    assert messages[-1]["role"] == "assistant"
