import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
import httpx
from pydantic import ValidationError

from ai4ia_api.agents.consent import tool_contract_hash
from ai4ia_api.agents.tool_exec import ToolExecutor, builtin_tools
from ai4ia_api.hard_quota.coverage import AttemptEnvelope
from ai4ia_api.hard_quota.dispatch import admitted_dispatch
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.request_constraints import constrain_request
from ai4ia_api.receipts import MAX_RECEIPT_BYTES
from ai4ia_api.workflows.automation_common import AutomationError, ExecutionLimits
from ai4ia_api.workflows.automation_models import WorkflowCheckpoint, persisted_model
from ai4ia_api.workflows.automation_receipts import project_message
from ai4ia_api.workflows.dispatch_scope import current_workflow_scope
from ai4ia_api.workflows.monetary_models import ApprovalSpend, BudgetView, OperationSpend, RunMoney
from ai4ia_api.workflows.monetary_quotes import operation_impact, quote_for_call
from ai4ia_api.workflows.monetary_profile import require_capped_profile
from ai4ia_api.workflows.run_controller import RunController
from tests.test_workflow_automation_service import begin, install
from tests.test_agent_runtime import _assistant_text
from tests.test_workflow_checkpoint_store import bundle


def pending(client):
    service, calls, sent = install(client, gated=True)
    user, run = begin(client, service)
    assert client.portal.call(service.advance, user.internal_user_id, run.runId)["status"] == "awaiting_approval"
    state, message = client.portal.call(service.load, user.internal_user_id, run.runId)
    assert len(calls) == 1 and sent == []
    return service, user, state, message, calls, sent


def test_new_pending_call_has_typed_immutable_unknown_spend_without_a_fake_zero(client, monkeypatch):
    service, user, state, _, calls, sent = pending(client)
    draft = state.draft
    quote = draft.spend
    assert quote.impact.amountMicroUsd is None and quote.impact.coverage == "unknown"
    assert quote.impact.bounds is None
    assert quote.budget.mode == "no_hard_dollar_cap" and quote.budget.remainingMicroUsd is None

    def no_repricing(*args, **kwargs):
        raise AssertionError("A historical quote read cannot query current prices.")

    monkeypatch.setattr(client.app.state.usage.pricing, "snapshot_token_prices", no_repricing)
    monkeypatch.setattr(client.app.state.usage.pricing, "rate", no_repricing)
    prefix = "/api/workflows/automation"
    for url in (prefix + "/approvals", f"{prefix}/runs/{state.runId}"):
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert "single-use" not in response.text
    reviewed, _ = client.portal.call(service.review, user.internal_user_id, state.runId, draft.id, user)
    assert reviewed.draft.spend == quote
    assert reviewed.draft.challengeSpendDigest == quote.quoteDigest
    assert len(calls) == 1 and sent == []
    response = client.post(f"{prefix}/runs/{state.runId}/approvals/{draft.id}/review")
    evidence = response.json()["spendEvidence"]
    assert evidence["impact"]["amountMicroUsd"] is None
    assert evidence["quote"]["quoteDigest"] == quote.quoteDigest
    assert isinstance(response.json()["spendImpact"], str), "Retain the older web consumer's additive field."


@pytest.mark.parametrize("changed", [False, True])
def test_valid_challenge_cannot_authorize_a_different_immutable_spend_quote(client, changed):
    service, user, state, _, calls, sent = pending(client)
    reviewed, grant = client.portal.call(service.review, user.internal_user_id, state.runId, state.draft.id, user)
    if changed:
        current, message = client.portal.call(service.load, user.internal_user_id, state.runId)
        updated = current.model_copy(deep=True)
        old = updated.draft.spend
        updated.draft.spend = ApprovalSpend.create(
            binding=old.bindingDigest, now=old.quotedAt + timedelta(microseconds=1),
            expires=old.expiresAt, impact=old.impact, budget=old.budget,
        )
        client.portal.call(service.commit, current, message, updated)
    decide = partial(
        service.decide, user.internal_user_id, state.runId, state.draft.id,
        user=user, decision="approve", request_id=reviewed.draft.challenge.id, grant=grant,
    )
    if changed:
        with pytest.raises(AutomationError, match="different spend evidence"):
            client.portal.call(decide)
        assert len(calls) == 1 and sent == []
    else:
        client.portal.call(decide)
        assert client.portal.call(service.advance, user.internal_user_id, state.runId)["status"] == "completed"
        assert len(calls) == 2 and sent == ["hello"]


@pytest.mark.parametrize("field,value", [("destination", "https://different.example"), ("contractDigest", "f" * 64)])
def test_spend_binding_refuses_changed_destination_or_schema_before_grant_consumption(client, field, value):
    service, user, state, _, calls, sent = pending(client)
    reviewed, grant = client.portal.call(service.review, user.internal_user_id, state.runId, state.draft.id, user)
    current, message = client.portal.call(service.load, user.internal_user_id, state.runId)
    updated = current.model_copy(deep=True)
    setattr(updated.draft, field, value)
    client.portal.call(service.commit, current, message, updated)
    with pytest.raises(AutomationError, match="no longer matches"):
        client.portal.call(partial(
            service.decide, user.internal_user_id, state.runId, state.draft.id,
            user=user, decision="approve", request_id=reviewed.draft.challenge.id, grant=grant,
        ))
    latest, _ = client.portal.call(service.load, user.internal_user_id, state.runId)
    assert not latest.draft.challenge.consumed
    assert len(calls) == 1 and not sent


def test_explicit_quote_refresh_rotates_the_grant_without_repeating_accepted_work(client):
    service, user, state, _, calls, sent = pending(client)
    prefix = f"/api/workflows/automation/runs/{state.runId}/approvals/{state.draft.id}"
    old = client.post(prefix + "/review").json()
    refreshed = client.post(prefix + "/review", json={"refreshSpendQuote": True})
    assert refreshed.status_code == 200, refreshed.text
    new = refreshed.json()
    assert old["spendEvidence"]["quote"]["quoteDigest"] != new["spendEvidence"]["quote"]["quoteDigest"]
    assert old["spendEvidence"]["quote"]["expiresAt"] == new["spendEvidence"]["quote"]["expiresAt"]
    assert client.post(prefix + "/decision", json={
        "decision": "approve", "requestId": old["requestId"], "grant": old["grant"],
    }).status_code == 409
    assert client.post(prefix + "/decision", json={
        "decision": "approve", "requestId": new["requestId"], "grant": new["grant"],
    }).status_code == 202
    assert len(calls) == 1 and not sent
    assert client.portal.call(service.advance, user.internal_user_id, state.runId)["status"] == "completed"
    assert len(calls) == 2 and sent == ["hello"]


def test_local_zero_requires_the_actual_repository_handler_not_its_safe_label():
    definition = builtin_tools()[0]
    contract = tool_contract_hash(
        definition.spec, definition.parameters, description=definition.spec.description,
        metadata=definition.consent_metadata,
    )
    local = operation_impact(definition, contract)
    assert local.coverage == "bounded" and local.amountMicroUsd == 0
    altered = replace(definition, handler=lambda args, ctx: {"may": "dispatch"})
    assert altered.spec == definition.spec
    unknown = operation_impact(altered, contract)
    assert unknown.coverage == "unknown" and unknown.amountMicroUsd is None
    assert operation_impact(definition, "f" * 64).amountMicroUsd is None


@pytest.mark.parametrize("guarded", [False, True])
def test_zero_effect_guard_is_inherited_by_actual_nested_dispatch_not_a_mutable_parent_flag(client, monkeypatch, guarded):
    service, calls, _ = install(client)
    original = ToolExecutor.execute
    outbound = []

    async def nested(executor, name, args, ctx):
        controller = current_workflow_scope()
        assert isinstance(controller, RunController)
        controller._local_zero.set(guarded)

        async def send():
            async with admitted_dispatch("mcp", {"exact": "operation"}, target="https://example.org") as lease:
                outbound.append(lease.payload)
                lease.report()

        child = asyncio.create_task(send())
        controller._local_zero.set(False)
        await child
        return await original(executor, name, args, ctx)

    monkeypatch.setattr(ToolExecutor, "execute", nested)
    user, state = begin(client, service)
    result = client.portal.call(service.advance, user.internal_user_id, state.runId)
    assert result["status"] == ("failed" if guarded else "completed"), result
    assert len(outbound) == (0 if guarded else 1)
    assert len(calls) == (1 if guarded else 2)


def test_unknown_exact_call_is_not_approvable_with_a_monetary_cap(client):
    _, _, state, _, _, _ = pending(client)
    account = RunMoney.new("e" * 64, 1_000_000)
    with pytest.raises(AutomationError, match="no supported monetary bound"):
        quote_for_call(
            state, state.draft, account, definition=None, now=state.draft.createdAt,
        )
    with pytest.raises(ValidationError, match="cannot appear free"):
        OperationSpend(
            coverage="unknown", amountMicroUsd=0, basis="unbounded-operation",
            reason="meter-coverage-unknown", bounds=None, localContractDigest=None,
        )
    with pytest.raises(ValidationError, match="incomplete"):
        BudgetView.model_validate({
            **BudgetView.from_account(account).model_dump(), "heldMicroUsd": None,
        })


@pytest.mark.parametrize("ignore_preflight", [False, True])
def test_shipping_none_still_refuses_finite_caps_before_actual_dispatch(client, monkeypatch, ignore_preflight):
    service, _, _ = install(client)
    client.app.state.hard_quota.attempts = AttemptEnvelope("fixture-is-not-proof", 1)
    client.app.state.agents.agents[0].tools = []
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        result = _assistant_text("done")
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
        return httpx.Response(200, json=result)

    client.app.state.gateway = ModelGatewayClient(
        client.app.state.settings, httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    assert client.post("/api/workflows", json={
        "name": "flow", "steps": [{"agent": "testleaf", "instruction": "{input}"}],
    }).status_code == 201
    body = {
        "selection": {"name": "flow", "model": "gpt-5.4"}, "input": "plain text",
        "limits": {"spendMode": "usd_app_meter", "maxSpendMicroUsd": 1_000_000},
        "allowTools": False, "allowAutomaticMemory": False,
        "idempotencyKey": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "~" + "1" * 32,
    }
    if ignore_preflight:
        monkeypatch.setattr(service, "require_spend_support", lambda *_args: None)
    refused = client.post("/api/workflows/automation/runs", json=body)
    if ignore_preflight:
        assert refused.status_code == 202, refused.text
        owner = next(iter(service.store.owners))
        run_id = refused.json()["runId"]
        result = client.portal.call(service.advance, owner, run_id)
        assert result["status"] == "failed", result
        handle = client.portal.call(service.owner, owner).value.runs[run_id]
        assert handle.terminal and not handle.active, "A proven pre-reservation refusal is not an unknown dispatch."
        assert handle.money.settledMicroUsd == handle.money.heldMicroUsd == 0
    else:
        assert refused.status_code == 422 and "verified bounded gateway" in refused.text
        assert not service.store.owners
    assert calls == []
    assert client.get("/api/workflows/automation/config").json()["monetaryCapAvailable"] is False
    body["limits"] = {"spendMode": "no_hard_dollar_cap"}
    body["idempotencyKey"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "~" + "2" * 32
    admitted = client.post("/api/workflows/automation/runs", json=body)
    assert admitted.status_code == 202, admitted.text
    owner = next(iter(service.store.owners))
    assert client.portal.call(service.advance, owner, admitted.json()["runId"])["status"] == "completed"
    assert len(calls) == 1 and not calls[0].get("tools")


def test_capped_profile_does_not_silently_drop_selected_tool_or_resource_requirements():
    selected = bundle()
    limited = ExecutionLimits(spendMode="usd_app_meter", maxSpendMicroUsd=1)
    with constrain_request(tools=False, automatic_memory=False):
        with pytest.raises(AutomationError, match="requires tools or resources"):
            require_capped_profile(selected.workflow, selected.agents, [], limited)
        selected.agents.agents[0].tools = []
        require_capped_profile(selected.workflow, selected.agents, [], limited)
        with pytest.raises(AutomationError, match="requires tools or resources"):
            require_capped_profile(selected.workflow, selected.agents, ["document"], limited)
    with pytest.raises(AutomationError, match="explicit"):
        require_capped_profile(selected.workflow, selected.agents, [], limited)


def test_monetary_receipt_keeps_quote_evidence_with_escaped_payload_budget(client):
    _, _, state, _, _, _ = pending(client)
    assert state.turn is not None
    enlarged = state.model_copy(deep=True)
    enlarged.turn.effectivePrompt = [{"role": "user", "content": "\u6f22" * 8000}]
    enlarged.approvalHistory = [
        enlarged.draft.model_copy(update={"id": f"prior-{index}", "state": "dispatched"})
        for index in range(23)
    ]
    message = project_message(enlarged)
    evidence = message.executionReceipt.workflowMoney
    assert evidence.quoteCount == 24 and len(evidence.quotes) == 4
    assert all(quote.amountMicroUsd is None for quote in evidence.quotes)
    assert len(json.dumps(message.executionReceipt.model_dump(mode="json"), ensure_ascii=True).encode("ascii")) <= MAX_RECEIPT_BYTES
    legacy = state.model_dump(mode="json")
    legacy["draft"].pop("spend")
    legacy["draft"].pop("challengeSpendDigest")
    restored = persisted_model(WorkflowCheckpoint, legacy)
    assert restored.draft.spend is None
    assert restored.model_dump(mode="json") == legacy
    assert project_message(restored).executionReceipt.workflowMoney is None
