"""Cross-protocol controls for MCP and realtime admission boundaries."""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from ai4ia_api.agents.mcp_client import HttpxMcpConnector, McpAuth
from ai4ia_api.hard_quota.dispatch import admission_scope
from ai4ia_api.hard_quota.models import QuotaError
from ai4ia_api.realtime_protocol import RealtimeProtocol
from ai4ia_api.routers.realtime import DEV_SUBPROTOCOL, UpstreamMessage
from tests.test_hard_quota_dispatch import Harness
from tests.test_mcp_protocol import CONTEXT, ENDPOINT, MODERN, PROTOCOLS, STATEFUL, URI, _wire
from tests.test_realtime_api import ScriptedRealtimeConnector, _client, _origin
from tests.test_realtime_staged_api import GA_SETTINGS


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("operation", ["tool", "resource"])
@pytest.mark.parametrize("denial", ["requests", "tokens", "dollars", "storage", "owner"])
async def test_all_mcp_versions_reserve_once_before_any_handshake_or_rpc(protocol, operation, denial):
    harness = Harness()
    context = replace(CONTEXT, protocol_version=protocol, owner_id="alice")
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(protocol, seen))) as http:
        connector = HttpxMcpConnector(client=http, hard_quota_enabled=True)

        async def invoke():
            if operation == "tool":
                return await connector.call_tool(
                    endpoint=ENDPOINT, auth=McpAuth(), context=context,
                    tool="forecast", arguments={"city": "SEA"},
                )
            return await connector.read_resource(
                endpoint=ENDPOINT, auth=McpAuth(), context=context, uri=URI,
            )

        if denial == "storage":
            harness.store.available = False
        elif denial != "owner":
            await harness.limits(**{
                "requests": {"requestsPerMinute": 0},
                "tokens": {"tokensPerDay": 1000},
                "dollars": {"costPerDayMicroUsd": 1000},
            }[denial])
        if denial == "owner":
            with pytest.raises(QuotaError, match="authenticated owner"):
                await invoke()
        else:
            with admission_scope(harness.controller, "alice"):
                with pytest.raises(QuotaError):
                    await invoke()
        assert seen == []

        harness.store.available = True
        await harness.limits(requestsPerMinute=1)
        with admission_scope(harness.controller, "alice") as admission:
            result = await invoke()
        assert result.content == "Sunny" if operation == "tool" else result.text == "Skill instructions"
        expected = "tools/call" if operation == "tool" else "resources/read"
        assert [body["method"] for _, body in seen] == (
            ["initialize", "notifications/initialized", expected] if protocol in STATEFUL else [expected]
        )
        assert admission.evidence_count == 1
        assert len(admission.evidence) == 1
        assert admission.evidence[0].phase == "settled"
        assert admission.evidence[0].charged.requests == 1
        assert not (await harness.store.read("bob")).state.entries
        sent_count = len(seen)
        with admission_scope(harness.controller, "alice"):
            with pytest.raises(QuotaError, match="would be exceeded"):
                await invoke()
        assert len(seen) == sent_count


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_mcp_frozen_arguments_and_derived_headers_remain_one_admitted_payload(protocol):
    harness = Harness()
    seen = []
    arguments = {"city": "SEA"}
    original_read = harness.store.read

    async def mutate(owner):
        arguments["city"] = "changed after admission began"
        return await original_read(owner)

    harness.store.read = mutate
    schema = {"type": "object", "properties": {
        "city": {"type": "string", "x-mcp-header": "City"},
    }}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(protocol, seen))) as http:
        connector = HttpxMcpConnector(client=http, hard_quota_enabled=True)
        with admission_scope(harness.controller, "alice"):
            await connector.call_tool(
                endpoint=ENDPOINT, auth=McpAuth(),
                context=replace(CONTEXT, protocol_version=protocol),
                tool="forecast", arguments=arguments, input_schema=schema,
            )
    request, body = seen[-1]
    assert arguments["city"] == "changed after admission began"
    assert body["params"]["arguments"] == {"city": "SEA"}
    if protocol is MODERN:
        assert request.headers["Mcp-Param-City"] == "SEA"
    else:
        assert "Mcp-Param-City" not in request.headers


async def test_modern_list_cache_never_caches_execution_or_consumes_its_reservations():
    harness = Harness()
    seen = []
    context = replace(CONTEXT, owner_id="alice")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_wire(MODERN, seen))) as http:
        connector = HttpxMcpConnector(client=http, hard_quota_enabled=True)
        with admission_scope(harness.controller, "alice") as admission:
            for _ in range(2):
                assert len(await connector.discover(
                    endpoint=ENDPOINT, auth=McpAuth(), context=context,
                )) == 1
            assert admission.evidence_count == 0
            assert [body["method"] for _, body in seen] == ["tools/list"]
            await harness.limits(requestsPerMinute=2)
            for _ in range(2):
                assert (await connector.call_tool(
                    endpoint=ENDPOINT, auth=McpAuth(), context=context,
                    tool="forecast", arguments={},
                )).content == "Sunny"
            assert admission.evidence_count == 2
            assert [body["method"] for _, body in seen] == [
                "tools/list", "tools/call", "tools/call",
            ]


@pytest.mark.parametrize("protocol", list(RealtimeProtocol))
@pytest.mark.parametrize("denial", ["requests", "tokens", "dollars", "storage"])
def test_ga_normalization_does_not_bypass_the_preconnect_quota_gate(protocol, denial):
    client = _client(
        realtime_enabled=True, hard_quota_enabled=True, entitlements_enabled=False,
        realtime_protocol=protocol, **GA_SETTINGS,
    )
    try:
        headers = {"X-Dev-User": "alice"}
        uid = client.get("/api/entitlement", headers=headers).json()["userId"]
        store = client.app.state.hard_quota.reservations.store
        if denial != "storage":
            store.seed(uid)
        cap = {
            "requests": {"requestsPerMinute": 0},
            "tokens": {"tokensPerDay": 1000},
            "dollars": {"costPerDayMicroUsd": 1000},
            "storage": {},
        }[denial]
        assert client.put(f"/api/admin/entitlements/{uid}", headers=headers, json=cap).status_code == 200
        expected = {"type": "response.audio.delta", "delta": "AAA=", "response_id": "fixture"}
        upstream = {
            **expected,
            "type": "response.output_audio.delta" if protocol is RealtimeProtocol.ga
                    else "response.audio.delta",
        }
        connector = ScriptedRealtimeConnector([
            UpstreamMessage("text", text=json.dumps(upstream)),
            UpstreamMessage("close", close_code=1000),
        ])
        client.app.state.realtime_connector = connector

        def connect():
            messages = []
            with client.websocket_connect(
                "/api/voice/live", subprotocols=[DEV_SUBPROTOCOL, "alice"], headers=_origin(),
            ) as websocket:
                for _ in range(10):
                    message = websocket.receive()
                    if message["type"] == "websocket.close":
                        return messages
                    if "text" in message:
                        messages.append(json.loads(message["text"]))
                raise AssertionError("bounded fixture did not terminate")

        connect()
        assert connector.connects == []
        if denial == "storage":
            store.seed(uid)
        assert client.put(
            f"/api/admin/entitlements/{uid}", headers=headers, json={"requestsPerMinute": 1},
        ).status_code == 200
        assert connect() == [expected]
        assert len(connector.connects) == 1
        if protocol is RealtimeProtocol.ga:
            assert "/openai/v1/realtime?model=" in connector.connects[0]["url"]
        else:
            assert "/openai/realtime?api-version=" in connector.connects[0]["url"]
    finally:
        client.__exit__(None, None, None)
