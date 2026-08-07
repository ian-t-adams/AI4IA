"""Citation provenance end to end through the chat endpoint (audit P1-14).

The unit tests in ``test_citation_provenance.py`` prove the verifier; these prove
the *wiring*, which is the part that would rot silently. A verifier nothing calls
is worth nothing, so each path that persists an assistant turn is exercised with
a fabricated citation and with a real one, and the persisted row is read back
through the API the browser actually uses.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.doc_chunks import DocChunkRecord
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.library.retrieval import DocumentRetrievalService
from ai4ia_api.main import create_app
from tests.conftest import make_settings

_CHUNK_TEXT = "Falcon shipped on the fourteenth of March."


def _vector(settings) -> list[float]:
    """A unit vector of the width the configured embedding model produces.

    The in-memory chunk store enforces the dimension the real one indexes at, so
    a hand-rolled 3-float vector would be rejected exactly as Azure AI Search
    would reject it."""
    dim = settings.memory_embedding_dimensions
    return [1.0] + [0.0] * (dim - 1)


class ScriptedGateway:
    """Replies with a fixed answer and records the prompt it was given."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_messages: list[dict] | None = None

    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.last_messages = list(messages)
        return {"choices": [{"message": {"role": "assistant", "content": self.reply}}]}

    async def stream(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        self.last_messages = list(messages)
        yield ChatChunk(
            delta=self.reply,
            raw=json.dumps({"choices": [{"delta": {"content": self.reply}}]}),
        )
        yield ChatChunk(done=True, raw="[DONE]")


class FixedEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, inputs):
        return [list(self._vector) for _ in inputs]

    async def embed_one(self, text):
        return list(self._vector)


def _make_client(reply: str) -> TestClient:
    client = TestClient(create_app(make_settings(document_understanding_enabled=True)))
    client.__enter__()
    client.app.state.gateway = ScriptedGateway(reply)
    ingestor = client.app.state.document_ingestor
    # The real embedder is bound to the gateway built at startup, not to the one
    # the test swaps in, so rebuild retrieval over the ingestor's own stores with
    # a deterministic embedder. Everything else is the production service.
    client.app.state.document_retrieval = DocumentRetrievalService(
        library=ingestor.library,
        blob_store=ingestor.blob,
        chunk_store=ingestor.chunks,
        embedder=FixedEmbedder(_vector(client.app.state.settings)),
        settings=client.app.state.settings,
    )
    return client


def _uid(client: TestClient) -> str:
    return client.get("/api/entitlement").json()["userId"]


def _session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_indexed_doc(client: TestClient, user_id: str) -> UserDocument:
    ingestor = client.app.state.document_ingestor
    doc = UserDocument(
        userId=user_id,
        filename="falcon.md",
        status=DocumentStatus.ready,
        summary="Project Falcon status brief",
    )
    path = blob_path(user_id, doc.id, PARSED_NAME)
    await ingestor.blob.put(path, _CHUNK_TEXT.encode("utf-8"), "text/markdown")
    doc.parsedPath = path
    await ingestor.library.create_document(doc)
    await ingestor.chunks.add_many(
        [
            DocChunkRecord(
                user_id=user_id,
                document_id=doc.id,
                chunk_index=0,
                content=_CHUNK_TEXT,
                heading="Timeline",
                char_start=0,
                char_end=len(_CHUNK_TEXT),
            )
        ],
        [_vector(client.app.state.settings)],
    )
    return doc


def _ask(client: TestClient, sid: str, *, stream: bool) -> None:
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "When did Falcon ship?", "stream": stream},
    )
    assert resp.status_code == 200, resp.text
    if stream:
        # Drain so the terminal upsert runs inside the response lifecycle.
        assert "[DONE]" in resp.text


def _assistant_row(client: TestClient, sid: str) -> dict:
    resp = client.get(f"/api/sessions/{sid}/messages")
    assert resp.status_code == 200, resp.text
    rows = [m for m in resp.json() if m["role"] == "assistant"]
    assert rows, "expected a persisted assistant message"
    return rows[-1]


async def _run(reply: str, *, stream: bool) -> dict:
    client = _make_client(reply)
    try:
        uid = _uid(client)
        await _seed_indexed_doc(client, uid)
        sid = _session(client)
        _ask(client, sid, stream=stream)
        return _assistant_row(client, sid)
    finally:
        client.__exit__(None, None, None)


async def test_the_prompt_and_the_registry_agree_on_the_span_id():
    client = _make_client("noop")
    try:
        uid = _uid(client)
        doc = await _seed_indexed_doc(client, uid)
        sid = _session(client)
        _ask(client, sid, stream=False)

        library = next(
            m["content"]
            for m in client.app.state.gateway.last_messages
            if "BEGIN LIBRARY" in m["content"]
        )
        row = _assistant_row(client, sid)
        assert row["sources"], "an indexed ready document must produce a registry"
        span = row["sources"][0]
        # The id in the prompt is the id in the receipt; if these ever diverge
        # every honest citation would be reported as fabricated.
        assert f"cite-as: [[cite:{span['spanId']}]]" in library
        assert span["documentId"] == doc.id
        assert span["filename"] == "falcon.md"
        assert span["excerpt"] == _CHUNK_TEXT
        assert len(span["contentSha256"]) == 64
    finally:
        client.__exit__(None, None, None)


async def test_non_streaming_turn_marks_a_fabricated_citation():
    row = await _run("It shipped in March [[cite:S7]].", stream=False)

    assert [c["status"] for c in row["citations"]] == ["unverified"]
    assert row["citations"][0]["spanId"] == "S7"
    assert row["citations"][0]["documentId"] is None


async def test_non_streaming_turn_verifies_a_real_citation():
    # The control for the test above: same path, same shape of answer, the only
    # difference is that the id was actually retrieved.
    row = await _run("It shipped in March [[cite:S1]].", stream=False)

    assert [c["status"] for c in row["citations"]] == ["verified"]
    assert row["citations"][0]["filename"] == "falcon.md"
    assert row["citations"][0]["documentId"] == row["sources"][0]["documentId"]


async def test_streaming_turn_marks_a_fabricated_citation():
    row = await _run("It shipped in March [[cite:S7]].", stream=True)

    assert row["status"] == "complete"
    assert [c["status"] for c in row["citations"]] == ["unverified"]


async def test_streaming_turn_verifies_a_real_citation():
    row = await _run("It shipped in March [[cite:S1]].", stream=True)

    assert row["status"] == "complete"
    assert [c["status"] for c in row["citations"]] == ["verified"]


async def test_the_registry_rides_the_first_stream_frame():
    client = _make_client("It shipped in March [[cite:S1]].")
    try:
        uid = _uid(client)
        await _seed_indexed_doc(client, uid)
        sid = _session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "When?", "stream": True},
        )
        assert resp.status_code == 200, resp.text
        first = json.loads(
            resp.text.split("\n\n")[0].removeprefix("data: ").strip()
        )["metadata"]

        # Minted before the model runs, so the browser can mark citations as they
        # stream instead of showing raw tokens until the row is refetched.
        assert [s["spanId"] for s in first["sources"]] == ["S1"]
        assert first["sources"][0]["filename"] == "falcon.md"
    finally:
        client.__exit__(None, None, None)


async def test_an_unattested_turn_omits_sources_from_the_stream_frame():
    client = TestClient(create_app(make_settings()))  # library off
    client.__enter__()
    try:
        client.app.state.gateway = ScriptedGateway("Nothing to cite.")
        sid = _session(client)
        resp = client.post(
            "/api/chat", json={"sessionId": sid, "content": "hi", "stream": True}
        )
        first = json.loads(
            resp.text.split("\n\n")[0].removeprefix("data: ").strip()
        )["metadata"]

        # The base frame stays byte-identical to what it has always been.
        assert set(first) == {"userMessageId", "assistantMessageId"}
    finally:
        client.__exit__(None, None, None)


async def test_an_uncited_answer_still_keeps_the_registry():
    row = await _run("It shipped in March.", stream=False)

    # No citations to check, but the sources remain a durable record of what the
    # answer could have used -- that is what makes "it cited nothing" auditable.
    assert row["citations"] is None
    assert len(row["sources"]) == 1


def test_a_turn_without_retrieval_is_left_unattested():
    client = TestClient(create_app(make_settings()))  # library off by default
    client.__enter__()
    try:
        client.app.state.gateway = ScriptedGateway("Nothing to cite [[cite:S1]].")
        sid = _session(client)
        _ask(client, sid, stream=False)
        row = _assistant_row(client, sid)

        # Retrieval never ran, so there is no registry to judge against and the
        # row says so, rather than accusing the answer on missing evidence.
        assert row["sources"] is None
        assert row["citations"] is None
    finally:
        client.__exit__(None, None, None)
