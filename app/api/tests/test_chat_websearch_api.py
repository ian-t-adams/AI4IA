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

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayError
from ai4ia_api.main import create_app
from ai4ia_api.routers.chat import _TOOLS_UNAVAILABLE_NOTICE
from ai4ia_api.websearch.factory import build_web_search_service
from ai4ia_api.websearch.contracts import WEBIQ_TOOL_NAMES
from tests.conftest import make_settings, sse_chunks


class FakeWebClient:
    """Stand-in for WebSearchClient: records calls and returns canned rows/page."""

    def __init__(self, *, structured=None) -> None:
        self.calls: list[dict] = []
        self.closed = False
        self.structured = structured if structured is not None else {
            "weatherResults": {"temperature": 24, "unit": "C", "sourceUrl": "https://example.com/weather",
                               "timestamp": "2026-09-06T12:00:00Z"},
        }

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

    async def structured_search(self, query, **kw):
        self.calls.append({"tool": "structured", "query": query, **kw})
        return self.structured

    classic_search = structured_search
    finance_search = structured_search
    places_search = structured_search
    sports_search = structured_search
    sonic_search = structured_search
    autosuggest = structured_search

    async def close(self):
        self.closed = True


class ScriptedWebGateway:
    """First tools-bearing call returns a ``web_search`` tool-call; the next call
    returns the final answer. A plain (no-tools) completion returns canned text. The
    tool-result messages seen on the follow-up call are captured so a test can assert
    the nonce fence reached the model."""

    def __init__(self, *, call_tool: bool = True, tool_name: str = "web_search") -> None:
        self.call_tool = call_tool
        self.tool_name = tool_name
        self.calls = 0
        self.tool_calls_seen = 0
        self.tools_offered_first_call: bool | None = None
        self.first_tools: list[dict] | None = None
        self.first_messages = None
        self.tool_result_messages: list[str] = []
        self.apis: list[str] = []

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        self.apis.append(api)
        if self.first_messages is None:
            self.first_messages = messages
        tools = (params or {}).get("tools")
        if self.tools_offered_first_call is None:
            self.tools_offered_first_call = bool(tools)
            self.first_tools = tools or []
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
                                        "name": self.tool_name,
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
        response = await self.complete(
            deployment=deployment,
            messages=messages,
            params=params,
            correlation_id=correlation_id,
            api=api,
        )
        for chunk in sse_chunks(response):
            yield chunk


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


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("tool", [
    "classic_search", "finance_search", "places_search", "sports_search",
    "sonic_search", "web_autosuggest",
])
def test_main_chat_invokes_structured_webiq_capabilities(tool, stream):
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gateway = ScriptedWebGateway(tool_name=tool)
        client.app.state.gateway = gateway
        sid = _new_session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Use WebIQ to find the current weather.", "stream": stream,
        })
        assert response.status_code == 200, response.text
        assert len(web.calls) == 1 and web.calls[0]["tool"] == "structured"
        result = json.loads(gateway.tool_result_messages[0])
        assert "weatherResults" in result["results"]
        assert "2026-09-06T12:00:00Z" in result["results"]
        assert "https://example.com/weather" in result["results"]
        assert result["results"].splitlines()[-1].startswith("END RESULTS ")
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("repetitions", [1, 100], ids=["within-budget", "over-budget"])
def test_rich_webiq_output_reaches_the_model_without_cutting_its_nonce_fence(stream, repetitions):
    client = _make_client()
    try:
        web = FakeWebClient(structured={
            "webResults": [{"url": f"https://example.com/{index}",
                            "content": "source passage with ordinary prose " * repetitions}
                           for index in range(4)],
        })
        _inject_web(client, web)
        gateway = ScriptedWebGateway(tool_name="classic_search")
        client.app.state.gateway = gateway
        response = client.post("/api/chat", json={
            "sessionId": _new_session(client), "content": "Read the sources with WebIQ.", "stream": stream,
        })
        assert response.status_code == 200, response.text
        assert len(web.calls) == 1
        delivered = gateway.tool_result_messages[0]
        assert len(delivered.encode("utf-8")) <= 8192
        result = json.loads(delivered)
        assert result["truncated"] is (repetitions == 100)
        assert result["results"].splitlines()[-1].startswith("END RESULTS ")
        body = result["results"].split("\n", 1)[1].rsplit("\n", 1)[0]
        rows = json.loads(body)["webResults"]
        assert [row["url"] for row in rows] == [f"https://example.com/{index}" for index in range(4)]
        assert all(row["content"].startswith("source passage") for row in rows)
        if repetitions == 1:
            assert all(row["content"] == "source passage with ordinary prose " for row in rows)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
@pytest.mark.parametrize("web_enabled", [False, True], ids=["disabled", "enabled"])
@pytest.mark.parametrize("route", ["main", "conversation-tools", "mentioned-agent"])
def test_chat_advertises_webiq_only_when_available(route, web_enabled, stream):
    client = _make_client()
    try:
        web = FakeWebClient()
        if web_enabled:
            _inject_web(client, web)
        gateway = ScriptedWebGateway(call_tool=web_enabled)
        client.app.state.gateway = gateway

        session_body = {"model": "gpt-5.2"}
        if route == "conversation-tools":
            session_body["toolOverrides"] = {"added": ["get_current_time"], "removed": []}
        created = client.post("/api/sessions", json=session_body)
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        content = "What's the weather in Chicago? Can you use WebIQ?"
        if route == "mentioned-agent":
            content = f"@general {content}"

        response = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": content, "stream": stream},
        )
        assert response.status_code == 200, response.text
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        assert messages[-1]["content"] == "Here is the answer."
        assert messages[-1]["agent"] == {
            "main": None,
            "conversation-tools": "conversation",
            "mentioned-agent": "general",
        }[route]
        assert client.get(f"/api/sessions/{sid}").json()["agentName"] is None

        assert gateway.first_tools is not None
        functions = {tool["function"]["name"]: tool["function"] for tool in gateway.first_tools}
        web_names = WEBIQ_TOOL_NAMES
        if web_enabled:
            assert web_names <= functions.keys()
            for name in web_names:
                description = functions[name]["description"].lower()
                assert "webiq" in description
                assert "web iq" in description
            assert [call["tool"] for call in web.calls] == ["web"]
            assert gateway.tool_result_messages
            assert "BEGIN RESULTS" in gateway.tool_result_messages[0]
        else:
            assert web_names.isdisjoint(functions)
            assert all("webiq" not in tool["description"].lower() for tool in functions.values())
            assert web.calls == []
    finally:
        client.__exit__(None, None, None)


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


# --- /research: disabled/default gives a local friendly reply ---
def test_research_slash_command_not_enabled_when_web_search_off():
    client = _make_client()
    try:
        assert client.app.state.web_search is None
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "/research latest AI news", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert "/research isn't enabled" in resp.json()["message"]["content"]
        assert gw.calls == 0
    finally:
        client.__exit__(None, None, None)


def test_research_slash_command_usage_when_empty():
    client = _make_client()
    try:
        _inject_web(client, FakeWebClient())

        sid = _new_session(client)
        resp = client.post(
            "/api/chat", json={"sessionId": sid, "content": "/research", "stream": False}
        )
        assert resp.status_code == 200, resp.text
        assert "Usage: /research" in resp.json()["message"]["content"]
    finally:
        client.__exit__(None, None, None)


# --- /research: enabled routes through the Web IQ capability tool loop ---
def test_research_slash_command_invokes_web_search_capability():
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _new_session(client)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "/research latest AI news", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"]["content"] == "Here is the answer."
        assert len(web.calls) == 1 and web.calls[0]["tool"] == "web"
        assert gw.tools_offered_first_call is True
        assert gw.first_messages[0]["role"] == "system"
        assert "Web IQ tools" in gw.first_messages[0]["content"]
        assert {"role": "user", "content": "latest AI news"} in gw.first_messages
        assert gw.tool_result_messages
        assert "BEGIN RESULTS" in gw.tool_result_messages[0]
    finally:
        client.__exit__(None, None, None)


# --- Responses-API models: capability loss is announced, not silent ---------
#
# The plain-chat tool loop is built against the chat-completions wire format, so a
# Responses models use this same governed loop through the gateway adapter. A
# genuinely non-tool-capable model still receives an explicit notice instead of
# silently answering as though it had searched.


def _session_with_model(client: TestClient, model: str) -> str:
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_responses_model_can_use_grounding_tools():
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _session_with_model(client, "gpt-5-pro")
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What happened in the news today?", "stream": False},
        )
        assert resp.status_code == 200, resp.text

        assert gw.tools_offered_first_call is True
        assert [call["tool"] for call in web.calls] == ["web"]
        assert gw.apis == ["responses", "responses"]
        systems = [m["content"] for m in gw.first_messages if m.get("role") == "system"]
        assert _TOOLS_UNAVAILABLE_NOTICE not in systems
    finally:
        client.__exit__(None, None, None)


def test_non_tool_chat_model_is_told_its_grounding_tools_are_missing():
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _session_with_model(client, "DeepSeek-V3.2")
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What happened today?", "stream": False},
        )

        assert resp.status_code == 200, resp.text
        assert gw.tools_offered_first_call is False
        assert web.calls == []
        systems = [
            message["content"]
            for message in gw.first_messages
            if message.get("role") == "system"
        ]
        assert _TOOLS_UNAVAILABLE_NOTICE in systems
    finally:
        client.__exit__(None, None, None)


def test_responses_model_gets_no_notice_when_no_capabilities_were_possible():
    # INERTNESS: with web search off and compute off there was nothing to lose, so
    # the turn must be byte-for-byte what it always was — no spurious notice
    # telling the model it lacks tools it was never going to be offered.
    client = _make_client()
    try:
        gw = ScriptedWebGateway(call_tool=False)
        client.app.state.gateway = gw
        assert client.app.state.web_search is None

        sid = _session_with_model(client, "gpt-5-pro")
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "Say hello.", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        systems = [m["content"] for m in gw.first_messages if m.get("role") == "system"]
        assert all(s != _TOOLS_UNAVAILABLE_NOTICE for s in systems)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("model", ["gpt-5.2", "MAI-Thinking-1"])
def test_chat_completions_model_still_gets_tools_not_a_notice(model):
    # The chat-completions path must be untouched: real tools, and never the
    # "you have no tools" notice (which would be an outright lie there).
    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gw = ScriptedWebGateway(call_tool=True)
        client.app.state.gateway = gw

        sid = _session_with_model(client, model)
        resp = client.post(
            "/api/chat",
            json={"sessionId": sid, "content": "What is happening today?", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        assert gw.tools_offered_first_call is True
        systems = [m["content"] for m in gw.first_messages if m.get("role") == "system"]
        assert all(s != _TOOLS_UNAVAILABLE_NOTICE for s in systems)
    finally:
        client.__exit__(None, None, None)


def test_nonstreaming_plain_tool_failure_persists_partial_error_without_fallthrough():
    class FailingSecondIterationGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.entered_second_iteration = False
            self.fallback_calls = 0

        async def complete(self, *, messages, params=None, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Searching. ",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": json.dumps(
                                                {"query": "today headlines"}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 3,
                        "total_tokens": 20,
                    },
                }
            if self.calls == 2:
                self.entered_second_iteration = any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == "call-1"
                    for message in messages
                )
                raise ModelGatewayError(502, "second iteration failed")
            self.fallback_calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "fallback answer",
                        },
                        "content_filter_results": {
                            "violence": {
                                "filtered": False,
                                "severity": "low",
                            }
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 99,
                    "completion_tokens": 1,
                    "total_tokens": 100,
                },
            }

    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gateway = FailingSecondIterationGateway()
        metered: list[dict] = []

        async def capture_usage(**kwargs):
            metered.append(kwargs)

        client.app.state.gateway = gateway
        client.app.state.usage.record_completion = capture_usage
        session_id = _new_session(client)

        response = client.post(
            "/api/chat",
            json={
                "sessionId": session_id,
                "content": "What is happening in the news today?",
                "stream": False,
            },
        )
        messages = client.get(f"/api/sessions/{session_id}/messages").json()
    finally:
        client.__exit__(None, None, None)

    assistants = [message for message in messages if message["role"] == "assistant"]
    assistant = assistants[-1] if assistants else None
    usage = metered[-1]["usage"] if metered else None
    assert {
        "status_code": response.status_code,
        "calls": gateway.calls,
        "entered_second_iteration": gateway.entered_second_iteration,
        "fallback_calls": gateway.fallback_calls,
        "handler_calls": [(call["tool"], call["query"]) for call in web.calls],
        "assistant_status": assistant["status"] if assistant else None,
        "steps": [
            (step["kind"], step["tool"])
            for step in (assistant.get("steps") or [] if assistant else [])
        ],
        "metered": (
            usage.prompt,
            usage.completion,
            usage.total,
            usage.calls,
            usage.complete,
            metered[-1]["status"],
        )
        if usage is not None
        else None,
    } == {
        "status_code": 502,
        "calls": 2,
        "entered_second_iteration": True,
        "fallback_calls": 0,
        "handler_calls": [("web", "today headlines")],
        "assistant_status": "error",
        "steps": [("delegate", "web_search")],
        "metered": (17, 3, 20, 2, False, "error"),
    }
    assert response.json()["detail"] == "Chat completion failed."


def test_first_nonstreaming_plain_tool_failure_falls_through_to_normal_chat(caplog):
    marker = "nonstream-fallback-hostile-detail"

    class FirstCallFailureGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ModelGatewayError(502, marker)
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "fallback answer",
                        },
                        "content_filter_results": {
                            "violence": {
                                "filtered": False,
                                "severity": "low",
                            }
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            }

    client = _make_client()
    try:
        web = FakeWebClient()
        _inject_web(client, web)
        gateway = FirstCallFailureGateway()
        metered: list[dict] = []

        async def capture_usage(**kwargs):
            metered.append(kwargs)

        client.app.state.gateway = gateway
        client.app.state.usage.record_completion = capture_usage
        session_id = _new_session(client)

        response = client.post(
            "/api/chat",
            json={"sessionId": session_id, "content": "find news", "stream": False},
        )
        messages = client.get(f"/api/sessions/{session_id}/messages").json()
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 200
    assert gateway.calls == 2
    assert web.calls == []
    assert messages[-1]["status"] == "complete"
    assert messages[-1]["content"] == "fallback answer"
    usage = metered[-1]["usage"]
    assert (
        usage.prompt,
        usage.completion,
        usage.total,
        usage.calls,
        usage.complete,
        metered[-1]["status"],
    ) == (5, 2, 7, 2, False, "complete")
    assert messages[-1]["executionReceipt"]["iterations"] == 2
    assert messages[-1]["executionReceipt"]["usage"]["calls"] == 2
    assert messages[-1]["safety"]["signals"][0]["modelCall"] == 2
    assert marker not in caplog.text


def test_nonstreaming_plain_tool_and_normal_fallback_failures_count_both(caplog):
    first_marker = "plain-tool-first-hostile-detail"
    second_marker = "normal-fallback-second-hostile-detail"

    class TwiceFailingGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            marker = first_marker if self.calls == 1 else second_marker
            raise ModelGatewayError(502, marker)

    client = _make_client()
    try:
        _inject_web(client, FakeWebClient())
        gateway = TwiceFailingGateway()
        metered: list[dict] = []

        async def capture_usage(**kwargs):
            metered.append(kwargs)

        client.app.state.gateway = gateway
        client.app.state.usage.record_completion = capture_usage
        session_id = _new_session(client)
        response = client.post(
            "/api/chat",
            json={"sessionId": session_id, "content": "find news", "stream": False},
        )
        messages = client.get(f"/api/sessions/{session_id}/messages").json()
    finally:
        client.__exit__(None, None, None)

    usage = metered[-1]["usage"]
    assert response.status_code == 502
    assert gateway.calls == 2
    assert messages[-1]["status"] == "error"
    assert (usage.calls, usage.known, usage.complete, metered[-1]["status"]) == (
        2,
        False,
        False,
        "error",
    )
    assert first_marker not in caplog.text
    assert second_marker not in caplog.text
