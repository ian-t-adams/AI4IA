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


def test_append_voice_turns_is_idempotent_per_conversation(client):
    sid = _create_session(client)["id"]
    payload = {
        "conversationId": "voice-cycle-123",
        "turns": [
            {"role": "user", "text": "hello", "createdAt": "2026-07-15T12:00:00Z"},
            {"role": "assistant", "text": "hi", "createdAt": "2026-07-15T12:00:01Z"},
        ],
    }

    first = client.post(f"/api/sessions/{sid}/voice-turns", json=payload)
    second = client.post(f"/api/sessions/{sid}/voice-turns", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert [message["id"] for message in second.json()] == [
        message["id"] for message in first.json()
    ]
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [message["content"] for message in messages] == ["hello", "hi"]
    assert messages[0]["createdAt"] == "2026-07-15T12:00:00Z"


def test_append_voice_turns_rejects_invalid_conversation_id(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={
            "conversationId": "not valid!",
            "turns": [{"role": "user", "text": "hello"}],
        },
    )

    assert resp.status_code == 422


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


def test_typed_and_voice_turns_are_listed_chronologically(client):
    sid = _create_session(client)["id"]
    typed = client.post(
        "/api/chat", json={"sessionId": sid, "content": "typed later", "stream": False}
    )
    assert typed.status_code == 200, typed.text

    voice = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={
            "conversationId": "earlier-cycle",
            "turns": [
                {
                    "role": "user",
                    "text": "spoken earlier",
                    "createdAt": "2000-01-01T00:00:00Z",
                }
            ],
        },
    )
    assert voice.status_code == 201, voice.text

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert messages[0]["content"] == "spoken earlier"
    assert messages[1]["content"] == "typed later"


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


def test_append_voice_turns_preserves_session_configuration(client):
    session = _create_session(client, model="gpt-5.2")
    sid = session["id"]
    patched = client.patch(
        f"/api/sessions/{sid}",
        json={"model": "gpt-5.3", "systemPrompt": "Stay concise."},
    )
    assert patched.status_code == 200, patched.text

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={
            "conversationId": "preserve-config",
            "turns": [{"role": "user", "text": "hello"}],
        },
    )
    assert resp.status_code == 201, resp.text

    current = client.get(f"/api/sessions/{sid}").json()
    assert current["model"] == "gpt-5.3"
    assert current["systemPrompt"] == "Stay concise."


def test_append_voice_turns_rejects_other_users_session(client):
    sid = _create_session(client)["id"]

    resp = client.post(
        f"/api/sessions/{sid}/voice-turns",
        json={"turns": [{"role": "user", "text": "intrude"}]},
        headers={"X-Dev-User": "mallory"},
    )
    assert resp.status_code == 404
