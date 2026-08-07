"""End-to-end test for a tool-enabled agent turn through POST /api/chat.

The default catalog gives @analyst the ``calculator`` tool, so a scripted gateway
that asks for the calculator then answers exercises the full runtime path:
registry authorization -> execution -> result fed back -> final answer persisted.
"""
from __future__ import annotations

import json

from tests.conftest import stream_like_gateway


def _create_session(client, model="gpt-5.2"):
    resp = client.post("/api/sessions", json={"title": "Chat", "model": model})
    assert resp.status_code == 201, resp.text
    return resp.json()


class ToolThenAnswerGateway:
    """First completion asks for the calculator; the second returns prose."""

    def __init__(self) -> None:
        self.calls = 0
        self.saw_tool_result = False

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        if self.calls == 1:
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
                                        "name": "calculator",
                                        "arguments": json.dumps({"expression": "6*7"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        # The second call must include the tool result for c1.
        if any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in messages):
            self.saw_tool_result = True
        return {"choices": [{"message": {"role": "assistant", "content": "It is 42."}}]}

    async def stream(self, **kwargs):
        # The tool loop token-streams since P1-16, so this double speaks SSE and
        # its tool call arrives in fragments — which is what actually exercises
        # the accumulator that reassembles it.
        async for chunk in stream_like_gateway(await self.complete(**kwargs)):
            yield chunk


def test_tool_enabled_agent_runs_tool_and_persists_answer(client):
    gw = ToolThenAnswerGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@analyst what is 6*7?", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"]["content"] == "It is 42."
    assert gw.calls == 2
    assert gw.saw_tool_result is True

    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "what is 6*7?"  # mention stripped
    assert messages[1]["content"] == "It is 42."
    assert messages[1]["agent"] == "analyst"


def test_tool_enabled_agent_streaming_emits_steps_then_answer(client):
    gw = ToolThenAnswerGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@analyst compute 6*7", "stream": True},
    )
    assert resp.status_code == 200, resp.text
    # Live activity events precede the answer, then the answer + DONE.
    assert '"step"' in resp.text
    assert "Calculat" in resp.text  # tool_start "Calculating" / tool_result "Calculated"
    assert "[DONE]" in resp.text
    # The answer arrives as SEVERAL deltas, not one blob at the end (P1-16). The
    # count assertion is the non-vacuity guard: a single terminal delta whose
    # content happens to concatenate correctly would pass the equality check
    # alone while reproducing exactly the defect this change removes.
    deltas = [
        (json.loads(line.removeprefix("data: "))["choices"][0]["delta"].get("content") or "")
        for line in resp.text.splitlines()
        if line.startswith("data: ")
        and line != "data: [DONE]"
        and "choices" in json.loads(line.removeprefix("data: "))
    ]
    assert len(deltas) > 1
    assert "".join(deltas) == "It is 42."
    # The tool loop ran (two model round trips), both of them streamed.
    assert gw.calls == 2

    # What streamed is what was saved, and the redacted trace is persisted on the
    # assistant message for after-the-fact view.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assistant = messages[-1]
    assert assistant["role"] == "assistant" and assistant["content"] == "It is 42."
    steps = assistant.get("steps") or []
    assert any(s["kind"] == "tool_result" and s["tool"] == "calculator" for s in steps)


def test_toolless_agent_does_not_invoke_tool_loop(client):
    """@coder has no tools; its turn must use the direct (single-call) path."""
    gw = ToolThenAnswerGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@coder hello", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    # Only one model call, and the first scripted response (a tool_call) is NOT
    # interpreted as tools here — the direct path extracts content (None -> "").
    assert gw.calls == 1


def test_tool_agent_on_responses_model_is_rejected_before_persist(client):
    """A tool-enabled agent pinned to a Responses-API model is refused with 422
    BEFORE any model call or persistence — the chat-completions tool loop has no
    Responses equivalent yet, so the turn must not run or leave a dangling
    message / rebind the session to an unusable model."""
    gw = ToolThenAnswerGateway()
    client.app.state.gateway = gw
    sid = _create_session(client)["id"]

    resp = client.post(
        "/api/chat",
        json={
            "sessionId": sid,
            "content": "@analyst what is 6*7?",
            "model": "gpt-5-pro",
            "stream": False,
        },
    )
    assert resp.status_code == 422, resp.text
    assert gw.calls == 0
    # No user/assistant message was persisted for the refused turn.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert messages == []
    # The session stays on its original model, not the unusable Responses one.
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["model"] == "gpt-5.2"
