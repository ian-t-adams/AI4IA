def _create_session(client, model="gpt-5.2"):
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_models_endpoint_requires_no_extra_setup(client):
    resp = client.get("/api/models")
    assert resp.status_code == 200
    assert any(m["id"] == "gpt-5.2" for m in resp.json()["models"])


def test_session_lifecycle(client):
    session = _create_session(client)
    sid = session["id"]

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    patched = client.patch(f"/api/sessions/{sid}", json={"title": "Renamed"})
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"

    deleted = client.delete(f"/api/sessions/{sid}")
    assert deleted.status_code == 204
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_chat_non_streaming_persists_messages(client):
    session = _create_session(client)
    sid = session["id"]

    resp = client.post(
        "/api/chat", json={"sessionId": sid, "content": "hi", "stream": False}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"]["content"] == "hello world"

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "complete"


def test_chat_streaming_emits_sse_and_persists(client):
    session = _create_session(client)
    sid = session["id"]

    resp = client.post("/api/chat", json={"sessionId": sid, "content": "hi", "stream": True})
    assert resp.status_code == 200
    assert "[DONE]" in resp.text

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assistant = messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["status"] == "complete"
    assert "hello" in assistant["content"]


def test_chat_unknown_model_is_rejected(client):
    session = _create_session(client, model="no-such-model")
    resp = client.post(
        "/api/chat", json={"sessionId": session["id"], "content": "hi", "stream": False}
    )
    assert resp.status_code == 400


def test_cross_user_session_is_not_visible(client):
    session = _create_session(client)
    sid = session["id"]
    # A different dev user (via X-Dev-User) must not see another user's session.
    resp = client.get(f"/api/sessions/{sid}", headers={"X-Dev-User": "mallory"})
    assert resp.status_code == 404
