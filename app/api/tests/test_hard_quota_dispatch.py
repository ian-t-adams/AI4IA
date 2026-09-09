"""Exercise the real outbound seams: denied work emits no provider/tool request."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ai4ia_api.agents.mcp_client import HttpxMcpConnector, McpAuth
from ai4ia_api.agents.runtime import AgentRunFailed, run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.catalog import ModelCatalog, ModelEntry, DeploymentOption
from ai4ia_api.code_interpreter.client import CodeInterpreterClient
from ai4ia_api.content_understanding.client import ContentUnderstandingClient
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement, EntitlementLimits
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.hard_quota.coverage import AttemptEnvelope
from ai4ia_api.hard_quota.dispatch import AdmissionController, admission_scope
from ai4ia_api.hard_quota.models import QuotaError
from ai4ia_api.hard_quota.store import LocalReservationStore
from ai4ia_api.model_evidence import ModelCallRecorder
from ai4ia_api.usage.pricing import PriceRate, PricingBook
from ai4ia_api.websearch.client import WebSearchClient
from tests.conftest import make_settings
from tests.test_agent_runtime import _assistant_tool_call, _assistant_text

NOW = 1_800_000_000
DEPLOYMENT = "fixture-text-deployment"
USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


class NoNumericReads:
    async def window_totals(self, *args, **kwargs):
        raise AssertionError("hard admission must not query the soft ledger")


class AvailableStore(LocalReservationStore):
    available = True

    async def read(self, owner):
        if not self.available:
            raise QuotaError("Hard quota coordination is unavailable.")
        snapshot = await super().read(owner)
        await asyncio.sleep(0)
        return snapshot


class Harness:
    def __init__(self):
        self.clock = [NOW]
        self.store = AvailableStore(clock=lambda: self.clock[0])
        self.store.seed("alice")
        self.store.seed("bob")
        self.entitlements = EntitlementService(
            InMemoryEntitlementStore(), NoNumericReads(), Entitlement.unlimited(),
            enabled=False, cache_ttl_seconds=0,
        )
        self.catalog = ModelCatalog(models=[
            ModelEntry(
                id="fixture-text", displayName="Fixture", category="chat", format="OpenAI",
                contextWindow=100, maxOutputTokens=20,
                options=[DeploymentOption(region="test", sku="Standard", deploymentName=DEPLOYMENT)],
            ),
        ])
        self.pricing = PricingBook(
            {"fixture-text": PriceRate(1.0, 2.0)}, currency="USD", version="fixture-v1",
        )
        self.controller = AdmissionController(
            entitlements=self.entitlements, store=self.store, catalog=self.catalog,
            pricing=self.pricing, enabled=True,
        )
        self.settings = make_settings(
            hard_quota_enabled=True, entitlements_enabled=False,
            model_gateway_url="https://gateway.test/openai",
            cu_base_url="https://document.test", cu_auth_mode="none",
            code_interpreter_base_url="https://compute.test", code_interpreter_auth_mode="none",
            code_interpreter_model="fixture-compute", web_search_enabled=True,
        )

    async def limits(self, **values):
        await self.entitlements.set("alice", EntitlementLimits(**values), updated_by=None)


@pytest.fixture
def harness(request):
    result = Harness()
    if hasattr(request, "param"):
        result.controller.enabled = request.param
        result.settings.hard_quota_enabled = request.param
    return result


CASES = [
    "chat", "responses", "anthropic", "chat-stream", "responses-stream", "anthropic-stream",
    "embedding", "image", "video", "speech", "transcription", "ocr",
    "cu-submit", "cu-inline", "compute", "compute-upload", "webiq", "mcp-tool", "mcp-resource",
]


def response_for(case, request):
    if case.startswith("mcp-"):
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
        elif method == "resources/read":
            result = {"contents": [{"uri": "skill://fixture", "text": "hello"}]}
        else:
            result = {"content": [{"type": "text", "text": "hello"}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    if case == "chat-stream":
        return httpx.Response(200, text=(
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            f'data: {json.dumps({"choices": [], "usage": USAGE})}\n\n'
            'data: [DONE]\n\n'
        ))
    if case == "responses-stream":
        data = {
            "type": "response.completed", "response": {
                "output": [], "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(data)}\n\n")
    if case == "anthropic-stream":
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 3, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))
    if case == "responses":
        return httpx.Response(200, json={
            "status": "completed", "output": [],
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        })
    if case == "anthropic":
        return httpx.Response(200, json={
            "type": "message", "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2},
        })
    if case == "compute-upload":
        return httpx.Response(200, json={"id": "file-fixture"})
    if case == "compute":
        return httpx.Response(200, json={"status": "completed", "output_text": "hello"})
    if case in {"cu-submit", "cu-inline"}:
        return httpx.Response(200, json={"result": {"contents": []}},
                              headers={"operation-location": "https://document.test/result"})
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": USAGE, "data": [{"index": 0, "embedding": [1.0]}], "text": "hello",
        "id": "video-fixture", "pages": [],
    })


@pytest.fixture(params=CASES)
async def outbound(request, harness):
    case = request.param
    sent = []

    def transport(req):
        sent.append(req)
        return response_for(case, req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(harness.settings, http)
        cu = ContentUnderstandingClient(harness.settings, http_client=http)
        compute = CodeInterpreterClient(harness.settings, http_client=http)
        mcp = HttpxMcpConnector(client=http, hard_quota_enabled=harness.settings.hard_quota_enabled)

        class WebTransport:
            async def request(self, **kwargs):
                sent.append(kwargs)
                return {"results": []}

        web = WebSearchClient(harness.settings, transport=WebTransport())

        async def invoke():
            if case in {"chat", "responses", "anthropic"}:
                return await gateway.complete(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}], api=case,
                )
            if case.endswith("-stream"):
                return [chunk async for chunk in gateway.stream(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
                    api=case.removesuffix("-stream"),
                )]
            if case == "embedding":
                return await gateway.embed(deployment=DEPLOYMENT, inputs=["hello"])
            if case == "image":
                return await gateway.generate_image(deployment=DEPLOYMENT, prompt="hello")
            if case == "video":
                return await gateway.create_video_job(
                    deployment=DEPLOYMENT, prompt="hello", width=1024, height=1024, n_seconds=4,
                )
            if case == "speech":
                return await gateway.synthesize_speech(
                    deployment=DEPLOYMENT, text="hello", voice="voice", response_format="mp3",
                )
            if case == "transcription":
                return await gateway.transcribe(
                    deployment=DEPLOYMENT, audio=b"audio", filename="a.wav", content_type="audio/wav",
                )
            if case == "ocr":
                return await gateway.analyze_document(
                    deployment=DEPLOYMENT, data=b"document", content_type="application/pdf",
                )
            if case == "cu-submit":
                return await cu.submit_binary("fixture", b"document", "application/pdf")
            if case == "cu-inline":
                return await cu.analyze_inline("fixture", b"document", "application/pdf")
            if case == "compute":
                return await compute.run(instructions="calculate", user_input="hello")
            if case == "compute-upload":
                return await compute.upload_file(filename="a.txt", content=b"hello")
            if case == "webiq":
                return await web.web_search("hello", max_results=1)
            if case == "mcp-tool":
                return await mcp.call_tool(
                    endpoint="https://mcp.test", auth=McpAuth(), tool="fixture", arguments={},
                )
            return await mcp.read_resource(
                endpoint="https://mcp.test", auth=McpAuth(), uri="skill://fixture",
            )

        yield SimpleNamespace(case=case, invoke=invoke, sent=sent, gateway=gateway)


async def test_every_surface_denies_at_zero_and_same_call_is_allowed_at_one(harness, outbound):
    await harness.limits(requestsPerMinute=0)
    with admission_scope(harness.controller, "alice"):
        with pytest.raises(QuotaError, match="would be exceeded"):
            await outbound.invoke()
    assert outbound.sent == []
    await harness.limits(requestsPerMinute=1)
    with admission_scope(harness.controller, "alice") as context:
        await outbound.invoke()
    assert len(outbound.sent) == (3 if outbound.case.startswith("mcp-") else 1)
    assert len(context.evidence) == 1
    assert context.evidence[0].charged.requests == 1


async def test_every_surface_fails_closed_on_storage_outage_with_healthy_control(harness, outbound):
    harness.store.available = False
    with admission_scope(harness.controller, "alice"):
        with pytest.raises(QuotaError, match="unavailable"):
            await outbound.invoke()
    assert outbound.sent == []
    harness.store.available = True
    with admission_scope(harness.controller, "alice"):
        await outbound.invoke()
    assert outbound.sent


@pytest.mark.parametrize("cap", ["tokensPerDay", "costPerDayMicroUsd"])
async def test_shipping_surfaces_refuse_unsupported_token_and_dollar_caps(harness, outbound, cap):
    await harness.limits(**{cap: 1_000_000})
    with admission_scope(harness.controller, "alice"):
        with pytest.raises(QuotaError, match="does not support"):
            await outbound.invoke()
    assert outbound.sent == []
    await harness.limits(requestsPerMinute=1)
    with admission_scope(harness.controller, "alice"):
        await outbound.invoke()
    assert outbound.sent


async def test_missing_owner_fails_before_every_outbound_send(outbound):
    with pytest.raises(QuotaError, match="authenticated owner"):
        await outbound.invoke()
    assert outbound.sent == []


@pytest.mark.parametrize("harness", [False, True], indirect=True)
async def test_disabled_owner_blocks_every_surface_in_both_modes(harness, outbound):
    await harness.limits(disabled=True)
    with admission_scope(harness.controller, "alice"):
        with pytest.raises(QuotaError, match="disabled"):
            await outbound.invoke()
    assert outbound.sent == []
    await harness.limits(disabled=False)
    with admission_scope(harness.controller, "alice"):
        await outbound.invoke()
    assert outbound.sent


@pytest.mark.parametrize("harness", [False], indirect=True)
async def test_soft_off_leaves_numeric_caps_and_coordination_out_of_dispatch(harness, outbound):
    await harness.limits(
        requestsPerMinute=0, tokensPerDay=0, costPerDayMicroUsd=0, computeExecutionsPerDay=0,
    )
    harness.store.available = False
    with admission_scope(harness.controller, "alice"):
        await outbound.invoke()
    assert outbound.sent  # NoNumericReads also proves no numeric ledger IO.


async def test_actual_gateway_retry_reuses_operation_identity_without_resending(harness):
    sent = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (sent.append(req) or response_for("chat", req)),
    )) as http:
        gateway = ModelGatewayClient(harness.settings, http)
        async def invoke(text="hello"):
            with admission_scope(harness.controller, "alice", root="stable", issued_at=NOW):
                return await gateway.complete(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": text}],
                )
        assert await invoke()
        with pytest.raises(QuotaError, match="already claimed"):
            await invoke()
        with pytest.raises(QuotaError, match="payload changed"):
            await invoke("changed")
        assert len(sent) == 1


async def test_fixture_token_and_versioned_dollar_bounds_settle_without_repricing(harness):
    harness.controller.attempts = AttemptEnvelope("fixture-single-send-v1", 1)
    await harness.limits(tokensPerDay=120, costPerDayMicroUsd=140)
    recorder = ModelCallRecorder(
        model_id="fixture-text", deployment=DEPLOYMENT, pricing=harness.pricing,
    )
    async def transport(req):
        # Mutation happens during the await. Settlement must use the old snapshot.
        harness.pricing._rates["fixture-text"] = PriceRate(1000, 1000)
        harness.pricing._version = "changed"
        return response_for("chat", req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(harness.settings, http)
        with admission_scope(harness.controller, "alice") as context:
            await recorder.observe(gateway.complete(
                deployment=DEPLOYMENT, messages=[{"role": "user", "content": "hello"}],
            ))
    evidence = context.evidence[0]
    assert evidence.reserved.tokens == 120
    assert evidence.reserved.microUsd == 140
    assert evidence.charged.tokens == 5
    assert evidence.charged.microUsd == 7
    assert evidence.priceVersion == "fixture-v1"
    assert recorder.snapshot()[0].admissions == (evidence,)


@pytest.mark.parametrize("stream", [False, True])
async def test_agent_loop_cannot_escape_shared_dispatch_limit(harness, stream):
    sent = []
    def transport(req):
        body = json.loads(req.content)
        sent.append(body)
        has_result = any(message.get("role") == "tool" for message in body["messages"])
        data = (
            _assistant_text("42") if has_result
            else _assistant_tool_call("calculate-1", "calculator", '{"expression":"6*7"}')
        )
        data["usage"] = USAGE
        if stream:
            from tests.conftest import sse_chunks
            return httpx.Response(200, text="".join(
                f"data: {chunk.raw}\n\n" for chunk in sse_chunks(data)
            ))
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gateway = ModelGatewayClient(harness.settings, http)
        registry, executor = build_tools()
        async def on_delta(_text):
            return None

        async def run():
            with admission_scope(harness.controller, "alice"):
                return await run_agent_turn(
                    deployment=DEPLOYMENT, messages=[{"role": "user", "content": "6*7?"}],
                    tool_names=["calculator"], gateway=gateway, registry=registry,
                    executor=executor, ctx=ToolContext(), on_delta=on_delta if stream else None,
                )
        await harness.limits(requestsPerMinute=1)
        with pytest.raises((AgentRunFailed, QuotaError)):
            await run()
        assert len(sent) == 1
        await harness.limits(requestsPerMinute=3)
        result = await run()
        assert result.text == "42"
        assert len(sent) == 3
