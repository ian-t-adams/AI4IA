"""Chat hot-path integration for Web IQ search on the MAIN CHAT (no @mention).

PR #63 wired the five web tools into the *agent* tool-loop only. This suite
covers the follow-up: broadening the plain-chat (``agent is None``) tool loop so
the main chat can search the live web when the feature is enabled (default-OFF).

End-to-end through the chat endpoint:

* **Web present, model calls a web tool.** A plain turn routes through the
  governed tool loop (``run_agent_turn``); the model calls ``web_search``, the
  fake Web IQ client runs, and the resolved answer is returned — NOT the normal
  ``gateway.complete`` fall-through.
* **Web present, model calls no tool / empty text.** The loop is entered but the
  turn falls through to the normal streaming answer (``gateway.complete``).
* **Web OFF and compute OFF (default).** The plain path is byte-for-byte
  unchanged: the tool loop is never entered and no tools are ever offered.
* **Compute active + web present.** Both tool sets are offered on one plain turn
  with no tool-name collision.

All IO is injected (in-memory stores + a fake Web IQ client + a scripted
tool-calling gateway); no network, no real key.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.library.blob_store import PARSED_NAME, blob_path
from ai4ia_api.library.compute_factory import build_document_compute
from ai4ia_api.library.models import DocumentStatus, UserDocument
from ai4ia_api.main import create_app
from ai4ia_api.websearch.factory import build_web_search_service
from tests.conftest import make_settings


class FakeWebClient:
    """Stand-in for WebSearchClient: returns canned rows and records calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    async def web_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "web", "query": query})
        return [{"title": "Weather", "url": "https://w.example/now", "content": "sunny"}]

    async def news_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "news", "query": query})
        return [{"title": "N", "url": "https://n.example/x", "content": "news"}]

    async def video_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "video", "query": query})
        return [{"title": "V", "url": "https://v.example/x", "content": "vid"}]

    async def image_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "image", "query": query})
        return [{"title": "I", "url": "https://i.example/x", "content": "img"}]

    async def browse(self, url, *, max_length, **kw):
        self.calls.append({"tool": "browse", "url": url})
        return {"url": url, "title": "T", "content": "page body"}

    async def close(self):
        self.closed = True


class ScriptedWebGateway:
    """Drives the plain-chat tool loop.

    ``mode`` decides what the model does on a *tools-bearing* call:
      * ``call_web``   -> request a ``web_search`` tool call, then a final answer.
      * ``empty``      -> return an empty answer (the loop falls through).
      * ``offer_only`` -> return a final answer immediately (no tool call).
    A *no-tools* call is the normal fall-through path; it returns canned text and
    is counted so tests can prove whether the loop fell through.
    """

    def __init__(self, *, mode: str) -> None:
        self.mode = mode
        self.offered: list[list[str]] = []  # tool names offered on each tools call
        self.no_tools_calls = 0
        self._web_requested = False

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        tools = (params or {}).get("tools")
        if tools:
            self.offered.append([t["function"]["name"] for t in tools])
            if self.mode == "empty":
                return {"choices": [{"message": {"role": "assistant", "content": ""}}]}
            if self.mode == "offer_only":
                return {"choices": [{"message": {"role": "assistant", "content": "Both tool sets were offered."}}]}
            if not self._web_requested:
                self._web_requested = True
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c1",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": json.dumps({"query": "weather today"}),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"role": "assistant", "content": "Here is what I found on the web."}}]}
        self.no_tools_calls += 1
        return {"choices": [{"message": {"role": "assistant", "content": "Normal streaming answer."}}]}

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


def _inject_web(client: TestClient, web_client: FakeWebClient) -> None:
    """Force the web-search service ON with an injected fake client, independent of
    the app's configured flag (mirrors how the compute tests inject compute)."""
    client.app.state.web_search = build_web_search_service(
        make_settings(web_search_enabled=True, web_search_max_results=5),
        entitlements=client.app.state.entitlements,
        metering=client.app.state.usage,
        client=web_client,
    )


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


def _inject_compute(client: TestClient, ci) -> None:
    client.app.state.document_compute = build_document_compute(
        client.app.state.settings,
        ingestor=client.app.state.document_ingestor,
        retrieval=client.app.state.document_retrieval,
        code_interpreter=ci,
    )


class _FakeCI:
    async def run(self, *, instructions, user_input, file_ids=None):
        from ai4ia_api.code_interpreter.models import CodeInterpreterResult

        return CodeInterpreterResult(status="completed", output_text="30")

    async def close(self):
        return None


# --- main chat + web present: the model calls web_search via the plain loop ---
def test_main_chat_web_search_runs_through_tool_loop():
    client = _make_client()  # web/compute OFF by default; we inject web below
    try:
        assert client.app.state.document_compute is None
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(mode="call_web")
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What's the weather today?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        # Answer came from the tool loop, not the normal fall-through completion.
        assert resp.json()["message"]["content"] == "Here is what I found on the web."
        # The Web IQ client was actually invoked by the main chat.
        assert any(c["tool"] == "web" for c in web.calls)
        # The plain tool loop ran (web_search was offered) and we never fell
        # through to a no-tools gateway.complete.
        assert gw.offered and "web_search" in gw.offered[0]
        assert gw.no_tools_calls == 0
    finally:
        client.__exit__(None, None, None)


# --- main chat + web present, model calls no tool: falls through to normal answer ---
def test_main_chat_web_present_no_tool_falls_through():
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(mode="empty")
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Just say hello.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        # Empty tool-loop answer -> normal completion answer is returned.
        assert resp.json()["message"]["content"] == "Normal streaming answer."
        # The loop WAS entered (tools offered) but no web tool was called and we
        # fell through to exactly one no-tools completion.
        assert gw.offered and "web_search" in gw.offered[0]
        assert web.calls == []
        assert gw.no_tools_calls == 1
    finally:
        client.__exit__(None, None, None)


# --- web OFF + compute OFF (default): no tool loop entered, zero regression ---
def test_web_off_compute_off_no_tool_loop():
    client = _make_client()
    try:
        assert client.app.state.web_search is None
        assert client.app.state.document_compute is None
        gw = ScriptedWebGateway(mode="offer_only")
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Hello there.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "Normal streaming answer."
        # No tools were EVER offered: the plain tool loop was not entered.
        assert gw.offered == []
        assert gw.no_tools_calls == 1
    finally:
        client.__exit__(None, None, None)


# --- compute active + web present on one plain turn: both offered, no collision ---
async def test_compute_and_web_both_offered_no_collision():
    client = _make_client(document_understanding_enabled=True, document_compute_enabled=True)
    try:
        uid = _uid(client)
        await _seed_ready_doc(client, uid)
        _inject_compute(client, _FakeCI())
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(mode="offer_only")
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Sum the amounts in the spreadsheet.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "Both tool sets were offered."
        # One plain tool loop offered BOTH capability sets with disjoint names.
        assert gw.offered, "tool loop was not entered"
        offered = gw.offered[0]
        assert "run_code" in offered  # compute
        assert "fetch_document" in offered  # document retrieval
        assert "web_search" in offered  # web search
        assert len(offered) == len(set(offered))  # no name collision
    finally:
        client.__exit__(None, None, None)
