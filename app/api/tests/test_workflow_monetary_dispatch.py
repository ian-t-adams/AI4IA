"""Real workflow/gateway/admission integration with the shared offline verifier."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import uuid4

import httpx
import pytest

from ai4ia_api.gateway.attempts import ATTEMPT_HEADER, ATTEMPT_VERSION, current_attempt_envelope
from ai4ia_api.config import GatewayAuthMode
from ai4ia_api.entitlements.models import EntitlementLimits
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.hard_quota.models import MAX_QUANTITY, QuotaError
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from ai4ia_api.usage.service import UsageService
from ai4ia_api.workflows.automation_common import digest
from ai4ia_api.workflows.automation_service import WorkflowAutomationService
from ai4ia_api.workflows.automation_store import CosmosAutomationStore
from ai4ia_api.workflows.models import WorkflowUpdate
from ai4ia_api.workflows.schedule_service import WorkflowScheduleService
from tests.cosmos_deletion_fake import Container
from tests.test_gateway_attempts import FixtureVerifier, wire as wire
from tests.test_genai_telemetry import POISON, _assert_clean, capture as capture
from tests.test_hard_quota_dispatch import DEPLOYMENT, Harness, response_for
from tests.test_workflow_automation_service import install


def set_prices(state, pricing):
    state.usage = UsageService(state.usage._repo, pricing, enabled=state.usage.enabled)
    state.hard_quota.pricing = pricing


def set_clock(service, now):
    if isinstance(service.store, CosmosAutomationStore):
        service.store._container.now = now
    else:
        service.store.clock = lambda: now


@pytest.fixture(params=["memory", "cosmos"])
def monetary(client, wire, request):
    service, _, _ = install(client)
    state = client.app.state
    catalog = Harness().catalog
    pricing = PricingBook({"fixture-text": PriceRate(1.0, 2.0)}, currency="USD", version="fixture-v1")
    state.catalog = state.policy.catalog = state.hard_quota.catalog = catalog
    set_prices(state, pricing)
    state.settings.model_gateway_url = "https://gateway.test/openai"
    state.settings.model_gateway_auth_mode = GatewayAuthMode.api_key
    state.settings.model_gateway_api_key = "fixture-ingress"
    state.settings.model_gateway_api_key_header = "S7P-KEY"
    state.agents.agents[0].tools = []
    verifier = FixtureVerifier()
    state.gateway = ModelGatewayClient(
        state.settings, httpx.AsyncClient(transport=wire[2]), attempt_verifier=verifier,
    )
    if request.param == "cosmos":
        container = Container("userId")
        container.now = datetime.now(timezone.utc)
        service.store = CosmosAutomationStore(container)
    assert not state.hard_quota.enabled and not state.settings.hard_quota_enabled
    return service, verifier, wire


def start(client, *, limit=140, steps=1):
    assert client.post("/api/workflows", json={
        "name": "priced", "steps": [{"agent": "testleaf", "instruction": "{input}"} for _ in range(steps)],
    }).status_code == 201
    body = {
        "selection": {"name": "priced", "model": "fixture-text"},
        "input": "Plain text.", "allowTools": False, "allowAutomaticMemory": False,
        "limits": {
            "spendMode": "usd_app_meter", "maxSpendMicroUsd": limit, "maxOutputTokens": 20,
            "maxToolCalls": 0,
        },
        "idempotencyKey": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") + "~" + uuid4().hex,
    }
    result = client.post("/api/workflows/automation/runs", json=body)
    assert result.status_code == 202, result.text
    run_id = result.json()["runId"]
    return run_id.partition(":")[0], run_id, body


@pytest.mark.parametrize("limit,allowed", [(139, False), (140, True), (141, True)])
def test_real_bound_reserves_before_send_at_the_exact_dollar_boundary(client, monetary, limit, allowed):
    service, verifier, (sent, response, _) = monetary
    owner, run_id, _ = start(client, limit=limit)
    holds = []
    claims = []

    def reply(request):
        snapshot = claims[-1]
        account = snapshot.runs[run_id].money
        assert account.heldMicroUsd == 140 and account.reservations == 1
        dispatch = next(effect for effect in snapshot.effects.values() if effect.category == "dispatch")
        assert dispatch.money.bounds.attemptVersion == ATTEMPT_VERSION
        assert dispatch.payloadDigest == digest({
            "surface": "chat", "payload": json.loads(request.content),
            "deployment": DEPLOYMENT, "target": str(request.url),
        })
        assert current_attempt_envelope(
            "chat", json.loads(request.content), deployment=DEPLOYMENT, target=str(request.url), owner=owner,
        ) is None
        return response_for("chat", request)

    response[0] = reply
    original_write = service.store.write_owner

    async def inspect_claim(prior, updated):
        committed = await original_write(prior, updated)
        if committed and updated.runs[run_id].money.heldMicroUsd:
            assert not sent
            holds.append(updated.runs[run_id].money)
            claims.append(updated.model_copy(deep=True))
        return committed

    service.store.write_owner = inspect_claim
    result = client.portal.call(service.advance, owner, run_id)
    assert result["status"] == ("completed" if allowed else "failed"), result
    assert len(sent) == int(allowed)
    if allowed:
        assert holds and holds[0].heldMicroUsd == 140
        assert sent[0].headers[ATTEMPT_HEADER].startswith(ATTEMPT_VERSION + ".")
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert account.settledMicroUsd == (7 if allowed else 0)
    assert account.compactedMicroUsd == account.settledMicroUsd
    assert account.heldMicroUsd == account.unknownMicroUsd == 0
    assert account.reservations == int(allowed)
    assert verifier.verified == 1
    again = client.portal.call(service.advance, owner, run_id)
    assert again["status"] == result["status"] and len(sent) == int(allowed)
    current = client.get(f"/api/workflows/automation/runs/{run_id}").json()
    assert current["budget"]["settledMicroUsd"] == (7 if allowed else 0)
    assert current["message"]["executionReceipt"]["workflowMoney"]["budget"]["heldMicroUsd"] == 0


@pytest.mark.parametrize("limit,calls", [(146, 1), (147, 2)])
def test_next_step_spends_only_remaining_budget_after_compaction(client, monetary, limit, calls):
    service, _, (sent, _, _) = monetary
    owner, run_id, _ = start(client, limit=limit, steps=2)
    first = client.portal.call(service.advance, owner, run_id)
    assert not first["terminal"] and len(sent) == 1
    before = client.portal.call(service.owner, owner).value
    assert before.runs[run_id].money.compactedMicroUsd == 7
    assert not before.effects
    final = client.portal.call(service.advance, owner, run_id)
    assert final["status"] == ("completed" if calls == 2 else "failed"), final
    assert len(sent) == calls
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert account.settledMicroUsd == 7 * calls and account.heldMicroUsd == 0


@pytest.mark.parametrize("failure", ["unknown", "partial", "overflow", "timeout", "lost_ack"])
def test_unknown_or_accepted_lost_work_keeps_the_original_full_reservation(client, monetary, failure):
    service, _, (sent, response, _) = monetary
    owner, run_id, _ = start(client, limit=300, steps=2)

    def fail(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "lost_ack":
            raise httpx.RemoteProtocolError("synthetic lost acknowledgment", request=request)
        result = response_for("chat", request).json()
        result["usage"] = (
            None if failure == "unknown" else {"prompt_tokens": 3} if failure == "partial"
            else {"prompt_tokens": MAX_QUANTITY, "completion_tokens": 1, "total_tokens": MAX_QUANTITY + 1}
        )
        return httpx.Response(200, json=result)

    response[0] = fail
    result = client.portal.call(service.advance, owner, run_id)
    assert result["terminal"]
    assert len(sent) == 1
    held = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert held.heldMicroUsd == held.unknownMicroUsd == 140
    assert held.settledMicroUsd == 0
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    client.portal.call(service.cancel, owner, run_id)
    assert client.portal.call(service.owner, owner).value.runs[run_id].money == held
    assert len(sent) == 1


def test_historical_settlement_and_reads_do_not_reprice_after_the_book_changes(client, monetary):
    service, _, (sent, response, _) = monetary
    owner, run_id, _ = start(client, limit=300, steps=2)

    def change_prices(request):
        set_prices(client.app.state, PricingBook(
            {"fixture-text": PriceRate(100.0, 200.0)}, currency="USD", version="new-prices",
        ))
        return response_for("chat", request)

    response[0] = change_prices
    assert not client.portal.call(service.advance, owner, run_id)["terminal"]
    first = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert first.settledMicroUsd == 7
    records = client.portal.call(partial(
        client.app.state.usage._repo.list_for_session, owner,
        client.portal.call(service.load, owner, run_id)[0].sessionId, limit=20,
    ))
    assert len(records) == 1 and records[0].priceVersion == "fixture-v1" and records[0].estCostMicroUsd == 7
    client.get(f"/api/workflows/automation/runs/{run_id}")
    assert client.portal.call(service.owner, owner).value.runs[run_id].money == first
    result = client.portal.call(service.advance, owner, run_id)
    assert result["status"] == "failed" and len(sent) == 1
    assert client.portal.call(service.owner, owner).value.runs[run_id].money.settledMicroUsd == 7


def test_changed_price_after_reservation_refuses_send_but_does_not_refund_the_claim(client, monetary):
    service, _, (sent, _, _) = monetary
    owner, run_id, _ = start(client, limit=300)
    original = service.store.write_owner

    async def change_price(prior, updated):
        result = await original(prior, updated)
        if result and updated.runs[run_id].money.heldMicroUsd:
            set_prices(client.app.state, PricingBook(
                {"fixture-text": PriceRate(1.0, 2.0)}, currency="USD", version="changed-version",
            ))
        return result

    service.store.write_owner = change_price
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert not sent
    assert account.heldMicroUsd == account.unknownMicroUsd == 140


@pytest.mark.parametrize("revoked", [False, True])
def test_execution_time_policy_recheck_never_uses_a_reservation_as_permission(client, monetary, revoked):
    service, _, (sent, _, _) = monetary
    owner, run_id, _ = start(client)
    original = service.store.write_owner

    async def revoke(prior, updated):
        result = await original(prior, updated)
        if revoked and result and updated.runs[run_id].money.heldMicroUsd:
            workflow = await client.app.state.workflow_service.get(owner, "priced")
            await client.app.state.workflow_service.update(owner, "priced", WorkflowUpdate(
                displayName=workflow.displayName, description=workflow.description, steps=workflow.steps,
                enabled=False, expectedRevision=workflow.revision,
            ))
        return result

    service.store.write_owner = revoke
    result = client.portal.call(service.advance, owner, run_id)
    assert len(sent) == (0 if revoked else 1)
    assert result["terminal"]


def test_changed_cap_preferences_cannot_reuse_an_admitted_run_identity(client, monetary):
    _, _, (sent, _, _) = monetary
    _, run_id, body = start(client)
    original = client.post("/api/workflows/automation/runs", json=body)
    assert original.status_code == 202 and original.json()["runId"] == run_id
    changed = {**body, "limits": {**body["limits"], "maxSpendMicroUsd": 141}}
    assert client.post("/api/workflows/automation/runs", json=changed).status_code == 409
    assert not sent


def test_verifier_revocation_is_a_typed_predispatch_refusal_not_an_unknown_charge(client, monetary):
    service, verifier, (sent, _, _) = monetary
    owner, run_id, _ = start(client)

    async def refuse(capability):
        raise QuotaError("Synthetic verifier refused current topology.")

    verifier.verify = refuse
    result = client.portal.call(service.advance, owner, run_id)
    assert result["terminal"] and not sent
    handle = client.portal.call(service.owner, owner).value.runs[run_id]
    assert handle.terminal and not handle.active
    assert handle.money.heldMicroUsd == handle.money.settledMicroUsd == 0


def test_capability_expiry_before_the_next_step_does_not_replay_completed_work(client, monetary):
    service, verifier, (sent, _, _) = monetary
    owner, run_id, _ = start(client, limit=300, steps=2)
    assert not client.portal.call(service.advance, owner, run_id)["terminal"]
    verifier.capability = replace(verifier.capability, expires_at=0)
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    assert len(sent) == 1
    assert client.portal.call(service.owner, owner).value.runs[run_id].money.settledMicroUsd == 7


def test_simultaneous_activities_still_have_one_capped_gateway_dispatch(client, monetary, monkeypatch):
    from ai4ia_api.gateway import client as gateway_module

    service, _, (sent, _, transport) = monetary
    owner, run_id, _ = start(client)

    async def race():
        entered, released = asyncio.Event(), asyncio.Event()

        async def send(request):
            entered.set()
            await released.wait()
            return await transport.handle_async_request(request)

        monkeypatch.setattr(
            gateway_module, "bounded_http_client",
            lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(send)),
        )
        first = asyncio.create_task(service.advance(owner, run_id))
        await entered.wait()
        second = await service.advance(owner, run_id)
        assert not second["terminal"]
        held = (await service.owner(owner)).value.runs[run_id].money
        assert held.heldMicroUsd == 140 and held.reservations == 1
        released.set()
        return await first

    assert client.portal.call(race)["status"] == "completed"
    assert len(sent) == 1


@pytest.mark.parametrize("api", ["chat", "responses", "anthropic"])
def test_each_real_provider_adapter_uses_the_same_priced_one_attempt_contract(client, monetary, api):
    service, _, (sent, response, _) = monetary
    client.app.state.catalog.models[0].api = api
    response[0] = lambda request: response_for(api, request)
    owner, run_id, _ = start(client)
    result = client.portal.call(service.advance, owner, run_id)
    assert result["status"] == "completed", result
    assert len(sent) == 1
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert account.settledMicroUsd == 7 and account.reservations == 1
    payload = json.loads(sent[0].content)
    assert "tools" not in payload
    if api == "responses":
        assert payload["store"] is False and payload["max_output_tokens"] == 20


@pytest.mark.parametrize("unpriced", [False, True])
def test_absent_price_is_never_a_free_capped_dispatch(client, monetary, unpriced):
    service, _, (sent, _, _) = monetary
    if unpriced:
        set_prices(client.app.state, PricingBook({}, currency="USD", version=None))
    owner, run_id, _ = start(client)
    result = client.portal.call(service.advance, owner, run_id)
    assert result["status"] == ("failed" if unpriced else "completed"), result
    assert len(sent) == (0 if unpriced else 1)
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert account.reservations == (0 if unpriced else 1)


def test_lost_owner_cas_ack_retains_the_hold_and_never_reissues_the_model(client, monetary):
    service, _, (sent, _, _) = monetary
    owner, run_id, _ = start(client)
    original = service.store.write_owner
    lost = False

    async def lose_ack(prior, updated):
        nonlocal lost
        result = await original(prior, updated)
        if result and not lost and updated.runs[run_id].money.heldMicroUsd:
            lost = True
            raise OSError("synthetic owner write acknowledgment lost")
        return result

    service.store.write_owner = lose_ack
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    held = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert lost and not sent
    assert held.heldMicroUsd == 140 and held.settledMicroUsd == 0 and held.reservations == 1
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    client.portal.call(service.cancel, owner, run_id)
    assert client.portal.call(service.owner, owner).value.runs[run_id].money == held
    assert not sent


@pytest.mark.parametrize("cleanup", ["cancel", "clear", "delete"])
def test_accepted_accounting_finishes_after_cancellation_and_conversation_cleanup(client, monetary, monkeypatch, cleanup):
    from ai4ia_api.gateway import client as gateway_module

    service, _, (sent, _, transport) = monetary
    owner, run_id, _ = start(client, limit=300, steps=2)
    session_id = client.portal.call(service.load, owner, run_id)[0].sessionId

    async def race():
        entered, released = asyncio.Event(), asyncio.Event()

        async def send(request):
            entered.set()
            await released.wait()
            return await transport.handle_async_request(request)

        monkeypatch.setattr(
            gateway_module, "bounded_http_client",
            lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(send)),
        )
        running = asyncio.create_task(service.advance(owner, run_id))
        await entered.wait()
        await service.cancel(owner, run_id)
        if cleanup == "clear":
            await client.app.state.session_repo.clear_messages(owner, session_id)
        elif cleanup == "delete":
            await client.app.state.session_repo.begin_deletion(owner, session_id)
        before = await client.app.state.usage._repo.list_for_session(owner, session_id, limit=20)
        assert before == [], "In-flight usage must remain provisional."
        set_prices(client.app.state, PricingBook({}, currency="USD", version=None))
        released.set()
        result = await running
        assert result["terminal"] and result["status"] == "cancelled"
        return await client.app.state.usage._repo.list_for_session(owner, session_id, limit=20)

    rows = client.portal.call(race)
    assert len(sent) == len(rows) == 1
    assert rows[0].estCostMicroUsd == 7 and rows[0].priceVersion == "fixture-v1"
    handle = client.portal.call(service.owner, owner).value.runs[run_id]
    assert handle.money.settledMicroUsd == 7 and handle.money.heldMicroUsd == 0
    assert handle.terminal and not handle.active


@pytest.mark.parametrize("revoked", [False, True])
def test_current_entitlement_stops_the_next_step_but_not_accepted_money_accounting(client, monetary, monkeypatch, revoked):
    from ai4ia_api.gateway import client as gateway_module

    service, _, (sent, _, transport) = monetary
    owner, run_id, _ = start(client, limit=300, steps=2)

    async def race():
        entered, released = asyncio.Event(), asyncio.Event()

        async def send(request):
            entered.set()
            await released.wait()
            return await transport.handle_async_request(request)

        monkeypatch.setattr(
            gateway_module, "bounded_http_client",
            lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(send)),
        )
        running = asyncio.create_task(service.advance(owner, run_id))
        await entered.wait()
        if revoked:
            await client.app.state.entitlements.set(owner, EntitlementLimits(disabled=True), updated_by=None)
        released.set()
        first = await running
        assert not first["terminal"]
        assert (await service.owner(owner)).value.runs[run_id].money.settledMicroUsd == 7
        return await service.advance(owner, run_id)

    result = client.portal.call(race)
    assert result["terminal"]
    assert len(sent) == (1 if revoked else 2)
    handle = client.portal.call(service.owner, owner).value.runs[run_id]
    assert handle.money.settledMicroUsd == (7 if revoked else 14)
    assert handle.terminal and not handle.active


def traced_gateway(client, verifier):
    state = client.app.state
    state.settings.applicationinsights_connection_string = "InstrumentationKey=synthetic"
    state.agents.agents[0].systemPrompt = f"Use only plain text. {POISON}"
    state.gateway = ModelGatewayClient(
        state.settings, http_client=state.gateway._http, attempt_verifier=verifier,
    )


def clean_model_spans(exporter, owner, run_id):
    from ai4ia_api.genai_telemetry import INSTRUMENTATION_NAME

    spans = [span for span in exporter.get_finished_spans() if span.instrumentation_scope.name == INSTRUMENTATION_NAME]
    for span in spans:
        _assert_clean(span)
        assert owner not in span.to_json() and run_id not in span.to_json()
        assert "fixture-ingress" not in span.to_json()
    return spans


def test_restart_does_not_export_reprice_or_charge_prior_capped_model_work(client, monetary, capture):
    service, verifier, (sent, _, _) = monetary
    exporter, _ = capture
    traced_gateway(client, verifier)
    owner, run_id, _ = start(client, limit=287, steps=2)
    assert not client.portal.call(service.advance, owner, run_id)["terminal"]
    original_span = clean_model_spans(exporter, owner, run_id)[0].to_json()
    state, _ = client.portal.call(service.load, owner, run_id)
    original = client.portal.call(partial(
        client.app.state.usage._repo.list_for_session, owner, state.sessionId, limit=20,
    ))[0]
    assert original.estCostMicroUsd == 7
    set_prices(client.app.state, PricingBook(
        {"fixture-text": PriceRate(2.0, 4.0)}, currency="USD", version="after-restart",
    ))
    resumed = WorkflowAutomationService(client.app.state, service.store, service.access)
    resumed.host = service.host
    assert client.portal.call(resumed.advance, owner, run_id)["status"] == "completed"
    assert client.portal.call(resumed.advance, owner, run_id)["status"] == "completed"
    spans = clean_model_spans(exporter, owner, run_id)
    assert len(spans) == len(sent) == 2 and spans[0].to_json() == original_span
    assert all(span.attributes["ai4ia.gen_ai.http_attempts"] == 1 for span in spans)
    assert all(span.attributes["gen_ai.usage.input_tokens"] == 3 for span in spans)
    rows = client.portal.call(partial(
        client.app.state.usage._repo.list_for_session, owner, state.sessionId, limit=20,
    ))
    assert len(rows) == 2 and next(row for row in rows if row.id == original.id) == original
    assert sum(row.estCostMicroUsd for row in rows) == 21
    account = client.portal.call(resumed.owner, owner).value.runs[run_id].money
    assert account.settledMicroUsd == account.compactedMicroUsd == 21 and account.reservations == 2


@pytest.mark.parametrize("failure", ["budget", "cancelled"])
def test_denied_and_cancelled_capped_spans_never_invent_free_provider_usage(client, monetary, capture, failure):
    service, verifier, (sent, response, _) = monetary
    exporter, _ = capture
    traced_gateway(client, verifier)
    owner, run_id, _ = start(client, limit=139 if failure == "budget" else 140)
    if failure == "cancelled":
        def cancel(request):
            raise asyncio.CancelledError()
        response[0] = cancel
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    spans = clean_model_spans(exporter, owner, run_id)
    assert len(spans) == 1
    assert spans[0].attributes["ai4ia.gen_ai.http_attempts"] == (0 if failure == "budget" else 1)
    assert spans[0].attributes["ai4ia.gen_ai.usage.coverage"] == "unknown"
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert account.heldMicroUsd == (0 if failure == "budget" else 140)
    assert account.settledMicroUsd == 0 and len(sent) == (0 if failure == "budget" else 1)


def test_safe_schedule_pins_an_independent_run_cap_and_one_occurrence_identity(client, monetary):
    service, _, (sent, _, _) = monetary
    owner, previous_run, body = start(client)
    assert client.portal.call(service.advance, owner, previous_run)["status"] == "completed"
    prior_money = client.portal.call(service.owner, owner).value.runs[previous_run].money
    now = datetime.now(timezone.utc)
    due = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    body = {
        **body,
        "idempotencyKey": now.isoformat().replace("+00:00", "Z") + "~" + uuid4().hex,
        "rule": {
            "frequency": "once", "timezone": "UTC", "localDate": due.date().isoformat(),
            "localTime": due.time().replace(tzinfo=None).isoformat(), "maxOccurrences": 1,
        },
    }
    saved = client.post("/api/workflows/automation/schedules", json=body)
    assert saved.status_code == 201, saved.text
    schedule = saved.json()
    assert schedule["limits"]["maxSpendMicroUsd"] == 140
    assert schedule["allowTools"] is False and schedule["allowAutomaticMemory"] is False
    set_clock(service, due)
    scheduler = WorkflowScheduleService(service)
    assert client.portal.call(scheduler.tick, owner, schedule["id"], schedule["generation"])["terminal"]
    assert client.portal.call(scheduler.tick, owner, schedule["id"], schedule["generation"])["terminal"]
    current = client.portal.call(scheduler.list, owner)[0]
    assert current.consumed == 1 and len(current.history) == 1
    run_id = current.history[0].runId
    assert run_id != previous_run and len(service.host.started) == 2
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert account.limitMicroUsd == 140 and account.heldMicroUsd == account.settledMicroUsd == 0
    assert account.budgetId != prior_money.budgetId
    assert client.portal.call(service.advance, owner, run_id)["status"] == "completed"
    assert len(sent) == 2
    assert client.portal.call(service.owner, owner).value.runs[previous_run].money == prior_money


@pytest.mark.parametrize("committed", [False, True])
def test_completed_metering_and_lost_checkpoint_ack_do_not_repeat_the_accepted_model(client, monetary, monkeypatch, committed):
    service, _, (sent, _, _) = monetary
    owner, run_id, _ = start(client)
    original = client.app.state.session_repo.replace_workflow_checkpoint
    lost = False

    async def lose_result(owner_id, state, message, **expected):
        nonlocal lost
        if state.operationState == "complete" and not lost:
            lost = True
            if committed:
                assert await original(owner_id, state, message, **expected)
            raise OSError("synthetic checkpoint acknowledgment lost")
        return await original(owner_id, state, message, **expected)

    monkeypatch.setattr(client.app.state.session_repo, "replace_workflow_checkpoint", lose_result)
    first = client.portal.call(service.advance, owner, run_id)
    assert lost and len(sent) == 1
    known = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert known.settledMicroUsd == 7 and known.heldMicroUsd == 0
    set_clock(service, client.portal.call(service.owner, owner).now + timedelta(seconds=40))
    resumed = WorkflowAutomationService(client.app.state, service.store, service.access)
    resumed.host = service.host
    result = client.portal.call(resumed.advance, owner, run_id)
    assert result["status"] == ("completed" if committed else "failed"), (first, result)
    assert len(sent) == 1
    after = client.portal.call(service.owner, owner).value.runs[run_id].money
    assert after.settledMicroUsd == 7 and after.reservations == 1


@pytest.mark.parametrize("foreign_close", [False, True])
def test_frozen_monetary_bounds_survive_stream_proof_unbinding_and_foreign_close(client, monetary, monkeypatch, foreign_close):
    service, verifier, (sent, response, _) = monetary
    response[0] = lambda request: response_for("chat-stream", request)
    owner, run_id, _ = start(client)
    gateway = client.app.state.gateway
    streamed = gateway.stream

    async def complete_through_real_stream(**kwargs):
        events = streamed(**kwargs)
        if foreign_close:
            await anext(events)
            await asyncio.create_task(events.aclose())
            raise asyncio.CancelledError()
        async for _event in events:
            pass
        return response_for("chat", httpx.Request("POST", "https://fixture.invalid")).json()

    monkeypatch.setattr(gateway, "complete", complete_through_real_stream)
    result = client.portal.call(service.advance, owner, run_id)
    assert result["terminal"] and len(sent) == verifier.verified == 1
    account = client.portal.call(service.owner, owner).value.runs[run_id].money
    if foreign_close:
        assert account.heldMicroUsd == account.unknownMicroUsd == 140
        assert account.settledMicroUsd == 0
    else:
        assert result["status"] == "completed", result
        assert account.settledMicroUsd == 7 and account.heldMicroUsd == 0
    assert client.portal.call(service.advance, owner, run_id)["terminal"]
    assert len(sent) == 1
