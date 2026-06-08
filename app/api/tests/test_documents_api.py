"""Document upload/list/delete API + chat-context injection (Phase 7C).

Covers the CRUD contract (multipart upload, summary shape, list, delete),
guard rails (unsupported type, empty file, per-session count cap, wrong
session, disabled-account block), and that an uploaded document's text is
injected into the final USER turn while the stored user message stays clean.
A store failure during injection must never break the chat.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.main import create_app
from tests.conftest import make_settings


class CapturingGateway:
    def __init__(self, reply: str = "Acknowledged.") -> None:
        self.reply = reply
        self.last_messages: list[dict] | None = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None):
        self.last_messages = list(messages)
        return {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None):
        self.last_messages = list(messages)
        yield ChatChunk(
            delta=self.reply,
            raw=json.dumps({"choices": [{"delta": {"content": self.reply}}]}),
        )
        yield ChatChunk(done=True, raw="[DONE]")


def _make_client(**overrides) -> TestClient:
    app = create_app(make_settings(admin_subjects="alice", **overrides))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = CapturingGateway()
    return c


@pytest.fixture
def client():
    c = _make_client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _new_session(client: TestClient, headers=None) -> str:
    resp = client.post(
        "/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _upload(client, sid, *, name="notes.txt", data=b"Hello document", ctype="text/plain", headers=None):
    return client.post(
        f"/api/sessions/{sid}/documents",
        files={"file": (name, data, ctype)},
        headers=headers,
    )


def test_upload_returns_summary_without_full_text(client):
    sid = _new_session(client)
    resp = _upload(client, sid, data=b"The quick brown fox jumps over the lazy dog.")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "notes.txt"
    assert body["charCount"] > 0
    assert body["truncated"] is False
    assert "quick brown fox" in body["preview"]
    assert "text" not in body  # summaries never leak the full extracted text


def test_list_and_delete_document(client):
    sid = _new_session(client)
    doc_id = _upload(client, sid).json()["id"]

    listed = client.get(f"/api/sessions/{sid}/documents")
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()] == [doc_id]

    deleted = client.delete(f"/api/sessions/{sid}/documents/{doc_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/sessions/{sid}/documents").json() == []


def test_unsupported_type_rejected(client):
    sid = _new_session(client)
    resp = _upload(client, sid, name="blob.bin", data=b"\x01\x02\x03\x04binary", ctype="application/octet-stream")
    assert resp.status_code == 415


def test_empty_file_rejected(client):
    sid = _new_session(client)
    resp = _upload(client, sid, data=b"")
    assert resp.status_code == 422


def test_count_cap_enforced(client):
    sid = _new_session(client)
    for i in range(8):
        assert _upload(client, sid, name=f"f{i}.txt", data=b"content here").status_code == 201
    ninth = _upload(client, sid, name="f8.txt", data=b"content here")
    assert ninth.status_code == 409


def test_upload_to_unknown_session_404(client):
    resp = _upload(client, "does-not-exist")
    assert resp.status_code == 404


def test_disabled_user_cannot_upload(client):
    admin = {"X-Dev-User": "alice"}
    carol = {"X-Dev-User": "carol"}
    sid = _new_session(client, headers=carol)
    uid = client.get("/api/entitlement", headers=carol).json()["userId"]

    assert client.put(
        f"/api/admin/entitlements/{uid}", json={"disabled": True}, headers=admin
    ).status_code == 200

    resp = _upload(client, sid, headers=carol)
    assert resp.status_code == 403


def test_document_text_injected_as_system_context(client):
    gw = client.app.state.gateway
    sid = _new_session(client)
    _upload(client, sid, name="brief.txt", data=b"Project Zephyr ships in March.")

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "When does the project ship?", "stream": False},
    )
    assert resp.status_code == 200, resp.text

    # Documents are injected as a SYSTEM block (not the user turn) so the
    # anti-injection framing never trips a provider jailbreak/prompt shield.
    system_turns = [m for m in gw.last_messages if m["role"] == "system"]
    doc_system = "\n".join(m["content"] for m in system_turns)
    assert "BEGIN DOCUMENT" in doc_system
    assert "Project Zephyr ships in March." in doc_system
    # The untrusted framing (randomized fence) must be present.
    assert "randomized per message" in doc_system

    # The user turn carries only the user's own words — no document dump.
    user_turns = [m for m in gw.last_messages if m["role"] == "user"]
    final_user = user_turns[-1]["content"]
    assert final_user == "When does the project ship?"
    assert "BEGIN DOCUMENT" not in final_user

    # The STORED user message must stay clean (no document dump).
    stored = client.get(f"/api/sessions/{sid}/messages").json()
    stored_user = [m for m in stored if m["role"] == "user"][-1]
    assert stored_user["content"] == "When does the project ship?"
    assert "BEGIN DOCUMENT" not in stored_user["content"]


def test_chat_survives_document_store_failure(client):
    gw = client.app.state.gateway
    sid = _new_session(client)
    _upload(client, sid, data=b"some reference text")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("documents container unavailable")

    client.app.state.session_repo.list_documents = _boom

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "Hello there", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    final_user = [m for m in gw.last_messages if m["role"] == "user"][-1]["content"]
    assert final_user == "Hello there"  # no doc block injected on failure
