"""Receipt evidence must originate at the adapted HTTP request, not the UI draft."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.agents.receipt import ReceiptDraft
from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.main import create_app
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.receipts import MAX_RECEIPT_BYTES, ExecutionReceipt
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from tests.conftest import make_settings


def transport_response(body, result):
    if body.get("stream"):
        delta = dict(result["choices"][0]["message"])
        delta.pop("role", None)
        if delta.get("tool_calls"):
            delta["tool_calls"] = [
                {**call, "index": index} for index, call in enumerate(delta["tool_calls"])
            ]
        frames = [{"choices": [{"delta": delta}]}]
        if "usage" in result:
            frames.append({"choices": [], "usage": result["usage"]})
        text = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(
            200, text=text + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )
    return httpx.Response(200, json=result)


@contextmanager
def transport_client(*, usage=True, priced=True, fail=False, respond=None):
    settings = make_settings()
    app = create_app(settings)
    requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if fail:
            return httpx.Response(502, text="provider body password: do-not-persist")
        result = {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}
        if usage:
            result["usage"] = {
                "prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250,
            }
        if respond is not None:
            result = respond(body, len(requests), result)
        return result if isinstance(result, httpx.Response) else transport_response(body, result)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with TestClient(app) as client:
            app.state.gateway = ModelGatewayClient(settings, http_client=http)
            app.state.usage._pricing = PricingBook(
                {"gpt-5.2": PriceRate(2, 8)} if priced else {},
                currency="USD", version="receipt-test-v1",
            )
            yield client, requests
    finally:
        asyncio.run(http.aclose())


def session(client):
    response = client.post("/api/sessions", json={"title": "Evidence", "model": "gpt-5.2"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def receipts(client, sid):
    response = client.get(f"/api/sessions/{sid}/messages")
    assert response.status_code == 200, response.text
    return [
        row["executionReceipt"] for row in response.json()
        if row["role"] == "assistant"
    ]


@pytest.mark.parametrize("stream", [False, True])
def test_receipt_records_the_adapted_request_and_original_price(stream):
    with transport_client() as (client, requests):
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": stream,
            "params": {
                "temperature": 0.3, "top_p": 0.4, "max_tokens": 2_000_000,
                "reasoning_effort": "medium",
            },
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        call = saved["runtime"]["modelCalls"][0]
        wire = requests[0]

        assert call["coverage"] == "recorded"
        assert call["modelSource"] == "session"
        assert call["parameterSource"] == "request"
        assert call["parameters"]["maxOutputTokens"] == wire["max_completion_tokens"]
        assert wire["max_completion_tokens"] < 2_000_000
        assert call["parameters"]["outputTokenField"] == "max_completion_tokens"
        assert "temperature" not in wire and "top_p" not in wire
        assert call["parameters"]["temperature"] is None
        assert call["parameters"]["topP"] is None
        assert call["parameters"]["reasoningEffort"] == wire["reasoning_effort"]
        assert call["cost"]["priceVersion"] == "receipt-test-v1"
        assert call["cost"]["priceInputPer1M"] == 2
        assert call["cost"]["priceOutputPer1M"] == 8
        assert saved["usage"]["cost"]["coverage"] == "known"
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 4000


def test_later_session_and_price_changes_do_not_reinterpret_a_receipt():
    with transport_client() as (client, requests):
        sid = session(client)
        first = client.post("/api/chat", json={
            "sessionId": sid, "content": "First", "stream": False,
            "params": {"max_tokens": 2048, "reasoning_effort": "low"},
        })
        assert first.status_code == 200, first.text
        before = receipts(client, sid)[0]
        assert before["usage"]["cost"]["priceVersions"] == ["receipt-test-v1"]
        client.app.state.usage._pricing = PricingBook(
            {"gpt-5.2": PriceRate(4, 16)}, currency="USD", version="receipt-test-v2",
        )
        update = client.patch(f"/api/sessions/{sid}", json={"systemPrompt": "Be brief."})
        assert update.status_code == 200, update.text
        second = client.post("/api/chat", json={
            "sessionId": sid, "content": "Second", "stream": False,
            "params": {"max_tokens": 4096, "reasoning_effort": "high"},
        })
        assert second.status_code == 200, second.text
        after = receipts(client, sid)
        assert requests[0]["max_completion_tokens"] != requests[1]["max_completion_tokens"]
        assert after[0] == before
        assert after[1]["usage"]["cost"]["estCostMicroUsd"] == 8000
        assert after[1]["usage"]["cost"]["priceVersions"] == ["receipt-test-v2"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("usage,priced", [(False, True), (True, False), (False, False)])
def test_missing_usage_or_price_is_unknown_not_zero(stream, usage, priced):
    with transport_client(usage=usage, priced=priced) as (client, _):
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": stream,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert saved["runtime"]["modelCalls"][0]["coverage"] == "recorded"
        assert saved["usage"]["cost"]["coverage"] == "unknown"
        assert saved["usage"]["cost"]["estCostMicroUsd"] is None
        assert saved["usage"]["cost"]["pricedCalls"] == 0
        assert saved["usage"]["cost"]["totalCalls"] == 1


@pytest.mark.parametrize("raw", [
    None, {}, {"total_tokens": 10}, {"prompt_tokens": 10},
    {"prompt_tokens": True, "completion_tokens": 1},
    {"prompt_tokens": -1, "completion_tokens": 1},
    {"prompt_tokens": "invalid", "completion_tokens": 1},
])
def test_malformed_or_one_sided_usage_cannot_fabricate_free_cost(raw):
    def respond(_body, _index, result):
        result["usage"] = raw
        return result

    with transport_client(respond=respond) as (client, _):
        sid = session(client)
        assert client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": False,
        }).status_code == 200
        assert receipts(client, sid)[0]["usage"]["cost"]["estCostMicroUsd"] is None


def test_explicitly_reported_zero_usage_has_a_known_zero_estimate():
    def respond(_body, _index, result):
        result["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return result

    with transport_client(respond=respond) as (client, _):
        sid = session(client)
        assert client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": False,
        }).status_code == 200
        cost = receipts(client, sid)[0]["usage"]["cost"]
        assert cost["coverage"] == "known"
        assert cost["estCostMicroUsd"] == 0


def test_price_snapshot_precedes_provider_await():
    def respond(_body, _index, result):
        book = client.app.state.usage._pricing
        book._rates["gpt-5.2"] = PriceRate(200, 800)
        book._version = "changed-during-call"
        return result

    with transport_client(respond=respond) as (client, _):
        sid = session(client)
        assert client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": False,
        }).status_code == 200
        saved = receipts(client, sid)[0]
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 4000
        assert saved["runtime"]["modelCalls"][0]["cost"]["priceVersion"] == "receipt-test-v1"
        assert client.app.state.usage._pricing.version == "changed-during-call"


@pytest.mark.parametrize("stream", [False, True])
def test_first_transport_failure_keeps_parameters_and_unknown_cost_without_exception_body(stream):
    with transport_client(fail=True) as (client, _):
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": stream,
            "params": {"max_tokens": 2048},
        })
        assert response.status_code == (200 if stream else 502), response.text
        saved = receipts(client, sid)[0]
        assert saved["status"] == "error" and saved["partial"]
        call = saved["runtime"]["modelCalls"][0]
        assert call["parameters"]["maxOutputTokens"] == 2048
        assert not call["providerCompleted"]
        assert saved["usage"]["cost"]["estCostMicroUsd"] is None
        assert "do-not-persist" not in json.dumps(saved)


@pytest.mark.parametrize("stream", [False, True])
def test_agent_tool_loop_records_each_real_call_and_partial_cost(stream):
    from tests.test_agent_runtime import _assistant_tool_call

    def respond(_body, index, result):
        if index == 1:
            result["choices"] = _assistant_tool_call(
                "calc-1", "calculator", '{"expression":"6*7"}',
            )["choices"]
        else:
            result.pop("usage")
        return result

    with transport_client(respond=respond) as (client, requests):
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@analyst Calculate 6*7", "stream": stream,
            "params": {"max_tokens": 2048},
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        calls = saved["runtime"]["modelCalls"]
        assert len(requests) == len(calls) == saved["runtime"]["modelCallCount"] == 2
        assert saved["toolCallCount"] == 1
        for call, body in zip(calls, requests, strict=True):
            assert call["parameters"]["toolChoice"] == body["tool_choice"] == "auto"
            assert call["parameters"]["maxOutputTokens"] == body["max_completion_tokens"]
        assert saved["usage"]["cost"]["coverage"] == "partial"
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 4000
        assert saved["usage"]["cost"]["pricedCalls"] == 1
        assert saved["usage"]["cost"]["totalCalls"] == 2


async def test_final_no_tool_call_has_its_own_effective_parameter_posture():
    from tests.test_agent_runtime import _assistant_tool_call

    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        result = (
            _assistant_tool_call("calc", "calculator", '{"expression":"6*7"}')
            if len(requests) == 1 else {"choices": [{"message": {"content": "42"}}]}
        )
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        registry, executor = build_tools()
        evidence = ModelCallRecorder()
        run = await run_agent_turn(
            deployment="dep", messages=[{"role": "user", "content": "Calculate"}],
            tool_names=["calculator"], gateway=gateway, registry=registry, executor=executor,
            ctx=ToolContext(), params={"max_tokens": 100}, max_iters=1,
            model_evidence=evidence,
        )
        assert run.iterations == len(requests) == 2
        calls = evidence.snapshot()
        assert calls[0].parameters.toolChoice == requests[0]["tool_choice"] == "auto"
        assert "tool_choice" not in requests[1]
        assert calls[1].parameters.toolChoice is None
        assert calls[1].parameters.maxOutputTokens == requests[1]["max_tokens"] == 100


@pytest.mark.parametrize("stream", [False, True])
def test_delegation_never_invents_parent_parameters_and_cost_is_not_double_counted(stream):
    from tests.test_agent_runtime import _assistant_tool_call
    from tests.test_chat_orchestration_api import _setup_agents

    def respond(body, _index, result):
        if not any(message["role"] == "tool" for message in body["messages"]) and (
            "helper" not in body["messages"][0]["content"].lower()
        ):
            result["choices"] = _assistant_tool_call(
                "delegate", "delegate_to_agent", '{"agent":"helper","task":"Calculate"}',
            )["choices"]
        return result

    with transport_client(respond=respond) as (client, requests):
        _setup_agents(client)
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "@boss Delegate this", "stream": stream,
            "params": {"max_tokens": 2048, "reasoning_effort": "high"},
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert len(requests) == 3
        assert saved["runtime"]["modelCallCount"] == 2
        child = saved["delegations"][0]
        assert child["runtime"]["agent"] == "helper"
        assert child["runtime"]["agentConfigSha256"]
        assert child["runtime"]["instructionSha256"]
        call = child["runtime"]["modelCalls"][0]
        assert call["modelSource"] == "supervisor"
        assert call["parameterSource"] == "delegation_default"
        assert call["requestOverrides"] == []
        assert call["parameters"]["maxOutputTokens"] is None
        assert call["parameters"]["reasoningEffort"] is None
        assert "max_completion_tokens" not in requests[1]
        assert "reasoning_effort" not in requests[1]
        assert child["usage"]["cost"]["estCostMicroUsd"] == 4000
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 12000
        assert saved["usage"]["cost"]["totalCalls"] == 3


@pytest.mark.parametrize("fail_last", [False, True])
def test_workflow_steps_keep_independent_defaults_prices_and_aggregate_cost(fail_last):
    from tests.test_workflows_api import _mk_agent, _wf_body

    def respond(_body, index, result):
        return httpx.Response(502, text="provider failure") if fail_last and index == 2 else result

    with transport_client(respond=respond) as (client, requests):
        for name in ("drafter", "editor"):
            assert _mk_agent(client, name).status_code == 201
        assert client.post("/api/workflows", json=_wf_body()).status_code == 201
        sid = session(client)
        response = client.post("/api/workflows/summarize/run", json={
            "sessionId": sid, "input": "Hello",
        })
        assert response.status_code == 200, response.text
        message = response.json()["message"]
        assert message["workflowRunFingerprint"]
        assert len(requests) == 2
        assert message["executionReceipt"]["runtime"]["workflowConfigSha256"]
        for index, (step, body) in enumerate(zip(message["workflowStepReceipts"], requests, strict=True)):
            call = step["runtime"]["modelCalls"][0]
            assert call["coverage"] == "recorded"
            assert call["modelSource"] == "workflow"
            assert call["parameterSource"] == "workflow_default"
            assert call["parameters"]["maxOutputTokens"] is None
            assert "max_completion_tokens" not in body
            assert step["usage"]["cost"]["estCostMicroUsd"] == (
                None if fail_last and index == 1 else 4000
            )
            assert step["runtime"]["instructionSha256"]
        assert message["executionReceipt"]["runtime"]["modelCalls"] is None
        cost = message["executionReceipt"]["usage"]["cost"]
        assert cost["estCostMicroUsd"] == (4000 if fail_last else 8000)
        assert cost["coverage"] == ("partial" if fail_last else "known")
        assert cost["totalCalls"] == 2


async def test_allowlist_drops_unsafe_values_but_preserves_valid_controls():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    )) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        evidence = ModelCallRecorder()
        params = {
            "temperature": "password: secret-value", "top_p": 1.1,
            "max_tokens": 50, "reasoning_effort": "https://unsafe.test/?api_key=private",
            "api_key": "raw-private-key", "thinking": {"text": "hidden text"},
            "arbitrary": {"unvalidated": True},
        }
        await evidence.observe(gateway.complete(
            deployment="dep", messages=[{"role": "user", "content": "Hello"}], params=params,
        ))
        call = evidence.snapshot()[0]
        assert call.coverage == "partial"
        assert call.parameters.maxOutputTokens == 50
        assert call.parameters.temperature is None
        assert call.parameters.topP is None
        assert call.parameters.reasoningEffort is None
        serialized = call.model_dump_json()
        for forbidden in ("secret-value", "unsafe.test", "raw-private-key", "hidden text", "arbitrary"):
            assert forbidden not in serialized


def test_historical_receipts_do_not_receive_new_evidence():
    restored = ExecutionReceipt.model_validate({
        "version": 1, "runtime": {"modelId": "old-model"},
        "usage": {"known": True, "complete": True, "calls": 1, "totalTokens": 10},
    })
    assert restored.runtime.modelCalls is None
    assert restored.runtime.modelCallCount is None
    assert restored.usage.cost is None


def test_receipt_owner_authorization_is_unchanged_with_new_fields():
    with transport_client() as (client, _):
        sid = session(client)
        assert client.post("/api/chat", json={
            "sessionId": sid, "content": "Private", "stream": False,
        }).status_code == 200
        assert receipts(client, sid)[0]["runtime"]["modelCalls"]
        denied = client.get(
            f"/api/sessions/{sid}/messages", headers={"X-Dev-User": "different-owner"},
        )
        assert denied.status_code == 404
        assert "Private" not in denied.text and "modelCalls" not in denied.text


@pytest.mark.parametrize("api", ["responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_adapters_capture_only_their_effective_controls(api, stream):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if api == "responses":
            result = {
                "id": "response", "status": "completed", "output": [],
                "usage": {"input_tokens": 1000, "output_tokens": 250, "total_tokens": 1250},
            }
            events = [{"type": "response.completed", "response": result}]
        else:
            result = {
                "type": "message", "content": [{"type": "text", "text": "Done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 250},
            }
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 1000}}},
                {"type": "message_delta", "usage": {"output_tokens": 250},
                 "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
        if stream:
            return httpx.Response(
                200, text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        evidence = ModelCallRecorder(
            model_id="model", deployment="deployment",
            pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="adapter-v1"),
        )
        kwargs = dict(
            deployment="deployment", messages=[{"role": "user", "content": "Hello"}],
            params={"max_tokens": 777, "temperature": 0.7, "top_p": 0.9, "reasoning_effort": "medium"},
            api=api,
        )
        if stream:
            async for _ in evidence.observe_stream(gateway.stream(**kwargs)):
                pass
        else:
            await evidence.observe(gateway.complete(**kwargs))
        recorded = evidence.snapshot()[0]
        key = "max_output_tokens" if api == "responses" else "max_tokens"
        assert recorded.parameters.outputTokenField == key
        assert recorded.parameters.maxOutputTokens == requests[0][key]
        assert recorded.parameters.maxOutputTokens == (16384 if api == "responses" else 777)
        assert recorded.parameters.temperature is None and recorded.parameters.topP is None
        assert recorded.parameters.reasoningEffort == ("medium" if api == "responses" else None)
        assert recorded.cost.estCostMicroUsd == 4000
        assert recorded.cost.priceVersion == "adapter-v1"
        assert recorded.providerInternals == "unknown"


@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_disconnect_retains_cancelled_delegation_evidence(cancel):
    from ai4ia_api.agents.agent_catalog import AgentCatalog, AgentSpec
    from ai4ia_api.agents.orchestration import build_delegate_capability
    from ai4ia_api.receipts import ReceiptRuntime
    from tests.test_agent_runtime import _assistant_tool_call
    from tests.test_chat_stream_protocol import _PersistingRepo, _test_agentic_stream

    child_started = asyncio.Event()
    block_child = asyncio.Event()
    book = PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="cancellation-v1")
    registry, executor = build_tools()
    boss = AgentSpec(
        name="boss", displayName="Boss", description="Supervisor",
        systemPrompt="Supervisor", links=["helper"],
    )
    helper = AgentSpec(
        name="helper", displayName="Helper", description="Helper", systemPrompt="Helper",
    )
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["messages"][0]["content"] == "Helper":
            child_started.set()
            if cancel:
                await block_child.wait()
        result = {"choices": [{"message": {"content": "Done"}}]}
        if len(requests) == 1:
            result = _assistant_tool_call(
                "delegate", "delegate_to_agent", '{"agent":"helper","task":"Calculate"}',
            )
        result["usage"] = {"prompt_tokens": 1000, "completion_tokens": 250}
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        evidence = ModelCallRecorder(
            model_id="model", deployment="deployment", pricing=book,
            model_source="session", parameter_source="request", overrides=("max_tokens",),
        )
        tools, handlers, usage_sink = build_delegate_capability(
            orchestrator=boss, composed=AgentCatalog(agents=[boss, helper]),
            gateway=gateway, registry=registry, executor=executor, deployment="deployment",
            model_id="model", pricing=book,
        )

        async def run(on_step):
            return await run_agent_turn(
                deployment="deployment",
                messages=[{"role": "system", "content": "Supervisor"},
                          {"role": "user", "content": "Delegate"}],
                tool_names=[], gateway=gateway, registry=registry, executor=executor,
                ctx=ToolContext(), params={"max_tokens": 2048}, on_step=on_step,
                extra_tools=tools, extra_handlers=handlers, model_evidence=evidence,
            )

        repo = _PersistingRepo()
        stream = _test_agentic_stream(
            run=run, repo=repo, extra_usage=usage_sink,
            receipt_draft=ReceiptDraft(
                runtime=ReceiptRuntime(modelId="model", agent="boss"), model_evidence=evidence,
            ),
        )
        assert "metadata" in await anext(stream)
        if cancel:
            await asyncio.wait_for(child_started.wait(), timeout=2)
            await asyncio.wait_for(stream.aclose(), timeout=2)
        else:
            async for _ in stream:
                pass
        saved = repo.persisted[-1].executionReceipt
        assert saved is not None
        assert saved.status == ("cancelled" if cancel else "complete")
        child = saved.delegations[0]
        assert child.status == ("cancelled" if cancel else "complete")
        call = child.runtime.modelCalls[0]
        assert call.parameterSource == "delegation_default"
        assert call.parameters.maxOutputTokens is None
        assert saved.runtime.modelCalls[0].parameters.maxOutputTokens == 2048
        assert saved.usage.cost.coverage == ("partial" if cancel else "known")
        assert saved.usage.cost.estCostMicroUsd == (4000 if cancel else 12000)
        assert saved.usage.cost.totalCalls == (2 if cancel else 3)
        assert child.usage.cost.estCostMicroUsd == (None if cancel else 4000)


@pytest.mark.parametrize("content", ["Hello", "@analyst Hello"])
def test_nonstream_cancellation_keeps_the_accepted_turn_receipt(content):
    def respond(_body, _index, _result):
        raise asyncio.CancelledError()

    with transport_client(respond=respond) as (client, _):
        sid = session(client)
        # Starlette's middleware reports the cancelled ASGI response this way;
        # the cancellation itself must still be persisted by the chat boundary.
        with pytest.raises(RuntimeError, match="No response returned"):
            client.post("/api/chat", json={
                "sessionId": sid, "content": content, "stream": False,
                "params": {"max_tokens": 2048},
            })
        saved = receipts(client, sid)[0]
        assert saved["status"] == "cancelled"
        assert saved["runtime"]["modelCalls"][0]["parameters"]["maxOutputTokens"] == 2048
        assert saved["usage"]["cost"]["coverage"] == "unknown"
        assert saved["usage"]["cost"]["estCostMicroUsd"] is None


@pytest.mark.parametrize("quota", [False, True])
def test_full_receipt_remains_bounded_with_all_evidence_lists_populated(quota):
    from ai4ia_api.agents.runtime import AgentStep
    from ai4ia_api.model_evidence import MAX_RECORDED_MODEL_CALLS
    from ai4ia_api.receipts import ReceiptRuntime
    from ai4ia_api.hard_quota.models import AdmissionEvidence, Amounts

    evidence = ModelCallRecorder(
        model_id="model", deployment="deployment",
        pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="bounds-v1"),
    )
    for _ in range(MAX_RECORDED_MODEL_CALLS + 1):
        call = evidence.start("deployment", "responses")
        call.request({"max_output_tokens": 16384, "reasoning": {"effort": "high"}})
        call.report_usage({"prompt_tokens": 1000, "completion_tokens": 250}, completed=True)
        if quota:
            for index in range(2):
                call.report_admission(AdmissionEvidence(
                    operationHash=f"{index + 1:064x}", surface="chat", phase="settled",
                    reserved=Amounts(tokens=2**40, microUsd=2**40),
                    charged=Amounts(tokens=1250, microUsd=4000),
                    priceVersion="v" * 96, attemptVersion="a" * 96,
                ))
    filler = "quoted text \u6c49\u5b57 \"\n" * 150
    messages = [{"role": "user", "content": filler} for _ in range(40)]
    draft = ReceiptDraft(
        runtime=ReceiptRuntime(modelId="model"), model_evidence=evidence,
        prompt_messages=messages,
        blocks=[(f"block-{i}", filler, True) for i in range(12)],
        offered=[{
            "type": "function", "function": {
                "name": f"tool_{i}", "description": filler, "parameters": {"type": "object"},
            },
        } for i in range(64)],
    )
    saved = draft.build(
        steps=[AgentStep(kind="tool_result", tool="calculator",
                         arguments={"input": filler}, result={"output": filler}) for _ in range(16)],
        model_requests=[messages] * 8,
    )
    assert len(json.dumps(saved.model_dump(mode="json")).encode("ascii")) <= MAX_RECEIPT_BYTES
    assert saved.truncated
    assert saved.runtime.modelCallCount == MAX_RECORDED_MODEL_CALLS + 1
    assert "model_calls_capped" in saved.notes
    assert saved.usage.cost.coverage == "partial"
    assert saved.usage.cost.priceVersions == ("bounds-v1",)
    if quota:
        assert saved.runtime.modelCalls[0].admissions
    small = ReceiptDraft(model_evidence=ModelCallRecorder()).build()
    assert len(small.model_dump_json()) < MAX_RECEIPT_BYTES
    assert not small.truncated


@pytest.mark.parametrize("stream", [False, True])
def test_plain_tool_fallback_preserves_all_effective_calls_and_cost(stream):
    from ai4ia_api.websearch.factory import build_web_search_service
    from tests.test_agent_runtime import _assistant_tool_call
    from tests.test_chat_websearch_api import FakeWebClient

    def respond(_body, index, result):
        if index == 1:
            result["choices"] = _assistant_tool_call(
                "search", "web_search", '{"query":"receipt"}',
            )["choices"]
        elif index == 2:
            result["choices"] = [{"message": {"content": ""}}]
        return result

    with transport_client(respond=respond) as (client, requests):
        client.app.state.web_search = build_web_search_service(
            make_settings(web_search_enabled=True), entitlements=client.app.state.entitlements,
            metering=client.app.state.usage, client=FakeWebClient(),
        )
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Search then answer", "stream": stream,
            "params": {"max_tokens": 2048},
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert len(requests) == saved["runtime"]["modelCallCount"] == 3
        assert saved["toolCalls"][0]["tool"] == "web_search"
        assert saved["runtime"]["modelCalls"][0]["parameters"]["toolChoice"] == "auto"
        assert saved["runtime"]["modelCalls"][2]["parameters"]["toolChoice"] is None
        assert "tools" not in requests[2]
        assert saved["usage"]["cost"]["totalCalls"] == 3
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 12000


def test_stream_usage_compatibility_retry_is_one_logical_call_with_two_attempts():
    def respond(body, index, result):
        if index == 1:
            assert body["stream_options"]["include_usage"]
            return httpx.Response(400, text="unsupported option")
        assert "stream_options" not in body
        return result

    with transport_client(respond=respond) as (client, requests):
        client.app.state.gateway._stream_include_usage = True
        sid = session(client)
        response = client.post("/api/chat", json={
            "sessionId": sid, "content": "Hello", "stream": True,
        })
        assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        assert len(requests) == 2
        assert saved["runtime"]["modelCallCount"] == 1
        assert saved["runtime"]["modelCalls"][0]["httpAttempts"] == 2
        assert saved["usage"]["cost"]["totalCalls"] == 1
        assert saved["usage"]["cost"]["estCostMicroUsd"] == 4000


def test_durable_step_and_persistence_never_reprice_recorded_evidence():
    from functools import partial

    from ai4ia_api.workflows.durable import DurableWorkflowService
    from ai4ia_api.workflows.models import Workflow
    from tests.test_workflows_api import _mk_agent, _wf_body

    with transport_client() as (client, requests):
        for name in ("drafter", "editor"):
            assert _mk_agent(client, name).status_code == 201
        created = client.post("/api/workflows", json=_wf_body())
        assert created.status_code == 201, created.text
        workflow = Workflow.model_validate(created.json())
        sid = session(client)
        state = client.app.state
        uid = client.get(f"/api/sessions/{sid}").json()["userId"]
        deployment = state.catalog.resolve_deployment("gpt-5.2")
        context = {
            "userId": uid, "sessionId": sid, "workflowName": workflow.name, "runInput": "Hello",
            "modelId": "gpt-5.2", "deployment": deployment.deploymentName, "api": "chat",
            "workflowSnapshot": workflow.model_dump(mode="json"),
            "usageTarget": {
                "deployment": deployment.deploymentName, "target": deployment.deploymentName,
                "region": deployment.region, "dataZone": deployment.dataZone,
                "provider": "azure_openai",
            },
        }
        service = DurableWorkflowService(endpoint="", task_hub="", app_state=state)
        outcome = client.portal.call(partial(
            service._execute_step, step=workflow.steps[0], index=0, previous="", context=context,
        ))
        before = outcome["result"]["receipt"]
        assert len(requests) == 1
        assert before["runtime"]["modelCalls"][0]["parameterSource"] == "workflow_default"
        state.usage._pricing = PricingBook(
            {"gpt-5.2": PriceRate(400, 1600)}, currency="USD", version="later-prices",
        )
        client.portal.call(service._persist, {
            "context": context, "ok": True, "text": "Done", "usage": outcome["usage"],
            "steps": [outcome["result"]],
        })
        saved = client.get(f"/api/sessions/{sid}/messages").json()[-1]
        assert saved["workflowStepReceipts"][0] == before
        assert saved["executionReceipt"]["usage"]["cost"]["estCostMicroUsd"] == 4000
        assert saved["executionReceipt"]["usage"]["cost"]["priceVersions"] == ["receipt-test-v1"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", [
    "known", "missing_usage", "error", "cancel", "bounded", "bounded_unknown",
])
def test_chat_workflow_tool_preserves_nested_receipts_without_double_metering(stream, mode):
    from tests.test_agent_runtime import _assistant_tool_call
    from tests.test_workflows_api import _mk_agent, _wf_body

    def respond(body, index, result):
        offered = [tool["function"]["name"] for tool in body.get("tools", [])]
        if "run_workflow" in offered and not any(
            message["role"] == "tool" for message in body["messages"]
        ):
            result["choices"] = _assistant_tool_call(
                "workflow", "run_workflow", '{"workflow":"summarize","input":"Hello"}',
            )["choices"]
        if "run_workflow" not in offered:
            if mode == "missing_usage" or (mode == "bounded_unknown" and index in {6, 7}):
                result.pop("usage", None)
            elif index == 3 and mode == "error":
                return httpx.Response(502, text="provider failure")
            elif index == 3 and mode == "cancel":
                raise asyncio.CancelledError()
        return result

    with transport_client(respond=respond) as (client, requests):
        for name in ("drafter", "editor"):
            assert _mk_agent(client, name).status_code == 201
        steps = (
            [{"agent": "drafter", "instruction": "Draft {input}"} for _ in range(6)]
            if mode.startswith("bounded") else None
        )
        assert client.post("/api/workflows", json=_wf_body(steps=steps)).status_code == 201
        assert client.post("/api/agents", json={
            "name": "manager", "systemPrompt": "Run the saved workflow", "tools": ["run_workflow"],
        }).status_code == 201
        sid = session(client)
        body = {
            "sessionId": sid, "content": "@manager Run summarize", "stream": stream,
            "params": {"max_tokens": 2048},
        }
        if mode == "cancel" and not stream:
            with pytest.raises(RuntimeError, match="No response returned"):
                client.post("/api/chat", json=body)
        else:
            response = client.post("/api/chat", json=body)
            assert response.status_code == 200, response.text
        saved = receipts(client, sid)[0]
        expected_calls = 8 if mode.startswith("bounded") else 3 if mode == "cancel" else 4
        assert len(requests) == expected_calls
        workflow = saved["delegations"][0]
        assert workflow["runtime"]["agent"] == "workflow:summarize"
        assert workflow["runtime"]["workflowConfigSha256"]
        assert len(workflow["delegations"]) == (4 if mode.startswith("bounded") else 2)
        assert all(
            step["runtime"]["modelCalls"][0]["parameterSource"] == "workflow_default"
            for step in workflow["delegations"]
        )
        assert saved["usage"]["calls"] == saved["usage"]["cost"]["totalCalls"] == expected_calls
        parent_calls = saved["runtime"]["modelCallCount"]
        child_cost = workflow["usage"]["cost"]
        assert parent_calls == (1 if mode == "cancel" else 2)
        assert parent_calls + child_cost["totalCalls"] == expected_calls
        expected_subtotal = {
            "known": 16000, "missing_usage": 8000, "error": 12000,
            "cancel": 8000, "bounded": 32000, "bounded_unknown": 24000,
        }[mode]
        assert saved["usage"]["cost"]["estCostMicroUsd"] == expected_subtotal
        assert child_cost["estCostMicroUsd"] == {
            "known": 8000, "missing_usage": None, "error": 4000,
            "cancel": 4000, "bounded": 24000, "bounded_unknown": 16000,
        }[mode]
        assert saved["usage"]["cost"]["coverage"] == (
            "known" if mode in {"known", "bounded"} else "partial"
        )
        if mode == "missing_usage":
            assert child_cost["coverage"] == "unknown"
        if mode == "cancel":
            assert saved["status"] == workflow["status"] == "cancelled"
        if mode.startswith("bounded"):
            assert "delegations_capped" in workflow["notes"]
            assert workflow["truncated"]
        assert len(json.dumps(saved).encode("ascii")) <= MAX_RECEIPT_BYTES
        records = next(iter(client.app.state.usage._repo._by_user.values()))
        assert len(records) == 2
        assert sum(record.calls for record in records) == expected_calls
        assert sorted(record.agent for record in records) == ["manager", "workflow:summarize"]


def test_receipt_budget_reserves_the_truncation_marker_at_the_exact_boundary():
    from ai4ia_api.receipts import (
        ReceiptPromptMessage, ReceiptToolCall, enforce_receipt_budget, text_payload,
    )

    receipt = ExecutionReceipt(
        prompt=[ReceiptPromptMessage(role="user", content=text_payload("")) for _ in range(40)],
        toolCalls=[ReceiptToolCall(
            tool="calculator", outcome="result", result=text_payload("result " * 40),
        )],
    )

    def wire_size(value):
        return len(json.dumps(value.model_dump(mode="json")).encode("ascii"))

    def after_first_shed():
        candidate = receipt.model_copy(deep=True)
        candidate.toolCalls[0].result = candidate.toolCalls[0].result.shed()
        return wire_size(candidate)

    target = MAX_RECEIPT_BYTES - 8
    for index, message in enumerate(receipt.prompt):
        room = (target - after_first_shed()) // (len(receipt.prompt) - index)
        message.content = text_payload(("p " * room)[:room])
    for _ in range(3):
        difference = target - after_first_shed()
        last = receipt.prompt[-1].content.text
        last = last + " " * difference if difference >= 0 else last[:difference]
        receipt.prompt[-1].content = text_payload(last)
    assert after_first_shed() == target
    assert wire_size(receipt) > MAX_RECEIPT_BYTES
    assert all(message.content.bytes <= 2048 for message in receipt.prompt)
    result = enforce_receipt_budget(receipt)
    assert "receipt_size_capped" in result.notes
    assert wire_size(result) <= MAX_RECEIPT_BYTES


def test_aggregate_shedding_cannot_mutate_independent_workflow_step_snapshots():
    from ai4ia_api.agents.runtime import AgentStep
    from ai4ia_api.receipts import ReceiptRuntime
    from ai4ia_api.usage.models import TokenUsage
    from ai4ia_api.workflows.receipts import workflow_receipt
    from ai4ia_api.workflows.runner import WorkflowRunResult, WorkflowStepResult

    evidence = ModelCallRecorder(
        model_id="model", deployment="deployment",
        pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="immutable-v1"),
    )
    call = evidence.start("deployment", "chat")
    call.request({"max_tokens": 100})
    call.report_usage({"prompt_tokens": 1000, "completion_tokens": 250}, completed=True)
    filler = "ordinary result text " * 90
    usage = TokenUsage.parse({"prompt_tokens": 1000, "completion_tokens": 250})
    child = ReceiptDraft(
        model_evidence=evidence,
        prompt_messages=[{"role": "user", "content": filler} for _ in range(10)],
    ).build(
        steps=[AgentStep(kind="tool_result", tool="calculator", result={"value": filler})
               for _ in range(8)],
        usage=usage,
    )
    steps = [
        WorkflowStepResult(agent="helper", ok=True, receipt=child.model_copy(deep=True))
        for _ in range(3)
    ]
    before = [step.receipt.model_dump(mode="json") for step in steps]
    parent = workflow_receipt(
        WorkflowRunResult(ok=True, text="Done", steps=steps, usage=usage.add(usage).add(usage)),
        runtime=ReceiptRuntime(agent="workflow:example"), include_steps=True,
    )
    assert parent.truncated
    assert parent.usage.cost.estCostMicroUsd == 12000
    assert [step.receipt.model_dump(mode="json") for step in steps] == before
    assert any(call.result.text for call in child.toolCalls)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -1, 1e300])
def test_nonfinite_or_out_of_bounds_prices_and_controls_are_explicit_gaps(bad_value):
    evidence = ModelCallRecorder(
        model_id="model", deployment="deployment",
        pricing=PricingBook({"model": PriceRate(bad_value, 8)}, currency="USD", version="bad-price"),
    )
    call = evidence.start("deployment", "chat")
    call.request({"temperature": bad_value, "max_tokens": 100, "tool_choice": {"type": []}})
    call.report_usage({"prompt_tokens": 1000, "completion_tokens": 250}, completed=True)
    snapshot = call.snapshot()
    assert snapshot.coverage == "partial"
    assert snapshot.parameters.temperature is None
    assert snapshot.parameters.maxOutputTokens == 100
    assert snapshot.parameters.toolChoice is None
    assert snapshot.cost.coverage == "unknown"
    assert snapshot.cost.estCostMicroUsd is None
    assert snapshot.cost.priceInputPer1M is None
    json.dumps(snapshot.model_dump(mode="json"), allow_nan=False)


def test_recorded_call_and_parameter_values_are_frozen():
    from pydantic import ValidationError

    evidence = ModelCallRecorder()
    call = evidence.start("deployment", "chat")
    request = {"max_tokens": 100, "reasoning_effort": "low"}
    call.request(request)
    saved = ReceiptDraft(model_evidence=evidence).build()
    request["max_tokens"] = 500
    call.request(request)
    assert saved.runtime.modelCalls[0].parameters.maxOutputTokens == 100
    with pytest.raises(ValidationError, match="frozen"):
        saved.runtime.modelCalls[0].parameters.maxOutputTokens = 300
    with pytest.raises(ValidationError, match="frozen"):
        saved.runtime.modelCalls[0].parameterSource = "request"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("raw,known", [
    ({}, False), ({"input_tokens": 4}, False), ({"output_tokens": 2}, False),
    ({"input_tokens": "missing", "output_tokens": 2}, False),
    ({"input_tokens": 4, "output_tokens": False}, False),
    ({"input_tokens": 4, "output_tokens": 2, "cache_read_input_tokens": "missing"}, False),
    ({"input_tokens": 0, "output_tokens": 0}, True),
])
async def test_anthropic_missing_usage_cannot_become_known_zero(stream, raw, known):
    def handle(_request):
        if stream:
            events = [
                {"type": "message_start", "message": {"usage": raw}},
                {"type": "message_delta", "usage": raw},
                {"type": "message_stop"},
            ]
            return httpx.Response(200, text="".join(
                f"data: {json.dumps(event)}\n\n" for event in events
            ))
        return httpx.Response(200, json={"type": "message", "content": [], "usage": raw})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        evidence = ModelCallRecorder(
            model_id="model", deployment="dep",
            pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="anthropic-v1"),
        )
        kwargs = dict(deployment="dep", messages=[], api="anthropic")
        if stream:
            async for _ in evidence.observe_stream(gateway.stream(**kwargs)):
                pass
        else:
            await evidence.observe(gateway.complete(**kwargs))
        saved = evidence.snapshot()[0]
        assert saved.usageKnown is known
        assert saved.cost.coverage == ("known" if known else "unknown")
        assert saved.cost.estCostMicroUsd == (0 if known else None)


@pytest.mark.parametrize("terminal_usage", [False, True])
async def test_anthropic_initial_usage_is_not_a_final_zero_cost_report(terminal_usage):
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 0, "output_tokens": 0}}},
    ]
    if terminal_usage:
        events.append({"type": "message_delta", "usage": {"output_tokens": 0}})
    events.append({"type": "message_stop"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text="".join(
            f"data: {json.dumps(event)}\n\n" for event in events
        )),
    )) as http:
        evidence = ModelCallRecorder(
            model_id="model", deployment="dep",
            pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="anthropic-v1"),
        )
        gateway = ModelGatewayClient(make_settings(), http_client=http)
        async for _ in evidence.observe_stream(gateway.stream(
            deployment="dep", messages=[], api="anthropic",
        )):
            pass
        cost = evidence.snapshot()[0].cost
        assert cost.coverage == ("known" if terminal_usage else "unknown")
        assert cost.estCostMicroUsd == (0 if terminal_usage else None)


@pytest.mark.parametrize("cancel", [False, True])
async def test_plain_stream_disconnect_keeps_evidence_when_closed_from_another_task(cancel):
    from ai4ia_api.auth.base import AuthenticatedUser
    from ai4ia_api.catalog import DeploymentOption
    from ai4ia_api.routers._chat_streaming import _plain_gateway_stream
    from tests.test_chat_stream_protocol import (
        _NoopMemory, _NoopMetering, _PersistingRepo, _stream_assistant,
    )

    frame = {
        "choices": [{"delta": {"content": "Partial"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 250},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n"),
    )) as http:
        evidence = ModelCallRecorder(
            model_id="model", deployment="deployment",
            pricing=PricingBook({"model": PriceRate(2, 8)}, currency="USD", version="disconnect-v1"),
        )
        repo = _PersistingRepo()
        stream = _plain_gateway_stream(
            assistant=_stream_assistant(), user_message_id="user-message",
            gateway=ModelGatewayClient(make_settings(), http_client=http),
            deployment=DeploymentOption(
                deploymentName="deployment", region="eastus", sku="GlobalStandard",
            ),
            messages=[{"role": "user", "content": "Hello"}], params={"max_tokens": 200},
            correlation_id="correlation", api="chat", repo=repo,
            memory=_NoopMemory(), metering=_NoopMetering(),
            user=AuthenticatedUser(
                internal_user_id="user", subject="subject", issuer="issuer", provider="dev",
            ),
            session_id="session", model_id="model", agent_name=None, content_for_model="Hello",
            receipt_draft=ReceiptDraft(model_evidence=evidence),
        )
        assert "metadata" in await anext(stream)
        assert "Partial" in await anext(stream)
        if cancel:
            await asyncio.create_task(stream.aclose())
        else:
            async for _ in stream:
                pass
        saved = repo.persisted[-1].executionReceipt
        assert saved.status == ("cancelled" if cancel else "complete")
        assert saved.runtime.modelCalls[0].parameters.maxOutputTokens == 200
        assert saved.usage.cost.coverage == ("unknown" if cancel else "known")
        assert saved.usage.cost.estCostMicroUsd == (None if cancel else 4000)
