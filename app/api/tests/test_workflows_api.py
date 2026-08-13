"""End-to-end tests for workflow CRUD + execution through the API."""
from __future__ import annotations

_USAGE = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


class _EchoGateway:
    """Echoes the last user message with a stable usage payload."""

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        return {
            "choices": [{"message": {"content": f"echo:{messages[-1]['content']}"}}],
            "usage": _USAGE,
        }

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):  # pragma: no cover
        raise AssertionError("workflow runs must not stream")


def _mk_agent(client, name, headers=None):
    return client.post(
        "/api/agents",
        json={"name": name, "systemPrompt": f"You are {name}."},
        headers=headers or {},
    )


def _session(client, model="gpt-5.4", headers=None):
    return client.post(
        "/api/sessions", json={"title": "Chat", "model": model}, headers=headers or {}
    ).json()["id"]


def _wf_body(name="summarize", steps=None):
    if steps is None:
        steps = [
            {"agent": "drafter", "instruction": "Draft about {input}"},
            {"agent": "editor", "instruction": "Polish: {previous}"},
        ]
    return {"name": name, "displayName": "Summarize", "steps": steps}


def test_create_list_run_and_delete_workflow(client):
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201

    created = client.post("/api/workflows", json=_wf_body())
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "summarize"

    listed = client.get("/api/workflows").json()["workflows"]
    assert [w["name"] for w in listed] == ["summarize"]

    sid = _session(client)
    run = client.post(
        "/api/workflows/summarize/run", json={"sessionId": sid, "input": "otters"}
    )
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["ok"] is True
    # Two steps thread input -> previous: editor sees the drafter's echoed output.
    assert payload["message"]["content"] == "echo:Polish: echo:Draft about otters"
    assert payload["message"]["agent"] == "workflow:summarize"

    # Both user + assistant messages persisted, attributed to the workflow.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "otters"
    assert messages[0]["agent"] == "workflow:summarize"

    # Usage from both steps metered as one workflow run.
    summary = client.get("/api/usage").json()
    assert summary["totalRequests"] == 1
    assert summary["totalTokens"] == 14  # 2 steps * 7

    assert client.delete("/api/workflows/summarize").status_code == 204
    assert client.get("/api/workflows").json()["workflows"] == []


def test_run_unknown_workflow_404(client):
    client.app.state.gateway = _EchoGateway()
    sid = _session(client)
    resp = client.post(
        "/api/workflows/ghost/run", json={"sessionId": sid, "input": "hi"}
    )
    assert resp.status_code == 404


def test_run_missing_session_404(client):
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    resp = client.post(
        "/api/workflows/summarize/run", json={"sessionId": "nope", "input": "hi"}
    )
    assert resp.status_code == 404


def test_run_rejects_responses_model_422(client):
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client)
    resp = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "hi", "model": "gpt-5-pro"},
    )
    assert resp.status_code == 422
    assert "Responses API" in resp.json()["detail"]


def test_run_rejects_model_without_tool_calling(client):
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client)

    resp = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "hi", "model": "DeepSeek-V3.2"},
    )

    assert resp.status_code == 422
    assert "does not support tool calling" in resp.json()["detail"]
    assert client.get(f"/api/sessions/{sid}/messages").json() == []


def test_run_rejects_blank_and_overlong_input(client):
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client)
    assert (
        client.post(
            "/api/workflows/summarize/run", json={"sessionId": sid, "input": "   "}
        ).status_code
        == 422
    )
    huge = "x" * 9000
    assert (
        client.post(
            "/api/workflows/summarize/run", json={"sessionId": sid, "input": huge}
        ).status_code
        == 422
    )


def test_run_requires_a_model(client):
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client, model=None)  # session with no model, no override
    resp = client.post(
        "/api/workflows/summarize/run", json={"sessionId": sid, "input": "hi"}
    )
    assert resp.status_code == 400


def test_create_validation_and_conflict(client):
    # First step must reference {input}.
    bad = client.post(
        "/api/workflows",
        json=_wf_body(steps=[{"agent": "a1", "instruction": "no placeholder"}]),
    )
    assert bad.status_code == 422

    ok = client.post(
        "/api/workflows",
        json=_wf_body(name="flow", steps=[{"agent": "a1", "instruction": "{input}"}]),
    )
    assert ok.status_code == 201
    dup = client.post(
        "/api/workflows",
        json=_wf_body(name="flow", steps=[{"agent": "a1", "instruction": "{input}"}]),
    )
    assert dup.status_code == 409


def test_workflows_are_isolated_per_user(client):
    alice = {"X-Dev-User": "alice"}
    bob = {"X-Dev-User": "bob"}
    body = _wf_body(name="mine", steps=[{"agent": "a1", "instruction": "{input}"}])
    assert client.post("/api/workflows", json=body, headers=alice).status_code == 201
    # Bob sees none of alice's workflows, and can reuse the same name.
    assert client.get("/api/workflows", headers=bob).json()["workflows"] == []
    assert client.post("/api/workflows", json=body, headers=bob).status_code == 201
    assert (
        client.post(
            "/api/workflows/mine/run",
            json={"sessionId": "x", "input": "hi"},
            headers=bob,
        ).status_code
        == 404  # bob has the workflow but not that session -> 404 session
    )
    # Alice still has exactly her one workflow.
    assert len(client.get("/api/workflows", headers=alice).json()["workflows"]) == 1


def test_update_cannot_modify_another_users_workflow(client):
    alice = {"X-Dev-User": "alice"}
    bob = {"X-Dev-User": "bob"}
    body = _wf_body(name="mine", steps=[{"agent": "a1", "instruction": "{input}"}])
    assert client.post("/api/workflows", json=body, headers=alice).status_code == 201
    # Bob's own partition has no "mine" -> 404, not a leak/edit of alice's.
    resp = client.put(
        "/api/workflows/mine",
        json={"displayName": "Hijacked"},
        headers=bob,
    )
    assert resp.status_code == 404, resp.text
    mine = client.get("/api/workflows", headers=alice).json()["workflows"]
    assert mine[0]["displayName"] == "Summarize"


def test_delete_cannot_remove_another_users_workflow(client):
    alice = {"X-Dev-User": "alice"}
    bob = {"X-Dev-User": "bob"}
    body = _wf_body(name="mine", steps=[{"agent": "a1", "instruction": "{input}"}])
    assert client.post("/api/workflows", json=body, headers=alice).status_code == 201
    # Idempotent no-op in bob's own (empty) partition -> 204, but alice's
    # workflow must survive untouched.
    resp = client.delete("/api/workflows/mine", headers=bob)
    assert resp.status_code == 204, resp.text
    assert [w["name"] for w in client.get("/api/workflows", headers=alice).json()["workflows"]] == [
        "mine"
    ]


def test_run_unknown_step_agent_persists_failure(client):
    """A workflow whose step references a non-existent agent runs, fails gracefully
    (ok=False), persists an assistant message, but meters nothing (no model call)."""
    client.app.state.gateway = _EchoGateway()
    assert (
        client.post(
            "/api/workflows",
            json=_wf_body(
                name="broken", steps=[{"agent": "ghost", "instruction": "{input}"}]
            ),
        ).status_code
        == 201
    )
    sid = _session(client)
    resp = client.post(
        "/api/workflows/broken/run", json={"sessionId": sid, "input": "hi"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "ghost" in resp.json()["message"]["content"]
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    # A zero-model-call failure must not consume a request slot or meter usage.
    assert client.get("/api/usage").json()["totalRequests"] == 0


def test_pre_run_guards_persist_nothing(client):
    """A 422 (Responses model) refused BEFORE the run must leave no messages and
    no usage — the user turn is only persisted once the run is actually allowed."""
    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client)
    resp = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "hi", "model": "gpt-5-pro"},
    )
    assert resp.status_code == 422
    assert client.get(f"/api/sessions/{sid}/messages").json() == []
    assert client.get("/api/usage").json()["totalRequests"] == 0


class _FailSecondCallGateway:
    """Succeeds on the first model call (with usage), raises on the second."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):
        self.calls += 1
        if self.calls >= 2:
            raise RuntimeError("boom")
        return {"choices": [{"message": {"content": "drafted"}}], "usage": _USAGE}

    async def stream(self, *, deployment, messages, params=None, correlation_id=None, api="chat"):  # pragma: no cover
        raise AssertionError


def test_partial_failure_persists_and_meters_consumed_usage(client):
    """Step 1 consumes tokens, step 2 fails: the run returns ok=False, persists an
    assistant failure message, and meters the usage actually consumed by step 1."""
    client.app.state.gateway = _FailSecondCallGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client)
    resp = client.post(
        "/api/workflows/summarize/run", json={"sessionId": sid, "input": "otters"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    # One request metered, reflecting only step 1's consumed tokens.
    summary = client.get("/api/usage").json()
    assert summary["totalRequests"] == 1
    assert summary["totalTokens"] == 7



def test_workflow_list_advertises_durable_availability_from_live_state(client):
    """The list's durableAvailable must agree with what a run would actually do.

    A client can only opt into durable execution if it knows the deployment
    supports it, and the only honest source for that is the same
    ``app.state.durable_workflows`` the run endpoint checks. A separately-plumbed
    web-side flag can drift from the API's real posture, which is how the feature
    shipped with a provisioned scheduler and no caller: nothing disagreed loudly
    enough to notice.
    """
    # Off: the field is present and false, and a durable run is refused. Asserting
    # both together is the point -- either alone would pass while the advertisement
    # and the behaviour disagreed.
    client.app.state.durable_workflows = None
    assert client.get("/api/workflows").json()["durableAvailable"] is False

    client.app.state.gateway = _EchoGateway()
    assert _mk_agent(client, "drafter").status_code == 201
    assert _mk_agent(client, "editor").status_code == 201
    assert client.post("/api/workflows", json=_wf_body()).status_code == 201
    sid = _session(client)
    refused = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "otters", "durable": True},
    )
    assert refused.status_code == 422

    # On: flipping the same attribute the run endpoint reads flips the
    # advertisement, so the two cannot disagree by construction.
    client.app.state.durable_workflows = object()
    assert client.get("/api/workflows").json()["durableAvailable"] is True
