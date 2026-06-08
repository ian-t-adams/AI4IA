"""End-to-end test for a multi-agent orchestrator turn through POST /api/chat.

A user creates a leaf agent (@helper) and an orchestrator (@boss) that links to
it. A scripted gateway drives the full delegation path: the supervisor asks to
``delegate_to_agent`` -> the linked agent runs as a sub-turn on the supervisor's
deployment -> its answer feeds back -> the supervisor composes the final reply.
Usage from all three model calls is metered to the single supervisor deployment.
"""
from __future__ import annotations

import json

_USAGE = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


def _mk(client, **body):
    return client.post("/api/agents", json=body)


class _OrchestratorGateway:
    """Routes by the system prompt + presence of a tool result:

    * helper sub-turn (system == helper prompt) -> a leaf answer
    * supervisor first call -> a delegate_to_agent tool call
    * supervisor call after the delegate result -> the final answer
    """

    def __init__(self) -> None:
        self.calls = 0
        self.delegated_task = None

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        system = (messages[0].get("content") if messages else "") or ""
        has_tool_result = any(m.get("role") == "tool" for m in messages)

        if "you are helper" in system.lower():
            return {
                "choices": [{"message": {"role": "assistant", "content": "Helper computed 42."}}],
                "usage": _USAGE,
            }
        if not has_tool_result:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "d1",
                                    "type": "function",
                                    "function": {
                                        "name": "delegate_to_agent",
                                        "arguments": json.dumps(
                                            {"agent": "helper", "task": "compute 6*7"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": _USAGE,
            }
        # Capture what the supervisor saw back from the delegate, then finalize.
        for m in messages:
            if m.get("role") == "tool":
                self.delegated_task = m.get("content")
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "The helper says: 42."}}
            ],
            "usage": _USAGE,
        }

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):  # pragma: no cover
        raise AssertionError("orchestrator turns must not use the streaming path")


def _setup_agents(client):
    assert _mk(client, name="helper", systemPrompt="You are helper.").status_code == 201
    assert (
        _mk(
            client,
            name="boss",
            systemPrompt="You coordinate specialists.",
            links=["helper"],
        ).status_code
        == 201
    )


def test_orchestrator_delegates_and_persists_final_answer(client):
    gw = _OrchestratorGateway()
    client.app.state.gateway = gw
    _setup_agents(client)

    sid = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}).json()[
        "id"
    ]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@boss solve 6*7 via your team", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"]["content"] == "The helper says: 42."

    # Three model calls: supervisor(delegate) -> helper(sub) -> supervisor(final).
    assert gw.calls == 3
    # The supervisor received the linked agent's answer back as a tool result.
    assert gw.delegated_task is not None
    assert "Helper computed 42." in gw.delegated_task

    # Final assistant message is persisted and attributed to the orchestrator.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "solve 6*7 via your team"  # mention stripped
    assert messages[1]["agent"] == "boss"

    # Usage from all three calls is metered to the single supervisor turn.
    summary = client.get("/api/usage").json()
    assert summary["totalRequests"] == 1
    assert summary["totalTokens"] == 21  # 3 calls * 7 tokens


def test_orchestrator_without_links_uses_direct_path(client):
    """An agent with neither tools nor links must NOT enter the delegation loop;
    it streams/answers via the direct single-call path."""
    gw = _OrchestratorGateway()
    client.app.state.gateway = gw
    assert _mk(client, name="solo", systemPrompt="You are solo.").status_code == 201

    sid = client.post("/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}).json()[
        "id"
    ]
    resp = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "@solo hello", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    # Single direct call (no delegate tool advertised, system != helper, no tool
    # result) -> the gateway returns its delegate tool_call shape, but the direct
    # path extracts content (None -> "") without running a tool loop.
    assert gw.calls == 1
