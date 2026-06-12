"""Tests for POST /api/sessions/{id}/voice-turns.

This endpoint persists a finalized Voice Live exchange back into a session so
text chat and live voice share ONE transcript/context. Voice turns land as
ordinary user/assistant messages tagged ``source=voice``; they feed model
context on subsequent typed turns like any other message.
"""


def _create_session(client, model="gpt-5.2"):
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_append_voice_turns_persists_into_transcript(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={
            "turns": [
                {"role": "user", "text": "what's the weather"},
                {"role": "assistant", "text": "Sunny and warm."},
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert [m["role"] for m in created] == ["user", "assistant"]
    assert all(m["source"] == "voice" for m in created)

    # They show up in the shared transcript, in order, alongside any chat turns.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["content"] for m in messages] == ["what's the weather", "Sunny and warm."]
    assert messages[0]["createdAt"] < messages[1]["createdAt"]


def test_voice_turns_then_typed_turn_share_one_session(client):
    sid = _create_session(client)["id"]

    client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "user", "text": "remember my name is Ian"}]},
    )
    # A subsequent typed chat turn lands in the SAME session after the voice turn.
    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "hi", "stream": False}
    )
    assert resp.status_code == 200, resp.text

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert messages[0]["source"] == "voice"
    assert messages[0]["content"] == "remember my name is Ian"
    # The typed turn (default source "chat") follows the voice turn.
    assert messages[1]["source"] == "chat"
    assert messages[1]["content"] == "hi"


def test_append_voice_turns_drops_empty_and_whitespace(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={
            "turns": [
                {"role": "user", "text": "   \n\t  "},
                {"role": "assistant", "text": "kept"},
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert len(created) == 1
    assert created[0]["content"] == "kept"


def test_append_voice_turns_strips_control_characters(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "user", "text": "a\x00b\x07c"}]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()[0]["content"] == "abc"


def test_append_voice_turns_rejects_invalid_role(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "system", "text": "nope"}]},
    )
    assert resp.status_code == 422


def test_append_voice_turns_enforces_count_cap(client):
    sid = _create_session(client)["id"]

    turns = [{"role": "user", "text": f"t{i}"} for i in range(201)]
    resp = client.post(f"/api/sessions/{sid}/voice-turns", json={"turns": turns})
    assert resp.status_code == 422


def test_append_voice_turns_caps_text_length(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "assistant", "text": "x" * 9000}]},
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()[0]["content"]) == 8000


def test_append_voice_turns_empty_list_is_noop(client):
    sid = _create_session(client)["id"]

    resp = client.post(f"/api/sessions/{sid}/voice-turns", json={"turns": []})
    assert resp.status_code == 201, resp.text
    assert resp.json() == []
    assert client.get(f"/api/sessions/{sid}/messages").json() == []


def test_append_voice_turns_touches_session_updated_at(client):
    session = _create_session(client)
    sid = session["id"]
    before = session["updatedAt"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "user", "text": "hello"}]},
    )
    assert resp.status_code == 201, resp.text

    after = client.get(f"/api/sessions/{sid}").json()["updatedAt"]
    assert after >= before


def test_append_voice_turns_rejects_other_users_session(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "user", "text": "intrude"}]},
        headers={"X-Dev-User": "mallory"},
    )
    assert resp.status_code == 404
