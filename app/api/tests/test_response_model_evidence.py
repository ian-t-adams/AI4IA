"""Provider-reported model identity must survive the real gateway and durable receipt."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ai4ia_api.catalog import _transform_infra_models, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.receipts import MAX_RECEIPT_BYTES, ExecutionReceipt
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from tests.conftest import make_settings
from tests.test_agent_runtime import _assistant_tool_call
from tests.test_model_call_evidence import receipts, transport_client

SOURCE = json.loads((Path(__file__).parents[3] / "infra" / "models.json").read_text())
ROUTER = next(row for row in SOURCE["catalog"] if row["category"] == "router")
MEMBERS = ROUTER["routing"]["models"]


def test_runtime_catalog_preserves_the_exact_router_response_identity_subset():
    packaged = load_catalog().get(ROUTER["name"])
    development = next(
        row for row in _transform_infra_models(SOURCE)["models"] if row["id"] == ROUTER["name"]
    )
    assert packaged is not None
    assert packaged.model_dump().get("routing") == ROUTER["routing"]
    assert development.get("routing") == ROUTER["routing"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_loop", [False, True])
def test_real_router_calls_record_each_selected_model_without_relabeling_the_request(stream, tool_loop):
    def respond(_body, index, result):
        member = MEMBERS[(index - 1) % len(MEMBERS)]
        result["model"] = member["name"] + "-" + member["version"]
        if tool_loop and index == 1:
            result["choices"] = _assistant_tool_call(
                "calculator-1", "calculator", '{"expression":"6*7"}',
            )["choices"]
        return result

    with transport_client(respond=respond) as (client, requests):
        client.app.state.usage._pricing = PricingBook(
            {ROUTER["name"]: PriceRate(2, 8), MEMBERS[0]["name"]: PriceRate(200, 800)},
            currency="USD", version="router-price-snapshot",
        )
        created = client.post("/api/sessions", json={"model": ROUTER["name"]})
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@analyst Calculate 6*7" if tool_loop else "Hello",
            "stream": stream, "allowTools": tool_loop, "allowAutomaticMemory": False,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert saved["runtime"]["modelId"] == ROUTER["name"]
        calls = saved["runtime"]["modelCalls"]
        assert len(requests) == len(calls) == (2 if tool_loop else 1)
        for index, call in enumerate(calls):
            member = MEMBERS[index]
            assert call["modelId"] == ROUTER["name"]
            assert call.get("responseModel") == {
                "status": "reported", "modelId": member["name"], "version": member["version"],
            }
            assert call["cost"]["priceVersion"] == "router-price-snapshot"
            assert call["cost"]["estCostMicroUsd"] == 4000
        if not stream:
            assert response.json()["message"]["executionReceipt"] == saved
        client.app.state.usage._pricing = PricingBook({}, currency="USD", version="later")
        assert receipts(client, sid)[0] == saved
        restored = ExecutionReceipt.model_validate(saved)
        assert restored.model_dump(mode="json")["runtime"]["modelCalls"] == calls
        assert len(json.dumps(restored.model_dump(mode="json")).encode("ascii")) <= MAX_RECEIPT_BYTES
        assert client.get(
            f"/api/sessions/{sid}/messages", headers={"X-Dev-User": "other-owner"},
        ).status_code == 404


@pytest.mark.parametrize(("model", "expected"), [
    (None, "not_reported"),
    (ROUTER["name"], "not_reported"),
    ("unreviewed-private-token", "unrecognized"),
    ("https://provider.invalid/?api-key=private", "unrecognized"),
    ({"api_key": "private"}, "unrecognized"),
])
def test_missing_or_unrecognized_response_models_never_become_guessed_history(model, expected):
    def respond(_body, _index, result):
        if model is not None:
            result["model"] = model
        return result

    with transport_client(respond=respond) as (client, _):
        sid = client.post("/api/sessions", json={"model": ROUTER["name"]}).json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": False, "allowAutomaticMemory": False,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert saved["runtime"]["modelCalls"][0].get("responseModel") == {
            "status": expected, "modelId": None, "version": None,
        }
        assert "private" not in json.dumps(saved)


@pytest.mark.parametrize("conflict", [False, True])
def test_native_sse_identity_handles_route_echoes_and_conflicts_without_last_chunk_wins(conflict):
    def respond(_body, _index, _result):
        members = [MEMBERS[0], MEMBERS[1] if conflict else MEMBERS[0]]
        models = [
            ROUTER["name"],
            *(member["name"] + "-" + member["version"] for member in members),
            members[0]["name"] + "-" + members[0]["version"],
        ]
        frames = [
            {"model": model, "choices": [{"delta": {"content": "answer" if index == 1 else ""}}]}
            for index, model in enumerate(models)
        ]
        text = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    with transport_client(respond=respond) as (client, requests):
        sid = client.post("/api/sessions", json={"model": ROUTER["name"]}).json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": True,
            "allowTools": False, "allowAutomaticMemory": False,
        })
        assert response.status_code == 200, response.text
        assert len(requests) == 1
        actual = receipts(client, sid)[0]["runtime"]["modelCalls"][0].get("responseModel")
        assert actual == (
            {"status": "conflicting", "modelId": None, "version": None} if conflict else
            {"status": "reported", "modelId": MEMBERS[0]["name"], "version": MEMBERS[0]["version"]}
        )


@pytest.mark.parametrize("api", ["responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_native_identity_is_observed_before_adapter_metadata_is_discarded(api, stream):
    entry = next(row for row in load_catalog().models if row.api == api)
    option = entry.options[0]
    version = option.modelVersion
    native_name = entry.id + "-" + version

    def handle(_request):
        if api == "responses":
            result = {"model": native_name, "status": "completed", "output": []}
            events = [{"type": "response.created", "response": result},
                      {"type": "response.completed", "response": result}]
        else:
            result = {"type": "message", "model": native_name, "content": [], "stop_reason": "end_turn"}
            events = [{"type": "message_start", "message": result}, {"type": "message_stop"}]
        return httpx.Response(
            200,
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            headers={"content-type": "text/event-stream"},
        ) if stream else httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(
            claude_enabled=True, claude_external_enabled=True,
        ), http_client=http)
        evidence = ModelCallRecorder(model_id=entry.id, deployment=option.deploymentName)
        arguments = {
            "deployment": option.deploymentName,
            "messages": [{"role": "user", "content": "Hello"}], "api": api,
        }
        if stream:
            async for _ in evidence.observe_stream(gateway.stream(**arguments)):
                pass
        else:
            await evidence.observe(gateway.complete(**arguments))
        assert evidence.snapshot()[0].model_dump()["responseModel"] == {
            "status": "reported", "modelId": entry.id, "version": version,
        }


def test_historical_call_evidence_is_not_reconstructed_from_current_router_members():
    old = ExecutionReceipt.model_validate({
        "runtime": {"modelId": ROUTER["name"], "modelCallCount": 1, "modelCalls": [{
            "iteration": 1, "modelId": ROUTER["name"],
        }]},
    })
    assert old.runtime.modelCalls[0].responseModel is None


def test_bare_model_names_do_not_invent_a_reported_version_and_saved_identity_is_immutable():
    from pydantic import ValidationError
    from ai4ia_api.agents.receipt import ReceiptDraft

    catalog = load_catalog()
    entry = catalog.get(ROUTER["name"])
    evidence = ModelCallRecorder(model_id=entry.id, deployment=entry.options[0].deploymentName)
    call = evidence.start(entry.options[0].deploymentName, "chat")
    call.configure_response_models(catalog, entry.options[0].deploymentName)
    call.response_metadata({"model": MEMBERS[0]["name"]})
    saved = ReceiptDraft(model_evidence=evidence).build()
    recorded = saved.runtime.modelCalls[0].responseModel
    assert recorded.modelId == MEMBERS[0]["name"] and recorded.version is None
    call.response_metadata({"model": MEMBERS[1]["name"]})
    assert call.snapshot().responseModel.status == "conflicting"
    assert saved.runtime.modelCalls[0].responseModel == recorded
    with pytest.raises(ValidationError, match="frozen"):
        recorded.modelId = "changed"


@pytest.mark.parametrize("quota", [False, True])
def test_expanded_response_identity_stays_inside_the_actual_escaped_receipt_budget(quota):
    from ai4ia_api.agents.receipt import ReceiptDraft
    from ai4ia_api.agents.runtime import AgentStep
    from ai4ia_api.model_evidence import MAX_RECORDED_MODEL_CALLS
    from ai4ia_api.hard_quota.models import AdmissionEvidence, Amounts

    catalog = load_catalog()
    entry = catalog.get(ROUTER["name"])
    evidence = ModelCallRecorder(model_id=entry.id, deployment=entry.options[0].deploymentName)
    for index in range(MAX_RECORDED_MODEL_CALLS + 1):
        call = evidence.start(entry.options[0].deploymentName, "chat")
        call.configure_response_models(catalog, entry.options[0].deploymentName)
        member = MEMBERS[index % len(MEMBERS)]
        call.request({"max_tokens": 4096})
        call.response_metadata({"model": member["name"] + "-" + member["version"]})
        if quota:
            call.report_admission(AdmissionEvidence(
                operationHash="a" * 64, surface="chat", phase="dispatched",
                reserved=Amounts(tokens=2**40, microUsd=2**40),
                charged=Amounts(),
            ))
    filler = 'wide "text" \u6c49\u5b57\n' * 500
    receipt = ReceiptDraft(
        model_evidence=evidence,
        prompt_messages=[{"role": "user", "content": filler}] * 40,
    ).build(steps=[
        AgentStep(kind="tool_result", tool="calculator",
                  arguments={"input": filler}, result={"output": filler})
        for _ in range(16)
    ])
    assert receipt.truncated
    assert receipt.runtime.modelCallCount == MAX_RECORDED_MODEL_CALLS + 1
    assert all(call.responseModel.status == "reported" for call in receipt.runtime.modelCalls)
    assert len(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=True).encode("ascii")) <= MAX_RECEIPT_BYTES
