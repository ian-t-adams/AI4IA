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
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from ai4ia_api.config import Settings
from ai4ia_api.catalog import DeploymentOption
from ai4ia_api.routers.workflows import (
    _DURABLE_SCHEDULING_LEASE_SECONDS,
    _claim_durable_run,
)
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Message, MessageRole, MessageStatus, Session
from ai4ia_api.usage.models import TokenUsage
from ai4ia_api.workflows.models import MAX_STEPS
from ai4ia_api.workflows.durable import (
    _MAX_STEP_TEXT_BYTES,
    _RUN_ID_SEPARATOR,
    _TRACE_BUDGET_BYTES,
    _TRUNCATION_MARKER,
    DurableRunStatus,
    DurableScheduleAcceptanceUnknownError,
    DurableScheduleRejectedError,
    DurableWorkflowService,
    _merge_usage,
    _step_from_dict,
    _truncate_for_payload,
    _usage_to_dict,
    build_orchestration_payload,
    durable_message_ids,
    durable_run_fingerprint,
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
        model_gateway_url="https://proxy.test/openai",
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="proxy-secret",
        model_gateway_api_key_header="S7P-KEY",
        model_gateway_allowed_hosts="proxy.test",
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

    async def schedule(self, payload, *, user_id, run_id=None):
        self.scheduled.append((payload, user_id))
        return run_id or f"{user_id}{_RUN_ID_SEPARATOR}deadbeef"

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
        json={
            "sessionId": sid,
            "input": "otters",
            "durable": True,
            "idempotencyKey": "feature-off-key",
        },
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
        json={
            "sessionId": sid,
            "input": "otters",
            "durable": True,
            "idempotencyKey": "accepted-run-key",
        },
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

    # The user turn and deterministic pending assistant are persisted before the
    # scheduler call, so a retry can reuse both without duplicating either.
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "streaming"
    assert messages[1]["workflowRunStatus"] == "accepted"


def test_durable_run_requires_a_caller_key_before_any_persistence(client):
    sid = _seed(client)
    stub = _StubDurable()
    client.app.state.durable_workflows = stub
    resp = client.post(
        "/api/workflows/summarize/run",
        json={"sessionId": sid, "input": "otters", "durable": True},
    )
    assert resp.status_code == 422
    assert "idempotencyKey" in resp.text
    assert stub.scheduled == []
    assert client.get(f"/api/sessions/{sid}/messages").json() == []


class _DefiniteFailureDurable(_StubDurable):
    async def schedule(self, payload, *, user_id, run_id=None):
        self.scheduled.append((payload, user_id))
        raise DurableScheduleRejectedError("permission denied")


class _AmbiguousThenAcceptedDurable(_StubDurable):
    async def schedule(self, payload, *, user_id, run_id=None):
        self.scheduled.append((payload, user_id))
        if len(self.scheduled) == 1:
            raise DurableScheduleAcceptanceUnknownError("transport unknown")
        return run_id


def test_definite_schedule_failure_persists_one_terminal_failed_run(client):
    sid = _seed(client)
    stub = _DefiniteFailureDurable()
    client.app.state.durable_workflows = stub
    body = {
        "sessionId": sid,
        "input": "otters",
        "durable": True,
        "idempotencyKey": "definite-failure-1",
    }

    first = client.post("/api/workflows/summarize/run", json=body)
    second = client.post("/api/workflows/summarize/run", json=body)
    assert first.status_code == second.status_code == 503
    assert len(stub.scheduled) == 1
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert len(messages) == 2
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["status"] == "error"
    assert messages[1]["workflowRunStatus"] == "schedule_failed"


def test_ambiguous_schedule_retry_reuses_messages_and_run(client):
    sid = _seed(client)
    stub = _AmbiguousThenAcceptedDurable()
    client.app.state.durable_workflows = stub
    body = {
        "sessionId": sid,
        "input": "otters",
        "durable": True,
        "idempotencyKey": "ambiguous-retry-1",
    }

    first = client.post("/api/workflows/summarize/run", json=body)
    assert first.status_code == 202
    assert first.json()["status"] == "acceptance_unknown"
    second = client.post("/api/workflows/summarize/run", json=body)
    assert second.status_code == 202
    assert second.json()["status"] == "accepted"
    assert second.json()["runId"] == first.json()["runId"]
    third = client.post("/api/workflows/summarize/run", json=body)
    assert third.status_code == 202
    assert len(stub.scheduled) == 2
    assert len(
        {scheduled_payload["context"]["runId"] for scheduled_payload, _ in stub.scheduled}
    ) == 1
    messages = client.get(f"/api/sessions/{sid}/messages").json()
    assert len(messages) == 2
    assert len({message["id"] for message in messages}) == 2
    assert messages[1]["workflowRunStatus"] == "accepted"


def test_same_key_with_different_execution_input_is_rejected(client):
    sid = _seed(client)
    stub = _AmbiguousThenAcceptedDurable()
    client.app.state.durable_workflows = stub
    body = {
        "sessionId": sid,
        "input": "otters",
        "durable": True,
        "idempotencyKey": "fingerprint-key",
    }
    assert client.post("/api/workflows/summarize/run", json=body).status_code == 202
    conflict = client.post(
        "/api/workflows/summarize/run",
        json={**body, "input": "badgers"},
    )
    assert conflict.status_code == 409
    assert len(stub.scheduled) == 1
    assert len(client.get(f"/api/sessions/{sid}/messages").json()) == 2


def test_same_idempotency_key_is_scoped_to_one_session_and_workflow(client):
    first_session = _seed(client)
    second_session = client.post(
        "/api/sessions", json={"title": "Other", "model": "gpt-5.4"}
    ).json()["id"]
    stub = _StubDurable()
    client.app.state.durable_workflows = stub
    base = {
        "input": "otters",
        "durable": True,
        "idempotencyKey": "session-scoped-key",
    }

    first = client.post(
        "/api/workflows/summarize/run",
        json={**base, "sessionId": first_session},
    )
    second = client.post(
        "/api/workflows/summarize/run",
        json={**base, "sessionId": second_session},
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["runId"] != second.json()["runId"]
    assert len(stub.scheduled) == 2
    assert len(client.get(f"/api/sessions/{first_session}/messages").json()) == 2
    assert len(client.get(f"/api/sessions/{second_session}/messages").json()) == 2


def test_retry_cannot_overwrite_a_terminal_result_that_lands_after_its_read(client):
    sid = _seed(client)
    inner = client.app.state.session_repo

    class CompleteDuringRetry:
        def __init__(self):
            self.armed = False

        def __getattr__(self, name):
            return getattr(inner, name)

        async def list_messages(self, user_id, session_id):
            snapshot = await inner.list_messages(user_id, session_id)
            if self.armed:
                pending = next(
                    (
                        message
                        for message in snapshot
                        if message.role.value == "assistant"
                        and message.workflowRunStatus
                        in {"pending", "acceptance_unknown"}
                    ),
                    None,
                )
                if pending is not None:
                    terminal = pending.model_copy(
                        update={
                            "content": "finished",
                            "status": MessageStatus.complete,
                            "workflowRunStatus": "completed",
                        }
                    )
                    await inner.upsert_message(user_id, terminal)
            return snapshot

    repo = CompleteDuringRetry()
    client.app.state.session_repo = repo
    stub = _AmbiguousThenAcceptedDurable()
    client.app.state.durable_workflows = stub
    body = {
        "sessionId": sid,
        "input": "otters",
        "durable": True,
        "idempotencyKey": "terminal-race-key",
    }
    assert (
        client.post("/api/workflows/summarize/run", json=body).json()["status"]
        == "acceptance_unknown"
    )
    repo.armed = True
    assert client.post("/api/workflows/summarize/run", json=body).status_code == 202
    assistant = client.get(f"/api/sessions/{sid}/messages").json()[1]
    assert assistant["content"] == "finished"
    assert assistant["status"] == "complete"
    assert assistant["workflowRunStatus"] == "completed"


def test_run_failure_replay_is_accepted_not_reported_as_schedule_failure(client):
    sid = _seed(client)
    inner = client.app.state.session_repo

    class FailBeforeReplayRead:
        def __init__(self):
            self.armed = False

        def __getattr__(self, name):
            return getattr(inner, name)

        async def list_messages(self, user_id, session_id):
            messages = await inner.list_messages(user_id, session_id)
            if self.armed:
                assistant = next(
                    message
                    for message in messages
                    if message.role.value == "assistant"
                )
                assistant.status = MessageStatus.error
                assistant.workflowRunStatus = "run_failed"
                assistant.content = "step failed"
            return messages

    repo = FailBeforeReplayRead()
    client.app.state.session_repo = repo
    stub = _AmbiguousThenAcceptedDurable()
    client.app.state.durable_workflows = stub
    body = {
        "sessionId": sid,
        "input": "otters",
        "durable": True,
        "idempotencyKey": "run-failure-replay",
    }
    assert client.post("/api/workflows/summarize/run", json=body).status_code == 202
    repo.armed = True
    replay = client.post("/api/workflows/summarize/run", json=body)
    assert replay.status_code == 202
    assert replay.json()["status"] == "accepted"
    assert len(stub.scheduled) == 1


def _claim_messages(
    session_id: str,
    *,
    fingerprint: str,
    content: str = "otters",
) -> tuple[Message, Message]:
    run_id = "u1:stable"
    user_id, assistant_id = durable_message_ids(run_id)
    return (
        Message(
            id=user_id,
            sessionId=session_id,
            userId="u1",
            role=MessageRole.user,
            content=content,
            workflowRunId=run_id,
            workflowRunStatus="accepted",
            workflowRunFingerprint=fingerprint,
        ),
        Message(
            id=assistant_id,
            sessionId=session_id,
            userId="u1",
            role=MessageRole.assistant,
            status=MessageStatus.streaming,
            workflowRunId=run_id,
            workflowRunStatus="pending",
            workflowRunFingerprint=fingerprint,
        ),
    )


class _InitialReadBarrierRepository:
    """Force two claims to read absence before either may create."""

    def __init__(self, inner: InMemorySessionRepository) -> None:
        self.inner = inner
        self.initial_reads = 0
        self.both_read = asyncio.Event()
        self.winner_finished = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def list_messages(self, user_id: str, session_id: str) -> list[Message]:
        snapshot = await self.inner.list_messages(user_id, session_id)
        if self.initial_reads < 2:
            self.initial_reads += 1
            if self.initial_reads == 2:
                self.both_read.set()
            await self.both_read.wait()
        return snapshot

    async def claim_workflow_run_if_absent(
        self,
        user_id: str,
        user_message: Message,
        pending_assistant: Message,
    ) -> bool:
        created = await self.inner.claim_workflow_run_if_absent(
            user_id, user_message, pending_assistant
        )
        if not created:
            await self.winner_finished.wait()
        return created


class _FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 10, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance_past_lease(self) -> None:
        self.current += timedelta(
            seconds=_DURABLE_SCHEDULING_LEASE_SECONDS + 1
        )


@pytest.mark.anyio
async def test_concurrent_identical_claim_schedules_once_and_preserves_terminal():
    inner = InMemorySessionRepository()
    session = await inner.create_session(Session(userId="u1"))
    repo = _InitialReadBarrierRepository(inner)
    schedules = 0

    async def request():
        nonlocal schedules
        user_message, assistant = _claim_messages(
            session.id, fingerprint="a" * 64
        )
        owns, current = await _claim_durable_run(
            repo,
            user_id="u1",
            user_message=user_message,
            pending_assistant=assistant,
        )
        if owns:
            schedules += 1
            terminal = current.model_copy(
                update={
                    "content": "finished",
                    "status": MessageStatus.complete,
                    "workflowRunStatus": "completed",
                }
            )
            await repo.upsert_message("u1", terminal)
            repo.winner_finished.set()
        return owns

    owners = await asyncio.gather(request(), request())
    assert sorted(owners) == [False, True]
    assert repo.initial_reads == 2
    assert schedules == 1
    messages = await repo.list_messages("u1", session.id)
    assistant = next(m for m in messages if m.role is MessageRole.assistant)
    assert assistant.content == "finished"
    assert assistant.workflowRunStatus == "completed"


@pytest.mark.anyio
async def test_concurrent_different_fingerprint_has_one_owner_and_one_conflict():
    inner = InMemorySessionRepository()
    session = await inner.create_session(Session(userId="u1"))
    repo = _InitialReadBarrierRepository(inner)
    schedules = 0

    async def request(fingerprint: str, content: str):
        nonlocal schedules
        user_message, assistant = _claim_messages(
            session.id, fingerprint=fingerprint, content=content
        )
        owns, _current = await _claim_durable_run(
            repo,
            user_id="u1",
            user_message=user_message,
            pending_assistant=assistant,
        )
        if owns:
            schedules += 1
            repo.winner_finished.set()
        return owns

    outcomes = await asyncio.gather(
        request("a" * 64, "otters"),
        request("b" * 64, "badgers"),
        return_exceptions=True,
    )
    assert sum(outcome is True for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert repo.initial_reads == 2
    assert schedules == 1
    user = next(
        message
        for message in await repo.list_messages("u1", session.id)
        if message.role is MessageRole.user
    )
    winner_index = outcomes.index(True)
    assert user.content == ("otters" if winner_index == 0 else "badgers")
    assert user.workflowRunFingerprint == ("a" * 64 if winner_index == 0 else "b" * 64)


@pytest.mark.anyio
async def test_crashed_claim_is_pending_and_preexpiry_retry_does_not_reschedule():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1"))
    clock = _FakeClock()
    user_message, assistant = _claim_messages(
        session.id, fingerprint="a" * 64
    )

    owns, claimed = await _claim_durable_run(
        repo,
        user_id="u1",
        user_message=user_message,
        pending_assistant=assistant,
        now=clock,
        lease_token_factory=lambda: "lease-first",
    )
    assert owns is True
    assert claimed.workflowScheduleLeaseToken == "lease-first"
    assert claimed.workflowScheduleLeaseExpiresAt == clock.current + timedelta(
        seconds=_DURABLE_SCHEDULING_LEASE_SECONDS
    )

    # Simulate process death here: the owner never calls the scheduler.
    replay_owns, replay = await _claim_durable_run(
        repo,
        user_id="u1",
        user_message=user_message,
        pending_assistant=assistant,
        now=clock,
        lease_token_factory=lambda: "must-not-be-used",
    )
    assert replay_owns is False
    assert replay.workflowRunStatus == "pending"
    assert replay.workflowScheduleLeaseToken == "lease-first"


def test_crash_then_http_retry_gets_timing_and_recovers_after_lease(
    client, monkeypatch
):
    sid = _seed(client)
    clock = _FakeClock()
    monkeypatch.setattr("ai4ia_api.routers.workflows._utc_now", clock)

    class CrashBeforeDtsAcceptance(_StubDurable):
        async def schedule(self, payload, *, user_id, run_id=None):
            raise RuntimeError("process died before DTS accepted the run")

    body = {
        "sessionId": sid,
        "input": "otters",
        "durable": True,
        "idempotencyKey": "crash-recovery-key",
    }
    client.app.state.durable_workflows = CrashBeforeDtsAcceptance()
    with pytest.raises(RuntimeError, match="process died"):
        client.post("/api/workflows/summarize/run", json=body)

    recovered = _StubDurable()
    client.app.state.durable_workflows = recovered
    pending = client.post("/api/workflows/summarize/run", json=body)
    assert pending.status_code == 202
    assert pending.json()["status"] == "pending"
    assert pending.json()["retryAfterSeconds"] == _DURABLE_SCHEDULING_LEASE_SECONDS
    assert pending.headers["Retry-After"] == str(
        _DURABLE_SCHEDULING_LEASE_SECONDS
    )
    assert pending.json()["leaseExpiresAt"] is not None
    assert recovered.scheduled == []

    clock.advance_past_lease()
    accepted = client.post("/api/workflows/summarize/run", json=body)
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["runId"] == pending.json()["runId"]
    assert len(recovered.scheduled) == 1
    assert recovered.scheduled[0][0]["context"]["runId"] == pending.json()["runId"]


@pytest.mark.anyio
async def test_expired_crash_lease_has_one_cas_winner_and_makes_progress():
    inner = InMemorySessionRepository()
    session = await inner.create_session(Session(userId="u1"))
    clock = _FakeClock()
    user_message, assistant = _claim_messages(
        session.id, fingerprint="a" * 64
    )
    owns, _ = await _claim_durable_run(
        inner,
        user_id="u1",
        user_message=user_message,
        pending_assistant=assistant,
        now=clock,
        lease_token_factory=lambda: "lease-crashed",
    )
    assert owns is True
    clock.advance_past_lease()
    repo = _InitialReadBarrierRepository(inner)
    scheduled_run_ids: list[str] = []

    async def retry(token: str) -> tuple[bool, Message]:
        return await _claim_durable_run(
            repo,
            user_id="u1",
            user_message=user_message,
            pending_assistant=assistant,
            now=clock,
            lease_token_factory=lambda: token,
        )

    results = await asyncio.gather(retry("lease-a"), retry("lease-b"))
    owners = [owns for owns, _current in results]
    winner = next(current for owns, current in results if owns)
    scheduled_run_ids.append(winner.workflowRunId or "")
    accepted = winner.model_copy(
        update={
            "workflowRunStatus": "accepted",
            "workflowScheduleLeaseToken": None,
            "workflowScheduleLeaseExpiresAt": None,
        }
    )
    assert await repo.replace_message_if_workflow_status(
        "u1",
        accepted,
        expected_status="pending",
        expected_lease_token=winner.workflowScheduleLeaseToken,
    )

    assert sorted(owners) == [False, True]
    assert scheduled_run_ids == ["u1:stable"]
    saved = next(
        message
        for message in await inner.list_messages("u1", session.id)
        if message.role is MessageRole.assistant
    )
    assert saved.workflowRunStatus == "accepted"
    assert saved.workflowScheduleLeaseToken is None


@pytest.mark.anyio
async def test_crashed_claim_still_rejects_different_fingerprint():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1"))
    clock = _FakeClock()
    user_message, assistant = _claim_messages(
        session.id, fingerprint="a" * 64
    )
    assert (
        await _claim_durable_run(
            repo,
            user_id="u1",
            user_message=user_message,
            pending_assistant=assistant,
            now=clock,
            lease_token_factory=lambda: "lease-first",
        )
    )[0]
    other_user, other_assistant = _claim_messages(
        session.id, fingerprint="b" * 64, content="badgers"
    )

    with pytest.raises(HTTPException) as exc:
        await _claim_durable_run(
            repo,
            user_id="u1",
            user_message=other_user,
            pending_assistant=other_assistant,
            now=clock,
            lease_token_factory=lambda: "lease-other",
        )

    assert exc.value.status_code == 409


@pytest.mark.anyio
async def test_expired_lease_never_reclaims_terminal_assistant():
    repo = InMemorySessionRepository()
    session = await repo.create_session(Session(userId="u1"))
    clock = _FakeClock()
    user_message, assistant = _claim_messages(
        session.id, fingerprint="a" * 64
    )
    _, claimed = await _claim_durable_run(
        repo,
        user_id="u1",
        user_message=user_message,
        pending_assistant=assistant,
        now=clock,
        lease_token_factory=lambda: "lease-first",
    )
    terminal = claimed.model_copy(
        update={
            "content": "finished",
            "status": MessageStatus.complete,
            "workflowRunStatus": "completed",
        }
    )
    await repo.upsert_message("u1", terminal)
    clock.advance_past_lease()

    owns, current = await _claim_durable_run(
        repo,
        user_id="u1",
        user_message=user_message,
        pending_assistant=assistant,
        now=clock,
        lease_token_factory=lambda: "must-not-be-used",
    )

    assert owns is False
    assert current.workflowRunStatus == "completed"
    assert current.content == "finished"
    assert current.workflowScheduleLeaseToken == "lease-first"


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
        json={
            "sessionId": sid,
            "input": "otters",
            "durable": True,
            "idempotencyKey": "owned-run-key",
        },
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


class _AmbiguousClient:
    def __init__(self, state=None):
        self.state = state

    async def schedule_new_orchestration(self, _name, *, input=None, instance_id=None):
        raise TimeoutError("response lost")

    async def get_orchestration_state(self, _run_id):
        return self.state


class _Code:
    def __init__(self, name: str):
        self.name = name


class _SchedulerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self._code = _Code(code)

    def code(self):
        return self._code


class _RejectedClient:
    def __init__(self, code: str):
        self.code = code
        self.reads = 0

    async def schedule_new_orchestration(self, _name, *, input=None, instance_id=None):
        raise _SchedulerError(self.code)

    async def get_orchestration_state(self, _run_id):
        self.reads += 1
        raise AssertionError("definite rejections must not be point-read as ambiguous")


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
async def test_schedule_recovers_when_point_read_proves_ambiguous_acceptance():
    svc = DurableWorkflowService(endpoint="e", task_hub="h", app_state=object())
    svc._client = _AmbiguousClient(state=object())
    run_id = f"alice{_RUN_ID_SEPARATOR}stable"
    assert (
        await svc.schedule({"steps": []}, user_id="alice", run_id=run_id)
        == run_id
    )


@pytest.mark.anyio
async def test_schedule_preserves_unknown_acceptance_for_same_id_retry():
    svc = DurableWorkflowService(endpoint="e", task_hub="h", app_state=object())
    svc._client = _AmbiguousClient()
    run_id = f"alice{_RUN_ID_SEPARATOR}stable"
    with pytest.raises(DurableScheduleAcceptanceUnknownError):
        await svc.schedule({"steps": []}, user_id="alice", run_id=run_id)


@pytest.mark.anyio
async def test_schedule_classifies_permission_rejection_as_definite():
    svc = DurableWorkflowService(endpoint="e", task_hub="h", app_state=object())
    client = _RejectedClient("PERMISSION_DENIED")
    svc._client = client
    run_id = f"alice{_RUN_ID_SEPARATOR}stable"
    with pytest.raises(DurableScheduleRejectedError):
        await svc.schedule({"steps": []}, user_id="alice", run_id=run_id)
    assert client.reads == 0


@pytest.mark.anyio
async def test_schedule_treats_already_existing_instance_as_idempotent_success():
    svc = DurableWorkflowService(endpoint="e", task_hub="h", app_state=object())
    client = _RejectedClient("ALREADY_EXISTS")
    svc._client = client
    run_id = f"alice{_RUN_ID_SEPARATOR}stable"
    assert (
        await svc.schedule({"steps": []}, user_id="alice", run_id=run_id)
        == run_id
    )
    assert client.reads == 0


def test_run_fingerprint_covers_every_frozen_execution_field():
    base = {
        "steps": [{"agent": "writer", "instruction": "Write {input}", "extraTools": []}],
        "context": {
            "userId": "u1",
            "sessionId": "s1",
            "workflowName": "write",
            "runInput": "otters",
            "modelId": "gpt-x",
            "deployment": "gpt-x-east",
            "usageTarget": {
                "provider": "azure_openai",
                "deployment": "gpt-x-east",
                "target": "gpt-x-east",
                "region": "eastus2",
                "dataZone": "US",
            },
            "email": "user@example.com",
            "libraryDocumentIds": ["doc-1"],
            "correlationId": "cid-1",
            "runId": "u1:run",
            "assistantMessageId": "a1",
        },
    }
    original = durable_run_fingerprint(base)
    mutations = (
        ("steps", [{"agent": "writer", "instruction": "Edit {input}", "extraTools": []}]),
        ("runInput", "badgers"),
        ("modelId", "gpt-y"),
        ("deployment", "gpt-x-west"),
        ("email", "other@example.com"),
        ("libraryDocumentIds", ["doc-2"]),
        ("usageTarget", {**base["context"]["usageTarget"], "region": "westus"}),
        ("usageTarget", {**base["context"]["usageTarget"], "dataZone": "EU"}),
    )
    for key, value in mutations:
        changed = copy.deepcopy(base)
        if key == "steps":
            changed["steps"] = value
        else:
            changed["context"][key] = value
        assert durable_run_fingerprint(changed) != original, key

    metadata_only = copy.deepcopy(base)
    metadata_only["context"].update(
        {
            "assistantMessageId": "a2",
            "correlationId": "cid-2",
            "runFingerprint": "ignored",
            "runId": "u1:other",
        }
    )
    assert durable_run_fingerprint(metadata_only) == original


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
        task = generator.send(None)
        while True:
            task = generator.send(
                pending.pop(0) if pending and task[1] == "ai4ia_workflow_step" else None
            )
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


def test_new_workflows_checkpoint_completed_step_receipts_and_bound_escaped_json() -> None:
    from ai4ia_api.receipts import ReceiptToolCall, build_receipt, json_payload

    receipt = build_receipt(calls=[
        ReceiptToolCall(
            tool="calculator", outcome="result",
            arguments=json_payload({"value": "汉字\"\n" * 1000}),
            result=json_payload({"value": "汉字\"\n" * 1000}),
        )
        for _ in range(8)
    ])
    payload = _payload(MAX_STEPS)
    payload["context"]["governanceVersion"] = 2
    outcomes = [{
        "result": {
            "ok": True, "text": "汉字\"\\\n" * 100_000,
            "receipt": receipt.model_dump(mode="json"),
        },
        "usage": {},
    } for _ in range(MAX_STEPS)]
    result, context = _drive_orchestrator(payload, outcomes)
    persisted = [
        value for name, value in context.calls if name == "ai4ia_workflow_persist"
    ]
    assert len(persisted) == MAX_STEPS
    assert len(persisted[0]["steps"]) == 1
    assert persisted[0]["checkpoint"] is True
    assert persisted[0]["steps"][0]["receipt"]["toolCallCount"] == 8
    assert all(len(json.dumps(value).encode("ascii")) < 1_000_000 for value in persisted)
    assert len(json.dumps(result).encode("ascii")) < 1_000_000


def test_durable_display_bound_does_not_change_next_steps_unicode_context():
    from ai4ia_api.workflows.runner import MAX_CARRY_LEN

    original = "\U0001f600" * (MAX_CARRY_LEN + 5000)
    payload = _payload(2)
    payload["context"]["governanceVersion"] = 2
    result, context = _drive_orchestrator(payload, [
        {"result": {"ok": True, "text": original}, "usage": {}},
        {"result": {"ok": True, "text": "final output"}, "usage": {}},
    ])
    steps = [value for name, value in context.calls if name == "ai4ia_workflow_step"]
    assert steps[1]["previous"] == original[:MAX_CARRY_LEN]
    assert result["text"] == "final output"
    assert result["steps"][0]["text"].endswith(_TRUNCATION_MARKER)


def test_text_within_the_budget_is_returned_unchanged() -> None:
    assert _truncate_for_payload("hello") == "hello"
    assert _truncate_for_payload("") == ""
    edge = "y" * (_MAX_STEP_TEXT_BYTES - 2)  # JSON string's surrounding quotes
    assert _truncate_for_payload(edge) == edge


def test_truncation_never_splits_a_multibyte_character() -> None:
    """The cut is measured in BYTES, so it can land inside a UTF-8 sequence."""
    out = _truncate_for_payload("e\u0301\u00e9\u4e2d" * 200_000)

    assert out.endswith(_TRUNCATION_MARKER)
    assert len(out.encode("utf-8")) <= _MAX_STEP_TEXT_BYTES
    out.encode("utf-8")  # a split sequence would not round-trip


# --------------------------------------------------------------------------
# step round-trip across the durable boundary
# --------------------------------------------------------------------------


def _workflow_with(step: Any):
    from ai4ia_api.workflows.models import Workflow

    return Workflow(
        id="w1",
        userId="u1",
        name="wf",
        displayName="Wf",
        description="",
        enabled=True,
        steps=[step],
        createdAt="2026-01-01T00:00:00Z",
        updatedAt="2026-01-01T00:00:00Z",
    )


def _payload_step(step: Any) -> dict[str, Any]:
    return build_orchestration_payload(
        _workflow_with(step),
        user_id="u1",
        session_id="s1",
        run_input="hi",
        model_id="m",
        deployment=DeploymentOption(
            region="eastus2", dataZone="US", sku="GlobalStandard", deploymentName="d"
        ),
        correlation_id=None,
    )["steps"][0]


def test_every_step_field_survives_the_durable_boundary() -> None:
    """A durable run must execute the SAME step an in-request run does.

    Both sides of this boundary once hand-listed fields, so `extraTools` was
    dropped in transit: a durable run silently executed with fewer tools than
    the identical synchronous run, and still answered 200 while the model
    narrated work it had no tool to do.

    Derived from the model's own fields rather than a fixed list, so a field
    added to WorkflowStep later is covered on the day it is written.
    """
    from ai4ia_api.workflows.models import WorkflowStep

    step = WorkflowStep(
        agent="general",
        instruction="Do {input}",
        extraTools=["remember_memory", "calculator"],
    )
    fields = set(type(step).model_fields)
    assert fields >= {"agent", "instruction", "extraTools"}  # non-vacuity

    raw = _payload_step(step)
    assert set(raw) == fields, "the payload must carry every field, not a subset"

    assert _step_from_dict(raw) == step


def test_a_payload_written_before_a_field_existed_still_replays() -> None:
    """Orchestration history is immutable, so old payloads must still load.

    A run started before `extraTools` shipped has only agent/instruction in its
    history; rebuilding must fall back to the default rather than raise, or the
    deploy that adds a field strands every in-flight run.
    """
    from ai4ia_api.workflows.models import WorkflowStep

    old = _step_from_dict({"agent": "general", "instruction": "Do {input}"})

    assert old == WorkflowStep(agent="general", instruction="Do {input}")
    assert old.extraTools == []


# --------------------------------------------------------------------------
# metering + failure containment (the persist path had no coverage at all)
# --------------------------------------------------------------------------


def _deployment(name: str, region: str, zone: str | None) -> DeploymentOption:
    return DeploymentOption(
        region=region, dataZone=zone, sku="GlobalStandard", deploymentName=name
    )


class _RecordingUsage:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_completion(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _RecordingRepo:
    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.touched: list[tuple[str, str]] = []

    async def upsert_message(self, uid: str, message: Any) -> None:
        self.messages.append(message)

    async def touch_session(self, uid: str, session_id: str) -> None:
        self.touched.append((uid, session_id))


class _CatalogThatWouldPickTheWrongOption:
    """Mirrors ModelCatalog.resolve_deployment's options[0] fallback."""

    def __init__(self, option: DeploymentOption | None) -> None:
        self._option = option
        self.calls = 0

    def resolve_deployment(self, model_id: str, **_kwargs: Any) -> Any:
        self.calls += 1
        return self._option


class _State:
    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        self.usage = _RecordingUsage()
        self.session_repo = _RecordingRepo()


def _service_for(state: Any) -> DurableWorkflowService:
    return DurableWorkflowService(
        endpoint="https://x.durabletask.io", task_hub="h", app_state=state
    )


def _persist_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "context": context,
        "ok": True,
        "text": "done",
        "usage": _usage_to_dict(
            TokenUsage(prompt=10, completion=5, total=15, known=True, calls=1)
        ),
    }


@pytest.mark.anyio
async def test_accepted_execution_failure_is_persisted_as_run_failed() -> None:
    state = _State(_CatalogThatWouldPickTheWrongOption(None))
    context = _context_from_payload(_deployment("m-eastus", "eastus2", "US"))
    context.update(
        {
            "runId": "u1:run",
            "assistantMessageId": "assistant-1",
            "runFingerprint": "f" * 64,
        }
    )
    payload = _persist_payload(context)
    payload["ok"] = False
    payload["text"] = "step failed"
    await _service_for(state)._persist(payload)
    message = state.session_repo.messages[0]
    assert message.status is MessageStatus.error
    assert message.workflowRunStatus == "run_failed"
    assert message.workflowRunFingerprint == "f" * 64


def _context_from_payload(
    deployment: DeploymentOption, *, api: str = "chat"
) -> dict[str, Any]:
    from ai4ia_api.workflows.models import WorkflowStep

    return build_orchestration_payload(
        _workflow_with(WorkflowStep(agent="a", instruction="do {input}")),
        user_id="u1",
        session_id="s1",
        run_input="hi",
        model_id="m",
        deployment=deployment,
        correlation_id=None,
        api=api,
    )["context"]


def test_orchestration_payload_freezes_provider_api() -> None:
    context = _context_from_payload(
        _deployment("gpt-5.6-sol-example", "eastus2", "US"),
        api="responses",
    )
    assert context["api"] == "responses"


def test_orchestration_payload_omits_default_api_for_legacy_fingerprint() -> None:
    context = _context_from_payload(
        _deployment("gpt-5.4-example", "eastus2", "US")
    )
    assert "api" not in context


async def test_metering_uses_the_deployment_frozen_at_schedule_time() -> None:
    """Regression: `_persist` re-resolved from modelId alone, which falls back to
    options[0]. A run the caller pinned to Sweden metered as the first option's
    region and data zone."""
    scheduled = _deployment("m-swedencentral-glbl", "swedencentral", "EU")
    wrong = _deployment("m-eastus2-glbl", "eastus2", "US")
    state = _State(_CatalogThatWouldPickTheWrongOption(wrong))

    await _service_for(state)._persist(_persist_payload(_context_from_payload(scheduled)))

    assert len(state.usage.calls) == 1
    target = state.usage.calls[0]["target"]
    assert target.deployment == "m-swedencentral-glbl"
    assert target.region == "swedencentral"
    assert target.dataZone == "EU"
    # The frozen descriptor makes re-resolution unnecessary entirely.
    assert state.catalog.calls == 0


async def test_metering_still_happens_when_the_model_no_longer_resolves() -> None:
    """A catalog change mid-run must not silently skip the ledger row: the tokens
    were spent either way."""
    state = _State(_CatalogThatWouldPickTheWrongOption(None))
    context = _context_from_payload(_deployment("m-eastus2-glbl", "eastus2", "US"))

    await _service_for(state)._persist(_persist_payload(context))

    assert len(state.usage.calls) == 1
    assert state.usage.calls[0]["target"].deployment == "m-eastus2-glbl"


async def test_a_legacy_orchestration_without_a_frozen_target_still_meters() -> None:
    """An in-flight run scheduled by the previous revision has no `usageTarget` in
    its history. Durability means it is still replaying after the deploy, so it
    must fall back rather than lose its ledger row."""
    fallback = _deployment("m-eastus2-glbl", "eastus2", "US")
    state = _State(_CatalogThatWouldPickTheWrongOption(fallback))
    context = _context_from_payload(fallback)
    del context["usageTarget"]  # pre-upgrade history

    await _service_for(state)._persist(_persist_payload(context))

    assert len(state.usage.calls) == 1
    assert state.usage.calls[0]["target"].region == "eastus2"
    assert state.catalog.calls == 1


async def test_a_zero_call_run_still_meters_nothing() -> None:
    """Control: the 'no model call happened' rule is unchanged."""
    state = _State(_CatalogThatWouldPickTheWrongOption(None))
    payload = _persist_payload(_context_from_payload(_deployment("d", "eastus2", "US")))
    payload["usage"] = _usage_to_dict(TokenUsage.empty())

    await _service_for(state)._persist(payload)

    assert state.usage.calls == []
    assert len(state.session_repo.messages) == 1  # the reply is still written


def test_a_step_activity_that_raises_is_returned_as_a_fatal_step() -> None:
    """Regression: an exception escaping the activity failed the ORCHESTRATION, so
    the persist activity never ran — no assistant message, and usage already
    spent by earlier steps was discarded. The in-request path cannot lose either.
    """
    state = _State(_CatalogThatWouldPickTheWrongOption(None))
    service = _service_for(state)
    # `_run_on_app_loop` raises when the worker was never started, which is the
    # same shape as the activity-bridge timeout this must contain.
    activity = service._build_step_activity()

    out = activity(None, {"step": {"agent": "a", "instruction": "i"}, "index": 2,
                          "context": {"userId": "u1"}})

    assert out["fatal"] is True
    assert out["result"]["ok"] is False
    assert out["result"]["agent"] == "a"
    # Keeps runner.py's "Step N:" prefix so failure attribution still works.
    assert out["result"]["error"].startswith("Step 3:")
    assert out["usage"]["calls"] == 0


# --------------------------------------------------------------------------
# the run budget (durable_workflow_timeout_seconds was read by nothing)
# --------------------------------------------------------------------------


class _FakeState:
    def __init__(self, status: str, age_seconds: float) -> None:
        from datetime import datetime, timedelta, timezone

        self.runtime_status = status
        self.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.last_updated_at = self.created_at
        self.failure_details = None
        self.serialized_output = None

    def get_output(self) -> Any:
        return None


class _FakeClient:
    def __init__(self, state: Any) -> None:
        self._state = state
        self.terminated: list[str] = []

    async def get_orchestration_state(self, run_id: str) -> Any:
        return self._state

    async def terminate_orchestration(self, instance_id: str, **_kw: Any) -> None:
        self.terminated.append(instance_id)


def _service_with_client(state: Any, *, timeout: int) -> DurableWorkflowService:
    svc = DurableWorkflowService(
        endpoint="https://x.durabletask.io",
        task_hub="h",
        app_state=None,
        timeout_seconds=timeout,
    )
    svc._client = _FakeClient(state)
    return svc


async def test_an_overdue_run_is_stopped_and_reported_terminated() -> None:
    svc = _service_with_client(_FakeState("RUNNING", 600), timeout=300)

    out = await svc.get_status("u1::abc", user_id="u1")

    assert out is not None
    assert out.status == "TERMINATED"
    assert out.ok is False
    assert "300s budget" in (out.error or "")
    assert svc._client.terminated == ["u1::abc"]


async def test_a_run_inside_its_budget_is_left_alone() -> None:
    """Control: without this, a get_status that terminated everything would pass
    the test above just as well."""
    svc = _service_with_client(_FakeState("RUNNING", 10), timeout=300)

    out = await svc.get_status("u1::abc", user_id="u1")

    assert out is not None
    assert out.status == "RUNNING"
    assert svc._client.terminated == []


async def test_a_finished_run_is_never_terminated_however_old() -> None:
    """A COMPLETED run that predates the budget must keep its result."""
    svc = _service_with_client(_FakeState("COMPLETED", 99_999), timeout=300)

    out = await svc.get_status("u1::abc", user_id="u1")

    assert out is not None
    assert out.status == "COMPLETED"
    assert svc._client.terminated == []


async def test_a_non_positive_budget_disables_enforcement() -> None:
    svc = _service_with_client(_FakeState("RUNNING", 99_999), timeout=0)

    out = await svc.get_status("u1::abc", user_id="u1")

    assert out is not None
    assert out.status == "RUNNING"
    assert svc._client.terminated == []
