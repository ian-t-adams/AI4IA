from __future__ import annotations

from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.sessions.models import Message


TURN_ID = "123e4567-e89b-42d3-a456-426614174000"
OTHER_TURN_ID = "123e4567-e89b-42d3-b456-426614174001"


def _session(client, *, user: str = "dev-user") -> str:
    response = client.post(
        "/api/sessions",
        json={"title": "Chat", "model": "gpt-5.2"},
        headers={"X-Dev-User": user},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _chat(
    client,
    session_id: str,
    *,
    content: str = "hello",
    headers: dict[str, str] | None = None,
    **changes,
):
    payload = {
        "sessionId": session_id,
        "content": content,
        "stream": False,
        "clientTurnId": TURN_ID,
        **changes,
    }
    return client.post("/api/chat", json=payload, headers=headers)


def test_client_turn_id_is_strictly_validated(client):
    session_id = _session(client)
    for invalid in ("short", TURN_ID.upper(), TURN_ID[:-1] + "g"):
        response = _chat(client, session_id, clientTurnId=invalid)
        assert response.status_code == 422
    assert client.get(f"/api/sessions/{session_id}/messages").json() == []


def test_retry_is_idempotent_and_both_rows_carry_turn_id(client):
    session_id = _session(client)
    first = _chat(client, session_id)
    second = _chat(client, session_id)

    assert first.status_code == second.status_code == 200
    assert first.json()["message"]["id"] == second.json()["message"]["id"]
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert len(messages) == 2
    assert {message["role"] for message in messages} == {"user", "assistant"}
    assert {message["clientTurnId"] for message in messages} == {TURN_ID}
    assert all("clientRequestFingerprint" not in message for message in messages)


def test_incompatible_turn_id_reuse_fails_without_duplicate(client):
    session_id = _session(client)
    assert _chat(client, session_id, content="first").status_code == 200
    conflict = _chat(client, session_id, content="different")
    assert conflict.status_code == 409
    assert "different chat request" in conflict.json()["detail"]
    assert len(client.get(f"/api/sessions/{session_id}/messages").json()) == 2


def test_turn_id_is_scoped_to_owned_target_session(client):
    owned = _session(client)
    foreign = _chat(
        client,
        owned,
        headers={"X-Dev-User": "mallory"},
    )
    assert foreign.status_code == 404

    other = _session(client)
    assert _chat(client, owned).status_code == 200
    assert _chat(client, other).status_code == 200
    owned_ids = {
        message["id"]
        for message in client.get(f"/api/sessions/{owned}/messages").json()
    }
    other_ids = {
        message["id"]
        for message in client.get(f"/api/sessions/{other}/messages").json()
    }
    assert owned_ids.isdisjoint(other_ids)


def test_stream_retry_replays_terminal_turn_without_duplicate(client):
    session_id = _session(client)
    first = _chat(client, session_id, stream=True)
    retry = _chat(client, session_id, stream=True)
    assert first.status_code == retry.status_code == 200
    assert f'"clientTurnId": "{TURN_ID}"' in retry.text
    assert "hello world" in retry.text
    assert len(client.get(f"/api/sessions/{session_id}/messages").json()) == 2


def test_local_reply_and_direct_tool_rows_carry_turn_id(client):
    local_session = _session(client)
    local = _chat(
        client,
        local_session,
        content="/process_document summarize this",
        stream=True,
    )
    assert local.status_code == 200
    assert f'"clientTurnId": "{TURN_ID}"' in local.text
    local_messages = client.get(
        f"/api/sessions/{local_session}/messages"
    ).json()
    assert [message["clientTurnId"] for message in local_messages] == [
        TURN_ID,
        TURN_ID,
    ]

    tool_session = _session(client)
    tool = _chat(
        client,
        tool_session,
        content="/calculator 2 + 2",
        clientTurnId=OTHER_TURN_ID,
    )
    assert tool.status_code == 200
    tool_messages = client.get(f"/api/sessions/{tool_session}/messages").json()
    assert [message["clientTurnId"] for message in tool_messages] == [
        OTHER_TURN_ID,
        OTHER_TURN_ID,
    ]


def test_agent_tool_loop_rows_carry_turn_id(client):
    session_id = _session(client)
    response = _chat(
        client,
        session_id,
        content="/generate_image a red bicycle",
    )
    assert response.status_code == 200
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["clientTurnId"] for message in messages] == [TURN_ID, TURN_ID]


def test_accepted_gateway_error_keeps_correlated_terminal_rows(client):
    class FailingGateway:
        async def complete(self, **_kwargs):
            raise ModelGatewayError(503, "temporarily unavailable")

    client.app.state.gateway = FailingGateway()
    session_id = _session(client)
    response = _chat(client, session_id)
    assert response.status_code == 502
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [message["clientTurnId"] for message in messages] == [TURN_ID, TURN_ID]
    assert messages[-1]["status"] == "error"


def test_historical_message_without_turn_id_still_validates():
    message = Message.model_validate(
        {
            "id": "old",
            "sessionId": "session",
            "userId": "user",
            "role": "assistant",
            "content": "legacy",
            "status": "complete",
        }
    )
    assert message.clientTurnId is None
