"""Active retired selections refuse before chat or specialist side effects."""
from __future__ import annotations

import pytest

from ai4ia_api.agents.tool_exec import ToolContext
from ai4ia_api.agents.user_agents import UserAgent
from tests.test_chat_websearch_api import ScriptedWebGateway
from tests.test_orchestration import (
    DELEGATE_TOOL_NAME, _Gateway, _build, _catalog, _leaf, _orchestrator,
)

TOOL = "generate_video"


def legacy_selection(client, selection, tools):
    session = client.post("/api/sessions", json={
        "model": "gpt-5.2", "libraryDocumentIds": [],
    }).json()
    agent = UserAgent(
        id="legacy-specialist", name="legacy-specialist", displayName="Legacy specialist",
        userId=session["userId"], systemPrompt="Answer the question.", tools=tools,
    )
    client.portal.call(client.app.state.agent_service._store.put, agent)
    changes = (
        {"agentName": agent.name} if selection == "agent"
        else {"toolOverrides": {"added": tools}}
    )
    saved = client.portal.call(
        client.app.state.session_repo.patch_session, session["userId"], session["id"], changes,
    )
    return saved, agent


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("selection", ["agent", "conversation"])
@pytest.mark.parametrize("mode", ["active", "clean", "removed"])
def test_chat_retired_effective_selection_refuses_before_message_and_inference(
    client, stream, selection, mode,
):
    gateway = ScriptedWebGateway(call_tool=False)
    client.app.state.gateway = gateway
    session, agent = legacy_selection(
        client, selection, [TOOL, "calculator"] if mode != "clean" else ["calculator"],
    )
    if mode == "removed":
        repaired = client.patch(f"/api/sessions/{session.id}", json={
            "toolOverrides": (
                {"removed": [TOOL]} if selection == "agent" else {"added": ["calculator"]}
            ),
        })
        assert repaired.status_code == 200, repaired.text
        session = client.portal.call(
            client.app.state.session_repo.get_session, session.userId, session.id,
        )
    response = client.post("/api/chat", json={
        "sessionId": session.id, "content": "Answer a synthetic question.", "stream": stream,
        "allowAutomaticMemory": False,
    })
    messages = client.get(f"/api/sessions/{session.id}/messages").json()
    if mode == "active":
        assert response.status_code == 422, response.text
        assert "generate_video is retired" in response.json()["detail"]
        assert "agent editor" in response.json()["detail"]
        assert "conversation tool settings" in response.json()["detail"]
        assert gateway.calls == 0 and messages == []
    else:
        assert response.status_code == 200, response.text
        assert gateway.calls == 1
        assert [message["role"] for message in messages] == ["user", "assistant"]
        offered = {item["function"]["name"] for item in gateway.first_tools}
        assert TOOL not in offered and "calculator" in offered
    stored = client.portal.call(
        client.app.state.session_repo.get_session, session.userId, session.id,
    )
    assert stored.toolOverrides == session.toolOverrides
    assert client.portal.call(
        client.app.state.agent_service._store.get, agent.userId, agent.name,
    ) == agent


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("selection", ["agent", "conversation"])
@pytest.mark.parametrize("constraint", ["session", "request"])
@pytest.mark.parametrize("tools_enabled", [False, True])
def test_chat_retired_only_selection_is_inactive_when_tools_are_explicitly_off(
    client, stream, selection, constraint, tools_enabled,
):
    gateway = ScriptedWebGateway(call_tool=False)
    client.app.state.gateway = gateway
    session, agent = legacy_selection(client, selection, [TOOL])
    if constraint == "session":
        session = client.portal.call(
            client.app.state.session_repo.patch_session, session.userId, session.id,
            {"toolsEnabled": tools_enabled},
        )
    response = client.post("/api/chat", json={
        "sessionId": session.id, "content": "Answer a synthetic question.", "stream": stream,
        "allowTools": tools_enabled if constraint == "request" else True,
        "allowAutomaticMemory": False,
    })
    messages = client.get(f"/api/sessions/{session.id}/messages").json()
    if tools_enabled:
        assert response.status_code == 422, response.text
        assert "generate_video is retired" in response.json()["detail"]
        assert gateway.calls == 0 and messages == []
    else:
        assert response.status_code == 200, response.text
        assert gateway.calls == 1 and gateway.first_tools == []
        assert [message["role"] for message in messages] == ["user", "assistant"]
    stored = client.portal.call(
        client.app.state.session_repo.get_session, session.userId, session.id,
    )
    assert stored.toolsEnabled == session.toolsEnabled
    assert stored.toolOverrides == session.toolOverrides
    assert client.portal.call(
        client.app.state.agent_service._store.get, agent.userId, agent.name,
    ) == agent


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [TOOL, "calculator", "unavailable_custom_tool", "generate_image"])
async def test_retired_specialist_refuses_before_child_checkpoint_and_inference(tool):
    gateway = _Gateway()
    target = _leaf("specialist").model_copy(update={"tools": [tool]})
    _, handlers, sink = _build(_orchestrator(["specialist"]), _catalog(target), gateway)
    checkpoints = []

    async def checkpoint(snapshot):
        checkpoints.append(snapshot)
        return None, frozenset()

    result = await handlers[DELEGATE_TOOL_NAME](
        {"agent": target.name, "task": "Answer a synthetic question."},
        ToolContext(child_checkpoint=checkpoint),
    )
    if tool == TOOL:
        assert "generate_video is retired" in result["error"]
        assert "specialist agent" in result["error"] and "agent editor" in result["error"]
        assert gateway.calls == 0 and checkpoints == [] and sink == []
    else:
        assert result == {"agent": target.name, "answer": "sub answer"}
        assert gateway.calls == 1 and len(checkpoints) == 1 and len(sink) == 1
    assert target.tools == [tool]
