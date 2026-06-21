"""Tests for the agents API: public catalog + user-defined agent CRUD."""
from __future__ import annotations

from ai4ia_api.gateway.client import ChatChunk


def test_agents_endpoint_returns_public_summaries(client):
    resp = client.get("/api/agents")
    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    names = {a["name"] for a in agents}
    assert {"general", "coder", "researcher"} <= names

    sample = agents[0]
    # Public shape only — internal persona/tool wiring must not be exposed.
    assert set(sample) == {"name", "displayName", "description", "enabled"}
    assert "systemPrompt" not in sample
    assert "tools" not in sample


# --- User-defined agents ------------------------------------------------------


class _CapturingGateway:
    def __init__(self) -> None:
        self.last_messages = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = messages
        yield ChatChunk(done=True, raw="[DONE]")


def _create(client, **over):
    body = {"name": "pirate", "systemPrompt": "Arr, talk like a pirate."}
    body.update(over)
    return client.post("/api/agents", json=body)


def test_create_lists_and_appears_in_public_catalog(client):
    resp = _create(client, displayName="Pirate", description="pirate talk")
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "pirate"
    assert created["userId"]

    mine = client.get("/api/agents/mine").json()["agents"]
    assert [a["name"] for a in mine] == ["pirate"]

    catalog = client.get("/api/agents").json()["agents"]
    assert "pirate" in {a["name"] for a in catalog}


def test_create_is_per_user_isolated(client):
    assert _create(client).status_code == 201
    # A different dev identity must not see the first user's agent.
    other = client.get("/api/agents", headers={"X-Dev-User": "someone-else"}).json()
    assert "pirate" not in {a["name"] for a in other["agents"]}
    other_mine = client.get(
        "/api/agents/mine", headers={"X-Dev-User": "someone-else"}
    ).json()
    assert other_mine["agents"] == []


def test_create_rejects_reserved_curated_name(client):
    resp = _create(client, name="coder")
    assert resp.status_code == 409, resp.text


def test_create_rejects_duplicate(client):
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409


def test_create_rejects_invalid_name(client):
    assert _create(client, name="Bad Name").status_code == 422


def test_create_rejects_unknown_model(client):
    assert _create(client, defaultModel="no-such-model").status_code == 422


def test_create_rejects_unattachable_tool(client):
    assert _create(client, tools=["danger"]).status_code == 422


def test_update_and_delete_roundtrip(client):
    assert _create(client).status_code == 201
    upd = client.put(
        "/api/agents/pirate", json={"systemPrompt": "Yarr, matey.", "enabled": False}
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["systemPrompt"] == "Yarr, matey."
    # Disabled agents drop out of the public @-menu but remain in /mine.
    assert "pirate" not in {a["name"] for a in client.get("/api/agents").json()["agents"]}
    assert "pirate" in {a["name"] for a in client.get("/api/agents/mine").json()["agents"]}

    assert client.delete("/api/agents/pirate").status_code == 204
    assert client.get("/api/agents/mine").json()["agents"] == []


def test_update_missing_returns_404(client):
    assert client.put("/api/agents/ghost", json={"systemPrompt": "x"}).status_code == 404


def test_delete_is_idempotent(client):
    assert client.delete("/api/agents/ghost").status_code == 204


def test_user_agent_is_mentionable_end_to_end(client):
    gw = _CapturingGateway()
    client.app.state.gateway = gw
    assert _create(client, systemPrompt="Arr, be a pirate.").status_code == 201

    sid = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}).json()[
        "id"
    ]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@pirate hello", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    # The user agent's persona prompt drove the turn, and the mention was stripped.
    assert gw.last_messages[0] == {"role": "system", "content": "Arr, be a pirate."}
    assert gw.last_messages[-1] == {"role": "user", "content": "hello"}


def test_user_agent_listed_by_agents_command(client):
    assert _create(client).status_code == 201
    sid = client.post("/api/sessions", json={"title": "Chat"}).json()["id"]
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "/agents", "stream": False}
    )
    assert resp.status_code == 200
    assert "@pirate" in resp.json()["message"]["content"]


def test_other_user_cannot_mention_my_agent(client):
    assert _create(client).status_code == 201
    other = {"X-Dev-User": "stranger"}
    sid = client.post("/api/sessions", json={"title": "Chat"}, headers=other).json()["id"]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@pirate hi", "stream": False},
        headers=other,
    )
    assert resp.status_code == 200
    assert "Unknown agent" in resp.json()["message"]["content"]


# --- Agent links / delegation validation -------------------------------------


def test_create_accepts_links_roundtrip(client):
    resp = _create(client, name="boss", links=["helper", "analyst"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["links"] == ["helper", "analyst"]
    # Links survive a round-trip through /mine.
    mine = {a["name"]: a for a in client.get("/api/agents/mine").json()["agents"]}
    assert mine["boss"]["links"] == ["helper", "analyst"]


def test_links_are_lowercased(client):
    resp = _create(client, name="boss", links=["Helper"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["links"] == ["helper"]


def test_create_allows_unknown_link_no_existence_check(client):
    # Existence is resolved at runtime (unknown target -> structured tool error),
    # so an unknown link name must not block the save.
    resp = _create(client, name="boss", links=["does-not-exist"])
    assert resp.status_code == 201, resp.text


def test_create_rejects_self_link(client):
    assert _create(client, name="boss", links=["boss"]).status_code == 422


def test_create_rejects_too_many_links(client):
    links = [f"a{i}" for i in range(6)]  # MAX_LINKS == 5
    assert _create(client, name="boss", links=links).status_code == 422


def test_create_rejects_invalid_link_name(client):
    assert _create(client, name="boss", links=["Bad Name"]).status_code == 422


def test_create_rejects_duplicate_links(client):
    assert _create(client, name="boss", links=["helper", "helper"]).status_code == 422
