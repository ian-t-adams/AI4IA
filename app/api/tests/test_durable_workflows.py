"""Durable workflow execution: config gating, the API contract, and ownership.

Also pins the two upstream facts the design is built on, because both are
invisible in our own code and would fail confusingly if they ever changed:

* the Durable Task SDK's activity executor is SYNCHRONOUS, so activities must
  be ``def`` and bridge to the app's loop themselves; and
* PyPI's ``asyncio`` distribution (pulled in transitively by ``durabletask``)
  must not shadow the stdlib module.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest

from ai4ia_api.config import Settings
from ai4ia_api.usage.models import TokenUsage
from ai4ia_api.workflows.models import MAX_STEPS
from ai4ia_api.workflows.durable import (
    _MAX_STEP_TEXT_BYTES,
    _RUN_ID_SEPARATOR,
    _TRACE_BUDGET_BYTES,
    _TRUNCATION_MARKER,
    DurableRunStatus,
    DurableWorkflowService,
    _merge_usage,
    _truncate_for_payload,
    _usage_to_dict,
)


# --------------------------------------------------------------------------
# config fail-closed
# --------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base = dict(
        env="local",
        auth_provider="dev",
        allow_dev_auth=True,
        session_store="memory",
        model_gateway_url="http://gateway.test",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_disabled_by_default_validates():
    s = _settings()
    assert s.durable_workflows_enabled is False
    s.validate_runtime()  # no raise


def test_enabled_local_needs_no_scheduler():
    # Local dev may enable the flag without provisioning a paid scheduler; the
    # run endpoint then simply has no service and answers 422.
    _settings(durable_workflows_enabled=True).validate_runtime()


def test_enabled_deployed_without_endpoint_is_rejected():
    s = _settings(
        env="dev",
        durable_workflows_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://c.documents.azure.com",
        durable_task_hub_name="hub",
    )
    with pytest.raises(RuntimeError, match="AI4IA_DURABLE_TASK_ENDPOINT"):
        s.validate_runtime()


def test_enabled_deployed_without_hub_is_rejected():
    s = _settings(
        env="dev",
        durable_workflows_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://c.documents.azure.com",
        durable_task_endpoint="https://x.eastus2.durabletask.io",
    )
    with pytest.raises(RuntimeError, match="AI4IA_DURABLE_TASK_HUB_NAME"):
        s.validate_runtime()


def test_enabled_deployed_with_memory_store_is_rejected():
    # Durability without shared storage is theatre: the run would outlive the
    # request but write its result into a per-replica dict.
    s = _settings(
        env="dev",
        durable_workflows_enabled=True,
        session_store="memory",
        durable_task_endpoint="https://x.eastus2.durabletask.io",
        durable_task_hub_name="hub",
    )
    with pytest.raises(RuntimeError, match="[Cc]osmos"):
        s.validate_runtime()


def test_enabled_deployed_fully_configured_validates():
    _settings(
        env="dev",
        durable_workflows_enabled=True,
        session_store="cosmos",
        cosmos_endpoint="https://c.documents.azure.com",
        durable_task_endpoint="https://x.eastus2.durabletask.io",
        durable_task_hub_name="hub",
    ).validate_runtime()


# --------------------------------------------------------------------------
# API contract
# --------------------------------------------------------------------------


class _EchoGateway:
    async def complete(
        self, *, deployment, messages, params=None, correlation_id=None, api="chat"
    ):
        return {
            "choices": [{"message": {"content": f"echo:{messages[-1]['content']}"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }

    async def stream(self, **_kw):  # pragma: no cover - workflows never stream
        raise AssertionError("workflow runs must not stream")


class _StubDurable:
    """Stands in for DurableWorkflowService at the router boundary."""

    def __init__(self):
        self.scheduled: list[tuple[dict, str]] = []

    async def schedule(self, payload, *, user_id):
        self.scheduled.append((payload, user_id))
        return f"{user_id}{_RUN_ID_SEPARATOR}deadbeef"

    async def get_status(self, run_id, *, user_id):
        owner, _, remainder = run_id.partition(_RUN_ID_SEPARATOR)
        if not remainder or owner != user_id:
            return None
        return DurableRunStatus(runId=run_id, status="RUNNING")

    async def stop(self):
        return None


def _seed(client):
    client.app.state.gateway = _EchoGateway()
    for name in ("drafter", "editor"):
        assert (
            client.post(
                "/api/agents",
                json={"name": name, "systemPrompt": f"You are {name}."},
            ).status_code
            == 201
        )
    assert (
        client.post(
            "/api/workflows",
            json={
                "name": "summarize",
                "displayName": "Summarize",
                "steps": [
                    {"agent": "drafter", "instruction": "Draft about {input}"},
                    {"agent": "editor", "instruction": "Polish: {previous}"},
                ],
            },
        ).status_code
        == 201
    )
    return client.post(
        "/api/sessions", json={"title": "Chat", "model": "gpt-5.4"}
    ).json()["id"]


def test_durable_run_is_refused_when_the_feature_is_off(client):
    sid = _seed(client)
    client.app.state.durable_workflows = None

    resp = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "otters", "durable": True},
    )
    # 422, NOT a silent fall back to a synchronous run: the caller asked for a
    # guarantee we cannot give, so answering 200 would answer a different
    # question than was asked.
    assert resp.status_code == 422, resp.text
    assert "AI4IA_DURABLE_WORKFLOWS_ENABLED" in resp.text

    # No assistant message was produced by a sneaky in-request run.
    roles = [m["role"] for m in client.get(f"/api/sessions/{sid}/messages").json()]
    assert "assistant" not in roles


def test_durable_run_returns_202_and_does_not_run_in_request(client):
    sid = _seed(client)
    stub = _StubDurable()
    client.app.state.durable_workflows = stub

    resp = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "otters", "durable": True},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["sessionId"] == sid
    assert body["runId"]

    # Scheduled exactly once, carrying the resolved steps and the caller's id.
    assert len(stub.scheduled) == 1
    payload, user_id = stub.scheduled[0]
    assert [s["agent"] for s in payload["steps"]] == ["drafter", "editor"]
    assert payload["context"]["sessionId"] == sid
    assert payload["context"]["userId"] == user_id
    # The run id is derived from the id the ROUTER resolved, never from input.
    assert body["runId"].startswith(f"{user_id}{_RUN_ID_SEPARATOR}")

    # The user turn is persisted immediately so the caller sees their input;
    # the assistant reply is the orchestration's job, not this request's.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user"]


def test_non_durable_run_still_executes_synchronously(client):
    # The durable branch must not disturb the default path.
    sid = _seed(client)
    client.app.state.durable_workflows = _StubDurable()

    resp = client.post(
        "/api/workflows/summarize/run", json={"sessionId": sid, "input": "otters"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert client.app.state.durable_workflows.scheduled == []


def test_status_of_another_users_run_is_404(client):
    sid = _seed(client)
    client.app.state.durable_workflows = _StubDurable()
    mine = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "otters", "durable": True},
    ).json()["runId"]

    assert client.get(f"/api/workflows/runs/{mine}").status_code == 200
    # 404 rather than 403: distinguishing "not yours" from "does not exist"
    # would confirm other users' runs to anyone able to guess an id.
    assert (
        client.get(
            f"/api/workflows/runs/somebody-else{_RUN_ID_SEPARATOR}deadbeef"
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------
# ownership is enforced BEFORE the fetch
# --------------------------------------------------------------------------


class _ExplodingClient:
    def get_orchestration_state(self, *_a, **_kw):  # pragma: no cover
        raise AssertionError("must not reach the scheduler for a foreign run id")


class _CapturingClient:
    """Records what the SDK client was asked to schedule.

    Async because the real client is ``AsyncDurableTaskSchedulerClient`` (grpc.aio).
    """

    def __init__(self):
        self.instance_ids: list[str] = []

    async def schedule_new_orchestration(self, _name, *, input=None, instance_id=None):
        self.instance_ids.append(instance_id)
        return instance_id


@pytest.mark.anyio
async def test_schedule_generates_the_run_id_from_the_caller_id():
    """The run id must be minted here, never accepted from the request.

    Ownership is checked by parsing the id (see get_status), so an id the
    caller could choose would let them mint one bearing another user's prefix
    and then read that user's run back.
    """
    svc = DurableWorkflowService(endpoint="e", task_hub="h", app_state=object())
    captured = _CapturingClient()
    svc._client = captured

    run_id = await svc.schedule({"steps": []}, user_id="alice")

    assert captured.instance_ids == [run_id]
    owner, sep, remainder = run_id.partition(_RUN_ID_SEPARATOR)
    assert owner == "alice"
    assert sep and remainder, "run id must carry an owner prefix and a random part"
    # Two runs for the same user must not collide.
    second = await svc.schedule({"steps": []}, user_id="alice")
    assert second != run_id


@pytest.mark.anyio
async def test_get_status_rejects_a_foreign_run_without_calling_the_scheduler():
    svc = DurableWorkflowService(endpoint="e", task_hub="h", app_state=object())
    svc._client = _ExplodingClient()

    # A post-fetch filter would still leak existence via timing and would put
    # another user's payload in this process's memory. Check first.
    assert await svc.get_status(f"alice{_RUN_ID_SEPARATOR}abc", user_id="bob") is None
    # A malformed id (no separator) is not treated as owned-by-everyone.
    assert await svc.get_status("no-separator", user_id="no-separator") is None


# --------------------------------------------------------------------------
# usage ledger must agree with TokenUsage.add, not re-derive it
# --------------------------------------------------------------------------


def test_merge_usage_matches_token_usage_add():
    # `complete` is an AND over both sides plus a guard on the other side's
    # calls/known; `known` is an OR. Re-deriving those rules inline is how the
    # durable ledger would silently disagree with the in-request one.
    cases = [
        (TokenUsage(prompt=1, completion=2, total=3, known=True, complete=True, calls=1),
         TokenUsage(prompt=4, completion=5, total=9, known=True, complete=True, calls=1)),
        (TokenUsage(prompt=1, completion=2, total=3, known=True, complete=True, calls=1),
         TokenUsage(prompt=0, completion=0, total=0, known=False, complete=True, calls=1)),
        (TokenUsage(prompt=0, completion=0, total=0, known=False, complete=False, calls=0),
         TokenUsage(prompt=7, completion=8, total=15, known=True, complete=True, calls=2)),
    ]
    for left, right in cases:
        expected = left.add(right)  # add() RETURNS a new instance; it does not mutate

        merged = _merge_usage(_usage_to_dict(left), _usage_to_dict(right))
        assert merged == _usage_to_dict(expected), (left, right)


# --------------------------------------------------------------------------
# upstream constraints this design depends on
# --------------------------------------------------------------------------


def test_sdk_activity_executor_is_synchronous():
    """An ``async def`` activity is NOT awaited by the SDK.

    ``_ActivityExecutor.execute`` calls the function and serializes whatever it
    returns, so a coroutine raises at serialization time. This is why every
    activity in ``durable.py`` is a plain ``def`` that bridges onto the app's
    loop itself. If a future SDK gains native async support this test fails and
    the bridge can be simplified.
    """
    import inspect

    from durabletask import worker as dt_worker

    assert not inspect.iscoroutinefunction(dt_worker._ActivityExecutor.execute)
    src = inspect.getsource(dt_worker._ActivityExecutor.execute)
    assert "await fn(" not in src, "SDK now awaits activities; revisit the sync bridge"


def test_pypi_asyncio_shim_does_not_shadow_the_stdlib():
    """``durabletask`` declares a dependency on PyPI's ``asyncio``.

    That distribution is a metadata-only placeholder (the real 3.4.x backport
    would drop an ancient ``asyncio/`` into site-packages). Assert the name
    still resolves to the stdlib so a resolver change that pulls the real
    backport is caught here rather than in production.
    """
    assert asyncio.__file__ is not None
    stdlib = sys.prefix.lower(), getattr(sys, "base_prefix", sys.prefix).lower()
    resolved = asyncio.__file__.lower()
    assert "site-packages" not in resolved, resolved
    assert any(resolved.startswith(p) for p in stdlib) or "lib" in resolved


# --------------------------------------------------------------------------
# payload ceiling
#
# The Durable Task Scheduler rejects a JSON orchestration payload over 1 MB.
# The orchestrator's RETURN VALUE is the binding surface: ``previous`` is
# replaced each step, but ``trace`` accumulates every step's output. These
# tests drive the real orchestrator generator rather than a stub, because the
# truncation only helps if it sits on the path the SDK actually serializes.
# --------------------------------------------------------------------------


class _FakeOrchestrationContext:
    """Minimal ctx: the orchestrator only ever calls ``call_activity``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def call_activity(self, name, *, input=None):  # noqa: A002 - SDK's own kwarg
        self.calls.append((name, input))
        return ("task", name)


def _drive_orchestrator(payload, outcomes):
    """Run the real orchestrator to completion, feeding it activity results."""
    svc = DurableWorkflowService(
        endpoint="https://example.eastus2.durabletask.io",
        task_hub="hub",
        app_state=object(),
    )
    ctx = _FakeOrchestrationContext()
    generator = svc._build_orchestrator()(ctx, payload)
    pending = list(outcomes)
    try:
        generator.send(None)
        while True:
            generator.send(pending.pop(0) if pending else None)
    except StopIteration as stop:
        return stop.value, ctx


def _payload(step_count: int):
    return {
        "steps": [{"agent": f"agent-{i}", "prompt": "p"} for i in range(step_count)],
        "context": {"sessionId": "sess", "userId": "user"},
    }


def test_a_full_length_run_of_huge_steps_stays_under_the_payload_ceiling() -> None:
    """The whole point: MAX_STEPS x unbounded output must not exceed 1 MB.

    Without truncation this serializes to ~12 MB and the scheduler rejects the
    orchestration at the END of the run -- after every model call has already
    been paid for.
    """
    huge = "x" * 2_000_000
    outcomes = [{"result": {"text": huge}, "usage": {}} for _ in range(MAX_STEPS)]
    result, _ctx = _drive_orchestrator(_payload(MAX_STEPS), outcomes)

    serialized = len(json.dumps(result).encode("utf-8"))
    assert serialized < 1_000_000, f"orchestration output is {serialized} bytes"
    assert len(result["steps"]) == MAX_STEPS
    assert all(s["text"].endswith(_TRUNCATION_MARKER) for s in result["steps"])


def test_a_fatal_step_error_is_bounded_as_well_as_its_text() -> None:
    """A provider error body can be arbitrarily long and lands in the payload."""
    outcomes = [{"result": {"error": "e" * 2_000_000}, "usage": {}, "fatal": True}]
    result, _ctx = _drive_orchestrator(_payload(1), outcomes)

    assert len(json.dumps(result).encode("utf-8")) < 1_000_000
    assert result["ok"] is False
    assert result["text"].endswith(_TRUNCATION_MARKER)


def test_the_trace_budget_leaves_headroom_under_the_one_megabyte_ceiling() -> None:
    """A property, not a restatement of the arithmetic.

    Catches raising MAX_STEPS or the budget without re-deriving the per-step
    allowance -- either of which puts the payload back over the ceiling while
    every other test keeps passing.
    """
    assert MAX_STEPS * _MAX_STEP_TEXT_BYTES <= _TRACE_BUDGET_BYTES
    assert _TRACE_BUDGET_BYTES < 1_000_000


def test_text_within_the_budget_is_returned_unchanged() -> None:
    assert _truncate_for_payload("hello") == "hello"
    assert _truncate_for_payload("") == ""
    edge = "y" * _MAX_STEP_TEXT_BYTES
    assert _truncate_for_payload(edge) == edge


def test_truncation_never_splits_a_multibyte_character() -> None:
    """The cut is measured in BYTES, so it can land inside a UTF-8 sequence."""
    out = _truncate_for_payload("e\u0301\u00e9\u4e2d" * 200_000)

    assert out.endswith(_TRUNCATION_MARKER)
    assert len(out.encode("utf-8")) <= _MAX_STEP_TEXT_BYTES
    out.encode("utf-8")  # a split sequence would not round-trip
