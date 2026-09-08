"""Chat-context injection for the document library.

End-to-end through the chat endpoint: when document understanding is enabled, a
*ready* library document is injected as a nonce-fenced SYSTEM block (Tier 1
summary card) while the stored user turn stays clean; when the feature is OFF
(default) no library block is ever injected (zero regression).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.main import create_app
from tests.conftest import make_settings
from tests.test_ai_search_chunks import _FakeIndexClient
from tests.test_doc_retrieval import FakeEmbedder, UnavailableSearchClient


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


def _new_session(
    client: TestClient, library_document_ids: list[str] | None | object = ...
) -> str:
    body: dict = {"title": "Chat", "model": "gpt-5.2"}
    if library_document_ids is not ...:
        body["libraryDocumentIds"] = library_document_ids
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_ready_doc(
    client: TestClient, user_id: str, filename: str = "brief.md"
) -> UserDocument:
    # The retrieval service shares the ingestor's in-memory stores, so seeding
    # through the ingestor's library/blob makes the document visible to chat.
    ingestor = client.app.state.document_ingestor
    doc = UserDocument(
        userId=user_id,
        filename=filename,
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


async def test_explicit_empty_library_selection_injects_nothing():
    client = _make_client(document_understanding_enabled=True)
    try:
        uid = _uid(client)
        await _seed_ready_doc(client, uid)
        sid = _new_session(client, [])
        _chat(client, sid)
        messages = client.app.state.gateway.last_messages
        assert all("BEGIN LIBRARY" not in message["content"] for message in messages)
    finally:
        client.__exit__(None, None, None)


async def test_nonempty_library_selection_is_an_exact_allowlist():
    client = _make_client(document_understanding_enabled=True)
    try:
        uid = _uid(client)
        selected = await _seed_ready_doc(client, uid, "selected.md")
        await _seed_ready_doc(client, uid, "excluded.md")
        sid = _new_session(client, [selected.id])
        _chat(client, sid)
        library = next(
            message["content"]
            for message in client.app.state.gateway.last_messages
            if "BEGIN LIBRARY" in message["content"]
        )
        assert "selected.md" in library
        assert "excluded.md" not in library
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("agent_prefix", ["", "@general "])
@pytest.mark.parametrize("admit_library", [False, True])
async def test_search_outage_receipt_survives_chat_and_keeps_canonical_access(
    stream, agent_prefix, admit_library, caplog, monkeypatch,
):
    if not admit_library:
        monkeypatch.setattr(
            "ai4ia_api.routers.chat._prompt_byte_budget", lambda *args: 1200,
        )
    client = _make_client(
        document_understanding_enabled=True,
        search_endpoint="https://example.search.windows.net",
        memory_embedding_dimensions=3,
    )
    try:
        uid = _uid(client)
        sid = _new_session(client)
        doc = await _seed_ready_doc(client, uid)
        retrieval = client.app.state.document_retrieval
        chunks = client.app.state.document_ingestor.chunks
        search = UnavailableSearchClient()
        chunks._injected_search_client = search
        chunks._index_client = _FakeIndexClient()
        retrieval._embedder = FakeEmbedder()

        for unavailable in (True, False):
            search.unavailable = unavailable
            response = client.post(
                "/api/chat",
                json={
                    "sessionId": sid,
                    "content": agent_prefix + "What is the Falcon status?",
                    "stream": stream,
                },
            )
            assert response.status_code == 200, response.text
            if stream:
                assert "[DONE]" in response.text
            rows = client.get(f"/api/sessions/{sid}/messages").json()
            assistant = [row for row in rows if row["role"] == "assistant"][-1]
            assert assistant["content"] == "Acknowledged."
            receipt = assistant["executionReceipt"]
            assert receipt["status"] == "complete"
            if agent_prefix:
                assert "fetch_document" in {
                    tool["name"] for tool in receipt["toolsOffered"]
                }
            else:
                assert receipt["toolsOffered"] == []
            assert (
                "library_retrieval_unavailable" in receipt["notes"]
            ) is unavailable
            assert receipt["partial"] is unavailable
            context = next(block for block in receipt["contextBlocks"] if block["kind"] == "library")
            assert context["admitted"] is admit_library
            assert context["sources"] == []
            if admit_library:
                assert (
                    "Library retrieval is unavailable" in context["content"]["text"]
                ) is unavailable
            else:
                assert context["content"] is None
                assert "library" in receipt["droppedContextBlocks"]
            assert "PRIVATE QUERY AND SOURCE CONTENT" not in json.dumps(receipt)
            assert "PRIVATE QUERY AND SOURCE CONTENT" not in caplog.text
            assert "token=secret" not in caplog.text
            if admit_library:
                assert "brief.md" in next(
                    message["content"]
                    for message in client.app.state.gateway.last_messages
                    if "BEGIN LIBRARY" in message["content"]
                )
            else:
                assert all(
                    "BEGIN LIBRARY" not in message["content"]
                    for message in client.app.state.gateway.last_messages
                )

            # Auth, manifest/summary reads, parsed-source access and inspector
            # inventory do not become health probes or depend on the chunk index.
            calls = len(search.search_calls)
            assert _uid(client) == uid
            manifest = client.get(f"/api/library/documents/{doc.id}")
            assert manifest.status_code == 200
            assert manifest.json()["status"] == "ready"
            assert manifest.json()["summary"] == doc.summary
            assert "All systems nominal." in (
                await retrieval.fetch_document(uid, doc.id)
            )["content"]
            inspector = client.get(f"/api/sessions/{sid}/inspector")
            assert inspector.status_code == 200
            assert inspector.json()["libraryDocuments"][0]["id"] == doc.id
            assert len(search.search_calls) == calls

        # Plain chat with an explicit empty selection never attempts retrieval,
        # even while the same configured Search backend is unavailable again.
        search.unavailable = True
        plain_sid = _new_session(client, [])
        calls = len(search.search_calls)
        _chat(client, plain_sid)
        assert len(search.search_calls) == calls
        assert retrieval._chunks is chunks
    finally:
        client.__exit__(None, None, None)


async def test_context_build_failure_is_explicit_and_redacted(monkeypatch, caplog):
    client = _make_client(document_understanding_enabled=True)
    try:
        sid = _new_session(client)
        _chat(client, sid)
        rows = client.get(f"/api/sessions/{sid}/messages").json()
        assert "library_retrieval_unavailable" not in rows[-1]["executionReceipt"]["notes"]

        async def failed_context(*args, **kwargs):
            raise TypeError("PRIVATE SOURCE BODY token=secret")

        monkeypatch.setattr(client.app.state.document_retrieval, "context", failed_context)
        _chat(client, sid)
        rows = client.get(f"/api/sessions/{sid}/messages").json()
        receipt = rows[-1]["executionReceipt"]
        assert "library_retrieval_unavailable" in receipt["notes"]
        assert receipt["partial"] is True
        assert receipt["status"] == "complete"
        assert "Library retrieval is unavailable" in json.dumps(receipt)
        assert "PRIVATE SOURCE BODY" not in json.dumps(receipt)
        assert "PRIVATE SOURCE BODY" not in caplog.text
    finally:
        client.__exit__(None, None, None)
