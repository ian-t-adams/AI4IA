"""Chat-context injection for the document library.

End-to-end through the chat endpoint: when document understanding is enabled, a
*ready* library document is injected as a nonce-fenced SYSTEM block (Tier 1
summary card) while the stored user turn stays clean; when the feature is OFF
(default) no library block is ever injected (zero regression).
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.main import create_app
from tests.conftest import make_settings


class CapturingGateway:
    def __init__(self, reply: str = "Acknowledged.") -> None:
        self.reply = reply
        self.last_messages: list[dict] | None = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = list(messages)
        return {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.last_messages = list(messages)
        yield ChatChunk(
            delta=self.reply,
            raw=json.dumps({"choices": [{"delta": {"content": self.reply}}]}),
        )
        yield ChatChunk(done=True, raw="[DONE]")


def _make_client(**overrides) -> TestClient:
    app = create_app(make_settings(**overrides))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = CapturingGateway()
    return c


def _uid(client: TestClient) -> str:
    return client.get("/api/entitlement").json()["userId"]


def _new_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_ready_doc(client: TestClient, user_id: str) -> UserDocument:
    # The retrieval service shares the ingestor's in-memory stores, so seeding
    # through the ingestor's library/blob makes the document visible to chat.
    ingestor = client.app.state.document_ingestor
    doc = UserDocument(
        userId=user_id,
        filename="brief.md",
        status=DocumentStatus.ready,
        summary="Project Falcon status brief",
    )
    path = blob_path(user_id, doc.id, PARSED_NAME)
    await ingestor.blob.put(path, b"# Falcon\n\nAll systems nominal.", "text/markdown")
    doc.parsedPath = path
    await ingestor.library.create_document(doc)
    return doc


def _chat(client: TestClient, sid: str) -> dict:
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "What is the Falcon status?", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_library_block_injected_when_enabled():
    client = _make_client(document_understanding_enabled=True)
    try:
        uid = _uid(client)
        sid = _new_session(client)
        doc = await _seed_ready_doc(client, uid)
        _chat(client, sid)

        messages = client.app.state.gateway.last_messages
        systems = [m["content"] for m in messages if m["role"] == "system"]
        library_blocks = [s for s in systems if "BEGIN LIBRARY" in s]
        assert library_blocks, "expected a LIBRARY system block"
        assert "brief.md" in library_blocks[0]
        assert "Project Falcon status brief" in library_blocks[0]
        assert f"id={doc.id}" in library_blocks[0]

        # The stored user turn stays clean (no library text leaks into history).
        user_turns = [m["content"] for m in messages if m["role"] == "user"]
        assert user_turns[-1] == "What is the Falcon status?"
        assert "BEGIN LIBRARY" not in user_turns[-1]
    finally:
        client.__exit__(None, None, None)


def test_no_library_block_when_disabled():
    client = _make_client()  # document_understanding_enabled defaults False
    try:
        sid = _new_session(client)
        _chat(client, sid)
        messages = client.app.state.gateway.last_messages
        assert all("BEGIN LIBRARY" not in m["content"] for m in messages)
        assert client.app.state.document_retrieval is None
    finally:
        client.__exit__(None, None, None)
