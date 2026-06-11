"""Chat hot-path integration for the compute path (Phase 11C).

End-to-end through the chat endpoint:

* **Disabled by default (zero regression).** With document compute OFF,
  ``app.state.document_compute`` is None, the intent router never runs, neither
  compute tool is ever offered, and a normal answer is returned.
* **Enabled.** A compute-intent turn routes through the governed tool loop: the
  model calls ``run_code``, the Code Interpreter (a fake here) runs over the ready
  document, and the resolved answer is returned. A plain Q&A turn does NOT route
  to compute even when enabled.

All IO is injected (in-memory stores + a fake Code Interpreter + a scripted
tool-calling gateway); no network.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.code_interpreter.models import CodeInterpreterResult
from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.compute_factory import build_document_compute
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.main import create_app
from tests.conftest import make_settings


class FakeCI:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, *, instructions, user_input, file_ids=None):
        self.calls.append(
            {"instructions": instructions, "user_input": user_input, "file_ids": file_ids}
        )
        return CodeInterpreterResult(status="completed", output_text="The total is 30.")

    async def close(self):
        return None


class ScriptedGateway:
    """Returns a run_code tool-call on the first tools-bearing call, then a final
    answer. A plain completion (no tools) just returns canned text."""

    def __init__(self, doc_id: str = "") -> None:
        self.doc_id = doc_id
        self.calls = 0
        self.tool_calls_seen = 0

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        tools = (params or {}).get("tools")
        if tools and self.tool_calls_seen == 0:
            self.tool_calls_seen += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_code",
                                        "arguments": json.dumps(
                                            {"document_id": self.doc_id, "task": "sum the amounts"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "The total is 30."}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        yield ChatChunk(delta="hi", raw=json.dumps({"choices": [{"delta": {"content": "hi"}}]}))
        yield ChatChunk(done=True, raw="[DONE]")


def _make_client(**overrides) -> TestClient:
    app = create_app(make_settings(**overrides))
    c = TestClient(app)
    c.__enter__()
    return c


def _uid(client: TestClient) -> str:
    return client.get("/api/entitlement").json()["userId"]


def _new_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_ready_doc(client: TestClient, user_id: str) -> UserDocument:
    ingestor = client.app.state.document_ingestor
    doc = UserDocument(
        userId=user_id, filename="data.csv", status=DocumentStatus.ready, summary="amounts"
    )
    path = blob_path(user_id, doc.id, PARSED_NAME)
    await ingestor.blob.put(path, b"name,amount\nA,10\nB,20\n", "text/markdown")
    doc.parsedPath = path
    await ingestor.library.create_document(doc)
    return doc


def _inject_compute(client: TestClient, ci: FakeCI) -> None:
    settings = client.app.state.settings
    client.app.state.document_compute = build_document_compute(
        settings,
        ingestor=client.app.state.document_ingestor,
        retrieval=client.app.state.document_retrieval,
        code_interpreter=ci,
    )


# --- disabled by default: zero regression ---
def test_compute_disabled_by_default():
    client = _make_client()  # document_compute_enabled defaults False
    try:
        client.app.state.gateway = ScriptedGateway()
        assert client.app.state.document_compute is None
        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Calculate the total amount.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        # Normal answer; no tools were ever offered (first call had no tools).
        assert resp.json()["message"]["content"] == "The total is 30."
    finally:
        client.__exit__(None, None, None)


# --- enabled: compute-intent turn routes to run_code ---
async def test_compute_turn_invokes_run_code():
    client = _make_client(document_understanding_enabled=True, document_compute_enabled=True)
    try:
        uid = _uid(client)
        doc = await _seed_ready_doc(client, uid)
        ci = FakeCI()
        _inject_compute(client, ci)
        gw = ScriptedGateway(doc_id=doc.id)
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Sum the amounts in the spreadsheet.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "The total is 30."
        # The Code Interpreter was actually invoked over the ready document.
        assert len(ci.calls) == 1
        assert "name,amount" in ci.calls[0]["user_input"]
    finally:
        client.__exit__(None, None, None)


# --- enabled: a plain Q&A turn does NOT route to compute ---
async def test_qa_turn_does_not_invoke_compute():
    client = _make_client(document_understanding_enabled=True, document_compute_enabled=True)
    try:
        uid = _uid(client)
        doc = await _seed_ready_doc(client, uid)
        ci = FakeCI()
        _inject_compute(client, ci)
        client.app.state.gateway = ScriptedGateway(doc_id=doc.id)

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What does this document describe?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        # Q&A stays on the normal RAG path; the interpreter is never called.
        assert ci.calls == []
    finally:
        client.__exit__(None, None, None)
