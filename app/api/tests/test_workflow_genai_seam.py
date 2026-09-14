"""Real gateway/exporter evidence across durable workflow boundaries."""
import asyncio
from functools import partial

import httpx
import pytest

from ai4ia_api.agents.agent_catalog import AgentCatalog
from ai4ia_api.entitlements.models import EntitlementLimits
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.request_constraints import constrain_request
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from ai4ia_api.workflows.automation_service import WorkflowAutomationService
from tests.test_genai_telemetry import POISON, _assert_clean, capture as capture
from tests.test_workflow_automation_service import begin, install


def _setup(client, *, gated=True):
    service, calls, sent = install(client, gated=gated)
    state = client.app.state
    state.agents = AgentCatalog(agents=[
        agent.model_copy(update={"systemPrompt": f"Complete the fixture. {POISON}"})
        for agent in state.agents.agents
    ])
    state.settings.applicationinsights_connection_string = "InstrumentationKey=synthetic"
    state.gateway = ModelGatewayClient(state.settings, http_client=state.gateway._http)
    return service, calls, sent


def _rows(client, owner, session):
    return client.portal.call(partial(
        client.app.state.usage._repo.list_for_session, owner, session, limit=20,
    ))


def _approve(client, service, user, run):
    state, _ = client.portal.call(service.load, user.internal_user_id, run.runId)
    reviewed, grant = client.portal.call(
        service.review, user.internal_user_id, run.runId, state.draft.id, user,
    )
    client.portal.call(partial(
        service.decide, user.internal_user_id, run.runId, state.draft.id,
        user=user, decision="approve", request_id=reviewed.draft.challenge.id, grant=grant,
    ))


def _clean_spans(exporter, user, run):
    spans = exporter.get_finished_spans()
    for span in spans:
        _assert_clean(span)
        for forbidden in (user.internal_user_id, run.sessionId, run.runId, "hello", "Calculate."):
            assert forbidden not in span.to_json()
    return spans


def test_restart_exports_only_new_dispatch_without_recounting_or_repricing(client, capture):
    exporter, _ = capture
    service, calls, sent = _setup(client)
    user, run = begin(client, service)
    assert client.portal.call(service.advance, user.internal_user_id, run.runId)["status"] == "awaiting_approval"
    first_spans = _clean_spans(exporter, user, run)
    assert len(first_spans) == 1
    first_span = first_spans[0].to_json()
    first_row = _rows(client, user.internal_user_id, run.sessionId)[0]
    assert first_row.totalTokens == 15
    client.app.state.usage._pricing = PricingBook(
        {"gpt-5.4": PriceRate(100, 200)}, currency="USD", version="synthetic-price-after-pause",
    )
    resumed = WorkflowAutomationService(client.app.state, service.store, service.access)
    resumed.host = service.host
    _approve(client, resumed, user, run)
    assert client.portal.call(resumed.advance, user.internal_user_id, run.runId)["status"] == "completed"
    assert client.portal.call(resumed.advance, user.internal_user_id, run.runId)["status"] == "completed"
    spans = _clean_spans(exporter, user, run)
    assert len(spans) == len(calls) == 2
    assert spans[0].to_json() == first_span
    assert all(span.attributes["ai4ia.gen_ai.http_attempts"] == 1 for span in spans)
    assert all(span.attributes["gen_ai.usage.input_tokens"] == 10 for span in spans)
    assert all(span.attributes["gen_ai.usage.output_tokens"] == 5 for span in spans)
    rows = _rows(client, user.internal_user_id, run.sessionId)
    assert len(rows) == 3  # two model operations and the one real MCP dispatch
    assert next(row for row in rows if row.id == first_row.id) == first_row
    model_rows = [row for row in rows if row.model == "gpt-5.4"]
    assert len(model_rows) == 2 and sum(row.totalTokens for row in model_rows) == 30
    assert any(row.priceVersion == "synthetic-price-after-pause" for row in model_rows)
    assert sent == ["hello"]


@pytest.mark.parametrize("deny", [None, "policy", "request"])
def test_resume_rechecks_policy_and_constraints_before_new_exported_work(client, capture, deny):
    exporter, _ = capture
    service, calls, sent = _setup(client)
    user, run = begin(client, service)
    client.portal.call(service.advance, user.internal_user_id, run.runId)
    _approve(client, service, user, run)
    if deny == "policy":
        client.portal.call(partial(
            client.app.state.entitlements.set, user.internal_user_id,
            EntitlementLimits(disabled=True), updated_by="synthetic-test",
        ))
    with constrain_request(tools=deny != "request", automatic_memory=True):
        result = client.portal.call(service.advance, user.internal_user_id, run.runId)
    spans = _clean_spans(exporter, user, run)
    assert len(spans) == len(calls) == (2 if deny is None else 1)
    assert sent == (["hello"] if deny is None else [])
    state, message = client.portal.call(service.load, user.internal_user_id, run.runId)
    assert message.executionReceipt.usage.calls == (2 if deny is None else 1)
    assert (result["status"] == "completed") is (deny is None)
    assert state.status != "running"


def test_cancelled_unknown_dispatch_is_not_reexported_or_reported_as_free(client, capture):
    exporter, _ = capture
    service, _, _ = _setup(client, gated=False)
    user, run = begin(client, service)

    async def race():
        started, cancel = asyncio.Event(), asyncio.Event()

        async def respond(request):
            started.set()
            await cancel.wait()
            raise asyncio.CancelledError()

        client.app.state.gateway = ModelGatewayClient(
            client.app.state.settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )
        running = asyncio.create_task(service.advance(user.internal_user_id, run.runId))
        await started.wait()
        await service.stop(user.internal_user_id, run.runId, "cancelled", "owner_cancelled")
        cancel.set()
        await running
        await service.advance(user.internal_user_id, run.runId)

    client.portal.call(race)
    spans = _clean_spans(exporter, user, run)
    assert len(spans) == 1
    assert spans[0].attributes["ai4ia.gen_ai.http_attempts"] == 1
    assert spans[0].attributes["ai4ia.gen_ai.usage.coverage"] == "unknown"
    assert spans[0].attributes["error.type"] == "cancelled"
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes
    rows = _rows(client, user.internal_user_id, run.sessionId)
    assert len(rows) == 1 and not rows[0].usageKnown and not rows[0].costKnown
    assert rows[0].workflowDispatchClaimed
