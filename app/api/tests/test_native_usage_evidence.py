"""Numeric provider evidence is descriptive, bounded and never a pricing rule."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import httpx
import pytest

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.receipts import ExecutionReceipt
from ai4ia_api.usage.models import TokenUsage, UsageRecord
from ai4ia_api.usage.native import (
    MAX_NATIVE_USAGE_CALLS, MAX_NATIVE_USAGE_VALUE, NativeUsage, NativeUsageBatch, capture_usage,
)
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from ai4ia_api.workflows.durable import _merge_usage, _usage_from_dict, _usage_to_dict
from tests.conftest import make_settings
from tests.test_model_call_evidence import receipts, transport_client

FIXTURE = json.loads(
    (Path(__file__).parents[2] / "web" / "test-fixtures" / "native_usage_details.json").read_text()
)


def native_response(api, usage, stream):
    if api == "responses":
        response = {"status": "completed", "output": [], "usage": usage}
        events = [{"type": "response.completed", "response": response}]
    else:
        response = {"choices": [{"message": {"content": "Synthetic"}}], "usage": usage}
        events = [{"choices": [{"delta": {"content": "Synthetic"}}]}, {"choices": [], "usage": usage}]
    if not stream:
        return httpx.Response(200, content=json.dumps(response), headers={"content-type": "application/json"})
    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(
        200, text=text + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"},
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
async def test_real_gateway_preserves_only_native_numeric_fields_without_repricing(case, stream):
    requests = []

    def handle(request):
        requests.append(request)
        return native_response(case["api"], case["usage"], stream)

    book = PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="frozen-prices")
    recorder = ModelCallRecorder(model_id="model", deployment="synthetic", pricing=book)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        kwargs = dict(
            deployment="synthetic", api=case["api"],
            messages=[{"role": "user", "content": "Synthetic"}],
        )
        if stream:
            usage = None
            async for chunk in recorder.observe_stream(gateway.stream(**kwargs)):
                if chunk.usage is not None:
                    usage = chunk.usage
        else:
            usage = (await recorder.observe(gateway.complete(**kwargs)))["usage"]
    assert len(requests) == 1
    call = recorder.snapshot()[0]
    observed = call.model_dump(mode="json").get("nativeUsage")
    assert observed == {
        "api": case["api"], "semantics": "provider_reported",
        "status": case["status"], "values": case["values"],
        "invalidFields": case["invalidFields"],
    }
    tokens = TokenUsage.parse(usage)
    serialized = tokens.model_dump(mode="json")
    assert serialized["nativeUsage"] == {
        "calls": [observed], "totalCalls": 1, "truncated": False,
    }
    assert TokenUsage.model_validate_json(tokens.model_dump_json()) == tokens
    assert call.cost.coverage == ("known" if case["costKnown"] else "unknown")
    assert call.cost.priceVersion == "frozen-prices"
    expected = book.estimate(
        "model", prompt_tokens=tokens.prompt, completion_tokens=tokens.completion,
        total_tokens=tokens.total, totals_mismatch=tokens.totalMismatch,
    )
    assert call.cost.estCostMicroUsd == expected.micro_usd
    assert tokens.cacheRead is None and tokens.cacheWrite is None


@pytest.mark.parametrize("value", [None, True, False, -1, 1.0, float("nan"), float("inf"),
                                  2**53, "secret-value", [1], {"password": "secret-value"}])
@pytest.mark.parametrize("api,group", [
    ("chat", "completion_tokens_details"), ("responses", "output_tokens_details"),
])
def test_invalid_numeric_details_are_named_not_coerced_or_copied(value, api, group):
    raw = {group: {"reasoning_tokens": value, "reasoning_content": "private-reasoning"},
           "arbitrary": "private-payload", "nativeUsage": {"values": {"prompt_tokens": 500}}}
    usage = TokenUsage.parse(capture_usage(raw, api=api))
    detail = usage.nativeUsage.calls[0]
    assert detail.status == "invalid" and detail.values == {}
    assert detail.invalidFields == (f"{group}.reasoning_tokens",)
    serialized = usage.model_dump_json()
    for forbidden in ("secret-value", "private-reasoning", "private-payload", "500", "NaN", "Infinity"):
        assert forbidden not in serialized


@pytest.mark.parametrize("value", [0, 1, MAX_NATIVE_USAGE_VALUE])
def test_numeric_bounds_keep_explicit_zero_and_largest_safe_integer(value):
    detail = TokenUsage.parse(capture_usage({
        "completion_tokens_details": {"reasoning_tokens": value},
    }, api="chat")).nativeUsage.calls[0]
    assert detail.values == {"completion_tokens_details.reasoning_tokens": value}
    assert detail.status == "reported" and detail.invalidFields == ()
    assert NativeUsage.model_validate_json(detail.model_dump_json()) == detail


@pytest.mark.parametrize("value", [-1, 2**53, True, float("nan"), float("inf"), "private"])
@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_real_wire_rejects_bad_detail_values_without_retaining_payloads(value, api, stream):
    raw = {
        **({"prompt_tokens": 22, "completion_tokens": 5} if api == "chat" else {"input_tokens": 22, "output_tokens": 5}),
        "total_tokens": 82,
        ("completion_tokens_details" if api == "chat" else "output_tokens_details"): {
            "reasoning_tokens": value, "reasoning_content": "private-reasoning",
        },
    }
    recorder = ModelCallRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: native_response(api, raw, stream),
    )) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        kwargs = dict(deployment="synthetic", api=api, messages=[])
        if stream:
            async for _ in recorder.observe_stream(gateway.stream(**kwargs)):
                pass
        else:
            await recorder.observe(gateway.complete(**kwargs))
    saved = recorder.snapshot()[0]
    field = "completion_tokens_details.reasoning_tokens" if api == "chat" else "output_tokens_details.reasoning_tokens"
    assert saved.nativeUsage.invalidFields == (field,)
    assert field not in saved.nativeUsage.values and saved.nativeUsage.status == "partial"
    assert saved.cost.coverage == "unknown" and saved.cost.estCostMicroUsd is None
    assert "private" not in saved.model_dump_json()
    assert "NaN" not in saved.model_dump_json() and "Infinity" not in saved.model_dump_json()


@pytest.mark.parametrize("raw,status", [({}, "unreported"), ([], "invalid"), ("private", "invalid")])
@pytest.mark.parametrize("stream", [False, True])
async def test_empty_or_malformed_usage_never_becomes_a_known_zero(raw, status, stream):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: native_response("chat", raw, stream),
    )) as http:
        recorder = ModelCallRecorder()
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        kwargs = dict(deployment="synthetic", messages=[])
        if stream:
            async for _ in recorder.observe_stream(gateway.stream(**kwargs)):
                pass
        else:
            await recorder.observe(gateway.complete(**kwargs))
    call = recorder.snapshot()[0]
    assert not call.usageKnown and call.promptTokens is None
    assert call.cost.coverage == "unknown" and call.cost.estCostMicroUsd is None
    assert call.nativeUsage.status == status
    assert call.nativeUsage.values == {} and call.nativeUsage.invalidFields == ()
    assert "private" not in call.model_dump_json()


def test_malformed_scalar_preserves_other_valid_native_fields_without_becoming_price_evidence():
    recorder = ModelCallRecorder()
    call = recorder.start("synthetic", "chat")
    call.report_usage(capture_usage({
        "prompt_tokens": "private", "completion_tokens": 5, "total_tokens": 82,
        "completion_tokens_details": {"reasoning_tokens": 55},
    }, api="chat"), completed=True)
    saved = call.snapshot()
    assert not saved.usageKnown and saved.cost.coverage == "unknown"
    assert saved.nativeUsage.values == {
        "completion_tokens": 5, "total_tokens": 82, "completion_tokens_details.reasoning_tokens": 55,
    }
    assert saved.nativeUsage.invalidFields == ("prompt_tokens",)
    assert "private" not in saved.model_dump_json()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("api,model", [("chat", "grok-4.7"), ("responses", "gpt-5.4-pro")])
def test_actual_api_receipt_and_ledger_keep_original_field_names_and_immutable_history(stream, api, model):
    case = next(row for row in FIXTURE["cases"] if row["api"] == api)

    def respond(_body, _index, _result):
        return native_response(api, case["usage"], stream)

    with transport_client(respond=respond) as (client, _):
        state = client.app.state
        state.usage._pricing = PricingBook({model: PriceRate(2, 8)}, currency="USD", version="original")
        created = client.post("/api/sessions", json={"model": model})
        assert created.status_code == 201, created.text
        sid, owner = created.json()["id"], created.json()["userId"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Synthetic", "stream": stream,
            "allowTools": False, "allowAutomaticMemory": False,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        native = saved["runtime"]["modelCalls"][0]["nativeUsage"]
        assert native["api"] == api and native["semantics"] == "provider_reported"
        assert native["values"] == case["values"]
        assert saved["usage"]["cost"]["coverage"] == "unknown"
        assert ExecutionReceipt.model_validate_json(json.dumps(saved)).model_dump(mode="json") == saved
        rows = client.portal.call(partial(state.usage._repo.list_for_session, owner, sid, limit=10))
        assert len(rows) == 1 and rows[0].nativeUsage.calls[0].model_dump(mode="json") == native
        state.usage._pricing = PricingBook({}, currency="USD", version="later")
        assert receipts(client, sid)[0] == saved
        assert client.get(f"/api/sessions/{sid}/messages", headers={"X-Dev-User": "other"}).status_code == 404


@pytest.mark.parametrize("content", ["Synthetic", "@analyst Synthetic"])
@pytest.mark.parametrize("final", ["replaced", "empty", "invalid"])
def test_actual_stream_uses_latest_usage_snapshot_not_summed_or_inherited_details(content, final):
    latest = (
        {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
         "completion_tokens_details": {"reasoning_tokens": 2}}
        if final == "replaced" else {} if final == "empty" else "private"
    )

    def respond(_body, _index, _result):
        events = [
            {"choices": [{"delta": {"content": "Synthetic"}}]},
            {"choices": [], "usage": {"prompt_tokens": 22, "completion_tokens": 5, "total_tokens": 82,
                                     "prompt_tokens_details": {"cached_tokens": 6}}},
            {"choices": [], "usage": latest},
            {"choices": [], "usage": None},
        ]
        return httpx.Response(
            200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    with transport_client(respond=respond) as (client, requests):
        created = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        sid = created["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": content, "stream": True, "allowAutomaticMemory": False,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        call = saved["runtime"]["modelCalls"][0]
        assert len(requests) == saved["usage"]["calls"] == 1
        expected = TokenUsage.parse(capture_usage(latest, api="chat"))
        assert call["nativeUsage"] == expected.nativeUsage.calls[0].model_dump(mode="json")
        assert "prompt_tokens_details.cached_tokens" not in call["nativeUsage"]["values"]
        assert saved["usage"]["totalTokens"] == (10 if final == "replaced" else None)
        rows = client.portal.call(partial(
            client.app.state.usage._repo.list_for_session, created["userId"], sid, limit=10,
        ))
        assert rows[0].nativeUsage == expected.nativeUsage
        assert rows[0].usageKnown is (final == "replaced")


def test_call_prefix_keeps_unknown_history_and_api_semantics_through_durable_continuation():
    first = TokenUsage.parse({"prompt_tokens": 1, "completion_tokens": 1})
    assert first.nativeUsage is None and "nativeUsage" not in first.model_dump()
    combined = first
    expected = [None]
    for index in range(MAX_NATIVE_USAGE_CALLS + 2):
        api = "chat" if index % 2 == 0 else "responses"
        raw = FIXTURE["cases"][0 if api == "chat" else 2]["usage"]
        if api == "responses":
            from ai4ia_api.gateway.client import _responses_usage_to_chat

            usage = TokenUsage.parse(_responses_usage_to_chat(raw))
        else:
            usage = TokenUsage.parse(capture_usage(raw, api=api))
        expected.append(usage.nativeUsage.calls[0])
        combined = combined.add(usage)
    assert combined.nativeUsage.calls == tuple(expected[:MAX_NATIVE_USAGE_CALLS])
    assert combined.nativeUsage.truncated
    assert combined.nativeUsage.totalCalls == combined.calls == MAX_NATIVE_USAGE_CALLS + 3
    restored = _usage_from_dict(_merge_usage({}, _usage_to_dict(combined)))
    assert restored == combined
    assert TokenUsage.model_validate_json(combined.model_dump_json()) == combined
    assert restored.add(TokenUsage.empty()).nativeUsage == combined.nativeUsage


def test_native_payload_cannot_be_injected_through_provider_keys_or_backfilled_in_history():
    raw = {
        "prompt_tokens": 22, "completion_tokens": 5, "total_tokens": 82,
        "nativeUsage": {"api": "responses", "values": {"output_tokens": 60}},
    }
    assert TokenUsage.parse(raw).nativeUsage is None
    captured = TokenUsage.parse(capture_usage(raw, api="chat"))
    assert captured.nativeUsage.calls[0].values == {
        "prompt_tokens": 22, "completion_tokens": 5, "total_tokens": 82,
    }
    restored = UsageRecord.model_validate({
        "userId": "owner", "sessionId": "session", "model": "grok-4.7",
        "promptTokens": 22, "completionTokens": 5, "totalTokens": 82,
        "costKnown": True, "estCostMicroUsd": 74,
    })
    assert "nativeUsage" not in restored.model_dump(mode="json")
    assert restored.costKnown and restored.estCostMicroUsd == 74
    with pytest.raises(ValueError, match="bounded call prefix"):
        NativeUsageBatch(calls=(), totalCalls=9, truncated=True)
    with pytest.raises(ValueError, match="API"):
        NativeUsage(api="responses", status="reported", values={"prompt_tokens": 2})


@pytest.mark.parametrize("stream", [False, True])
def test_tool_loop_keeps_each_calls_native_fields_and_only_prices_reconciled_calls(stream):
    from tests.test_agent_runtime import _assistant_tool_call

    first, last = FIXTURE["cases"][:2]

    def respond(_body, index, result):
        result["usage"] = first["usage"] if index == 1 else last["usage"]
        if index == 1:
            result["choices"] = _assistant_tool_call("calc", "calculator", '{"expression":"6*7"}')["choices"]
        return result

    with transport_client(respond=respond) as (client, requests):
        created = client.post("/api/sessions", json={"model": "gpt-5.2"}).json()
        sid = created["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@analyst Calculate", "stream": stream,
            "allowAutomaticMemory": False,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert len(requests) == saved["toolCallCount"] + 1 == 2
        calls = saved["runtime"]["modelCalls"]
        assert [call["nativeUsage"]["values"] for call in calls] == [first["values"], last["values"]]
        assert saved["usage"]["cost"]["coverage"] == "partial"
        assert saved["usage"]["cost"]["pricedCalls"] == 1
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 32 * 2 + 103 * 8
        rows = client.portal.call(partial(
            client.app.state.usage._repo.list_for_session, created["userId"], sid, limit=10,
        ))
        assert len(rows) == 1
        assert [item.values for item in rows[0].nativeUsage.calls] == [first["values"], last["values"]]
        assert rows[0].nativeUsage.totalCalls == 2 and not rows[0].nativeUsage.truncated


async def test_actual_checkpoint_resume_keeps_native_evidence_without_replaying_or_backfilling():
    from ai4ia_api.agents.approvals import approval_key, arguments_digest
    from ai4ia_api.agents.runtime import AgentRunPaused, run_agent_turn
    from ai4ia_api.agents.tool_exec import ToolContext
    from tests.test_agent_continuation import StoredController, setup_calls
    from tests.test_agent_runtime import _messages

    registry, executor, sent, first, final = setup_calls()
    first["usage"] = FIXTURE["cases"][0]["usage"]
    final["usage"] = FIXTURE["cases"][1]["usage"]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=first if len(requests) == 1 else final)

    def recorder():
        return ModelCallRecorder(
            model_id="model", deployment="synthetic",
            pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="original"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        controller = StoredController()
        kwargs = dict(
            deployment="synthetic", messages=_messages(), tool_names=["calculator", "send"],
            registry=registry, executor=executor, max_iters=2, gateway=gateway,
        )
        with pytest.raises(AgentRunPaused):
            await run_agent_turn(
                **kwargs, ctx=ToolContext(turn_budgets={}), checkpoint=controller, model_evidence=recorder(),
            )
        restored = StoredController(controller.serialized)
        original = restored.restored.modelEvidence[0]
        assert original.nativeUsage.values == FIXTURE["cases"][0]["values"]
        assert restored.restored.usage.nativeUsage.calls[0] == original.nativeUsage
        key = approval_key("send", arguments_digest({"text": "hello"}))
        resumed = await run_agent_turn(
            **kwargs, checkpoint=restored, model_evidence=recorder(),
            ctx=ToolContext(invocation_approvals=frozenset({key}), turn_budgets={}),
        )
    assert len(requests) == 2 and sent == ["hello"]
    assert resumed.model_evidence.snapshot()[0] == original
    assert [item.values for item in resumed.usage.nativeUsage.calls] == [
        FIXTURE["cases"][0]["values"], FIXTURE["cases"][1]["values"],
    ]
    assert resumed.segment_usage.nativeUsage.totalCalls == 1
    assert resumed.segment_usage.nativeUsage.calls[0].values == FIXTURE["cases"][1]["values"]


async def test_outbox_ack_binds_native_details_that_are_not_in_the_compatibility_usage_dict():
    from types import SimpleNamespace

    from ai4ia_api.gateway.client import _responses_usage_to_chat
    from ai4ia_api.workflows.automation_common import AutomationError
    from ai4ia_api.workflows.automation_models import EffectIntent
    from ai4ia_api.workflows.automation_service import WorkflowAutomationService
    from ai4ia_api.workflows.automation_store import InMemoryAutomationStore
    from ai4ia_api.workflows.run_controller import RunController
    from tests.test_workflow_monetary_ledger import NOW, RUN, funded
    from tests.test_workflow_usage_once import record

    store = InMemoryAutomationStore()
    value = funded(store, 100, legacy=True)
    value.effects["one"] = EffectIntent(
        id="one", runId=RUN, sessionId="session", operationId="operation",
        category="dispatch", payloadDigest="a" * 64, state="dispatched",
        startedAt=NOW, resultDigest=None, delivered=False, usage=record(),
    )
    assert await store.create_owner(value)
    controller = object.__new__(RunController)
    controller.service = WorkflowAutomationService(None, store, None)
    controller.current = SimpleNamespace(runId=RUN)
    controller.owner_id = "owner"
    first = _responses_usage_to_chat({
        "input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
        "input_tokens_details": {"cached_tokens": 1},
    })
    changed = _responses_usage_to_chat({
        "input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
        "input_tokens_details": {"cached_tokens": 2},
    })
    assert first == changed and first.native != changed.native
    await controller.after_dispatch("one", usage=first, completed=True, outcome="complete")
    await controller.after_dispatch("one", usage=first, completed=True, outcome="complete")
    with pytest.raises(AutomationError, match="already recorded differently"):
        await controller.after_dispatch("one", usage=changed, completed=True, outcome="complete")
    saved = (await store.read_owner("owner")).value.effects["one"].usage
    assert saved.nativeUsage.calls[0].values["input_tokens_details.cached_tokens"] == 1
