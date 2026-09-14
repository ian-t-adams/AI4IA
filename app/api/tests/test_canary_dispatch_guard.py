"""Production factory guard: exact owner, real v1 claim, adapted body, one dispatch."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.main import create_app
from ai4ia_api.request_constraints import (
    CANARY_SENTINEL, arm_fresh_dispatch, build_canary_dispatch_guard, build_evaluation_dispatch_guard,
    constrain_request, fresh_session_required,
)
from ai4ia_api.sessions.memory_repo import InMemorySessionRepository
from ai4ia_api.sessions.models import Session
from tests.conftest import make_settings


async def claimed(repo):
    session = await repo.create_session(Session(
        userId="owner", model="model", title="Fixture", libraryDocumentIds=[],
    ))
    result = await repo.claim_fresh_session("owner", session)
    assert result is not None
    return result


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_real_adapter_payload_is_bounded_and_guard_permits_only_one_dispatch(api):
    repo = InMemorySessionRepository(deletion_enabled=True)
    session = await claimed(repo)
    gateway = ModelGatewayClient(make_settings())
    guard = build_canary_dispatch_guard(repo)
    with constrain_request(tools=False, automatic_memory=False, require_fresh_session=True):
        arm_fresh_dispatch("owner", session, "deployment", api, CANARY_SENTINEL)
        builder = gateway.build_responses_request if api == "responses" else gateway.build_request
        body = builder(
            deployment="deployment", messages=[{"role": "user", "content": CANARY_SENTINEL}],
            params={"max_tokens": 64, "reasoning_effort": "none"},
        ).json
        if api == "responses":
            assert body["max_output_tokens"] == 64
        result = await asyncio.gather(
            guard("owner", "deployment", body), guard("owner", "deployment", body),
        )
        assert result.count(True) == 1
        assert not await guard("owner", "deployment", body)
    assert not fresh_session_required()


@pytest.mark.parametrize("mutation", [
    "owner", "deployment", "input", "tools", "output", "unclaimed", "deleted", "scope", "unsupported",
])
async def test_invalid_envelopes_never_become_a_dispatch_grant(mutation):
    repo = InMemorySessionRepository(deletion_enabled=True)
    session = await claimed(repo)
    guard = build_canary_dispatch_guard(repo)
    body = {"messages": [{"role": "user", "content": CANARY_SENTINEL}], "max_tokens": 64}
    with constrain_request(tools=False, automatic_memory=False, require_fresh_session=True):
        if mutation != "unclaimed":
            arm_fresh_dispatch(
                "owner", session, "deployment", "anthropic" if mutation == "unsupported" else "chat",
                CANARY_SENTINEL,
            )
        if mutation == "input":
            body["messages"] = [{"role": "user", "content": "Unapproved input"}]
        elif mutation == "tools":
            body["tools"] = [{"type": "function"}]
        elif mutation == "output":
            body["max_tokens"] = 65
        elif mutation == "deleted":
            await repo.begin_deletion("owner", session.id)
        elif mutation == "scope":
            await repo.patch_session("owner", session.id, {"libraryDocumentIds": ["late"]})
        assert not await guard(
            "different-owner" if mutation == "owner" else "owner",
            "different-deployment" if mutation == "deployment" else "deployment",
            body,
        )


def test_factory_guard_is_armed_by_real_endpoint_claim_not_a_caller_tag():
    app = create_app(make_settings(session_deletion_enabled=True))
    with TestClient(app) as client:
        session = client.post("/api/sessions", json={
            "title": "Fixture", "model": "gpt-5.2", "libraryDocumentIds": [],
        }).json()
        factory_guard = app.state.canary_dispatch_guard
        accepted = []
        adapter = ModelGatewayClient(make_settings())

        class EnforcedGateway:
            async def complete(self, *, deployment, messages, params, **_):
                actual = adapter.build_request(
                    deployment=deployment, messages=messages, params=params,
                ).json
                allowed = await factory_guard(session["userId"], deployment, actual)
                if not allowed:
                    raise ModelGatewayError(403, "Canary envelope denied.")
                accepted.append(actual)
                return {"choices": [{"message": {"content": "ready"}}]}

        app.state.gateway = EnforcedGateway()
        request = {
            "sessionId": session["id"], "content": CANARY_SENTINEL, "stream": False,
            "params": {"max_tokens": 64, "reasoning_effort": "none"},
            "allowTools": False, "allowAutomaticMemory": False, "requireFreshSession": True,
        }
        result = client.post("/api/chat", json=request)
        assert result.status_code == 200, result.text
        assert len(accepted) == 1
        assert client.post("/api/chat", json=request).status_code == 409
        other = client.post("/api/sessions", json={
            "title": "Fixture", "model": "gpt-5.2", "libraryDocumentIds": [],
        }).json()
        # A body flag without a successful fresh claim is insufficient.
        result = client.post("/api/chat", json={
            **request, "sessionId": other["id"], "requireFreshSession": False,
        })
        assert result.status_code == 502
        assert len(accepted) == 1


def test_default_responses_floor_is_unchanged_without_fresh_constraint():
    gateway = ModelGatewayClient(make_settings())
    default = gateway.build_responses_request(
        deployment="deployment", messages=[], params={"max_tokens": 64},
    ).json
    assert default["max_output_tokens"] > 64


@pytest.mark.parametrize("prompt_size,output,expected", [
    (4096, 256, True), (4097, 256, False), (4096, 257, False),
])
async def test_separate_evaluation_guard_uses_same_claim_and_bounded_single_prompt(prompt_size, output, expected):
    repo = InMemorySessionRepository(deletion_enabled=True)
    session = await claimed(repo)
    prompt = "x" * prompt_size
    guard = build_evaluation_dispatch_guard(repo)
    with constrain_request(tools=False, automatic_memory=False, require_fresh_session=True):
        arm_fresh_dispatch("owner", session, "deployment", "chat", prompt)
        payload = {"messages": [{"role": "user", "content": prompt}], "max_tokens": output}
        assert await guard("owner", "deployment", payload) is expected
        assert not await guard("owner", "deployment", payload)


async def test_guard_profiles_do_not_create_separate_allowances_or_widen_the_monitor():
    repo = InMemorySessionRepository(deletion_enabled=True)
    session = await claimed(repo)
    canary = build_canary_dispatch_guard(repo)
    evaluation = build_evaluation_dispatch_guard(repo)
    with constrain_request(tools=False, automatic_memory=False, require_fresh_session=True):
        arm_fresh_dispatch("owner", session, "deployment", "chat", "Authored synthetic case")
        payload = {"messages": [{"role": "user", "content": "Authored synthetic case"}], "max_tokens": 64}
        assert not await canary("owner", "deployment", payload)
        assert not await evaluation("owner", "deployment", payload)
    session = await claimed(repo)
    with constrain_request(tools=False, automatic_memory=False, require_fresh_session=True):
        arm_fresh_dispatch("owner", session, "deployment", "chat", CANARY_SENTINEL)
        payload = {"messages": [{"role": "user", "content": CANARY_SENTINEL}], "max_tokens": 64}
        assert await evaluation("owner", "deployment", payload)
        assert not await canary("owner", "deployment", payload)


def test_both_guards_are_registered_by_the_real_factory():
    app = create_app(make_settings())
    with TestClient(app):
        assert callable(app.state.canary_dispatch_guard)
        assert callable(app.state.evaluation_dispatch_guard)
