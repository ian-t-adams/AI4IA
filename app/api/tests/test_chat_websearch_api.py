"""Chat hot-path integration for Web IQ search on the MAIN chat (default-OFF).

The owner's ask is that web search reach the **main chat** (no @mentioned agent),
not just tool-enabled agents. The main chat has ``agent is None`` and never enters
the agent tool path, so it is covered by the agent-less plain-chat tool loop. These
tests drive that path end-to-end through the chat endpoint:

* **Enabled — tool used.** A main-chat turn whose model calls ``web_search`` runs
  the governed tool loop, the (fake) Web IQ client is invoked, the nonce-fenced
  untrusted result is handed back to the model, and the final answer is returned.
* **Enabled — no tool.** A main-chat turn whose model calls no tool still returns
  the normal answer and never performs a search.
* **Disabled by default (zero regression).** With web search OFF and compute OFF,
  ``app.state.web_search`` is None, no tool loop engages, the gateway is never
  offered tools, and a normal answer is returned (streaming path unchanged).

All IO is injected (in-memory stores + a fake Web IQ client + a scripted
tool-calling gateway); no network and no real key.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ChatChunk
from ai4ia_api.main import create_app
from ai4ia_api.websearch.factory import build_web_search_service
from tests.conftest import make_settings


class FakeWebClient:
    """Stand-in for WebSearchClient: records calls and returns canned rows/page."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    async def web_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "web", "query": query, "max_results": max_results})
        return [
            {"title": "Headline one", "url": "https://a.example/1", "content": "alpha"},
            {"title": "Headline two", "url": "https://b.example/2", "content": "beta"},
        ]

    async def news_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "news", "query": query, "max_results": max_results})
        return []

    async def video_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "video", "query": query, "max_results": max_results})
        return []

    async def image_search(self, query, *, max_results, **kw):
        self.calls.append({"tool": "image", "query": query, "max_results": max_results})
        return []

    async def browse(self, url, *, max_length, **kw):
        self.calls.append({"tool": "browse", "url": url, "max_length": max_length})
        return {"url": url, "title": "Page", "content": "body"}

    async def close(self):
        self.closed = True


class ScriptedWebGateway:
    """First tools-bearing call returns a ``web_search`` tool-call; the next call
    returns the final answer. A plain (no-tools) completion returns canned text. The
    tool-result messages seen on the follow-up call are captured so a test can assert
    the nonce fence reached the model."""

    def __init__(self, *, call_tool: bool = True) -> None:
        self.call_tool = call_tool
        self.calls = 0
        self.tool_calls_seen = 0
        self.tools_offered_first_call: bool | None = None
        self.tool_result_messages: list[str] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        tools = (params or {}).get("tools")
        if self.tools_offered_first_call is None:
            self.tools_offered_first_call = bool(tools)
        # Capture any tool-result messages so the test can inspect the fenced payload.
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "tool":
                self.tool_result_messages.append(str(m.get("content", "")))
        if self.call_tool and tools and self.tool_calls_seen == 0:
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
                                        "name": "web_search",
                                        "arguments": json.dumps({"query": "today headlines"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "Here is the answer."}}]}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        yield ChatChunk(delta="hi", raw=json.dumps({"choices": [{"delta": {"content": "hi"}}]}))
        yield ChatChunk(done=True, raw="[DONE]")


def _make_client(**overrides) -> TestClient:
    app = create_app(make_settings(**overrides))
    c = TestClient(app)
    c.__enter__()
    return c


def _new_session(client: TestClient) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _inject_web(client: TestClient, web_client: FakeWebClient) -> None:
    """Override app.state.web_search with a service backed by a fake Web IQ client
    (mirrors how the compute tests inject a fake Code Interpreter)."""
    client.app.state.web_search = build_web_search_service(
        make_settings(web_search_enabled=True),
        entitlements=client.app.state.entitlements,
        metering=client.app.state.usage,
        client=web_client,
    )


# --- enabled: main chat (agent None) routes a web turn through web_search ---
def test_main_chat_invokes_web_search():
    client = _make_client()  # compute + retrieval off; only web is injected
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        assert client.app.state.web_search is not None
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What is happening in the news today?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "Here is the answer."
        # The main chat (no @mention) actually reached the live web client.
        assert len(web.calls) == 1 and web.calls[0]["tool"] == "web"
        # Tools were offered on the very first model call (main-chat coverage).
        assert gw.tools_offered_first_call is True
        # The untrusted result handed back to the model is nonce-fenced.
        assert gw.tool_result_messages
        assert "BEGIN RESULTS" in gw.tool_result_messages[0]
    finally:
        client.__exit__(None, None, None)


# --- enabled: main chat that calls no tool returns the normal answer, no search ---
def test_main_chat_no_tool_call_returns_answer_without_search():
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(call_tool=False)
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Say hello.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "Here is the answer."
        # No tool was called, so no web search was performed.
        assert web.calls == []
    finally:
        client.__exit__(None, None, None)


# --- disabled by default: plain streaming path is byte-for-byte unchanged ---
def test_plain_path_unchanged_when_web_search_off():
    client = _make_client()  # web_search_enabled + document_compute default OFF
    try:
        assert client.app.state.web_search is None
        assert client.app.state.document_compute is None
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What is happening in the news today?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        # Normal answer; the tool loop never engaged, so the gateway was never
        # offered tools on its first (and only) completion call.
        assert resp.json()["message"]["content"] == "Here is the answer."
        assert gw.tools_offered_first_call is False
        assert gw.tool_calls_seen == 0
    finally:
        client.__exit__(None, None, None)
