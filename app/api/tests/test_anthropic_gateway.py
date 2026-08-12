from __future__ import annotations

import json

import httpx

from ai4ia_api.agents.runtime import run_agent_turn
from ai4ia_api.agents.tool_exec import ToolContext, build_tools
from ai4ia_api.gateway.client import ModelGatewayClient
from tests.conftest import make_settings


DEPLOYMENT = "claude-opus-4-8-slurmfactory-eastus2-glbl"


def _client(handler=None) -> ModelGatewayClient:
    transport = httpx.MockTransport(handler) if handler is not None else None
    http = httpx.AsyncClient(transport=transport) if transport is not None else None
    return ModelGatewayClient(
        make_settings(model_gateway_url="https://proxy.test/openai"),
        http_client=http,
    )


def _sse(*events: dict) -> str:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )


def test_build_request_uses_internal_deployment_path_and_claude_body():
    client = _client()
    request = client.build_anthropic_request(
        deployment=DEPLOYMENT,
        messages=[
            {"role": "system", "content": "Be precise."},
            {"role": "user", "content": "Hello"},
        ],
        params={
            "temperature": 0.7,
            "top_p": 1,
            "max_tokens": 2048,
            "reasoning_effort": "xhigh",
        },
    )

    assert request.url == (
        "https://proxy.test/openai/deployments/"
        f"{DEPLOYMENT}/chat/completions"
    )
    assert "api-version=" not in request.url
    assert request.json == {
        "model": DEPLOYMENT,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Hello"}]}
        ],
        "max_tokens": 2048,
        "stream": False,
        "system": "Be precise.",
    }


def test_tool_schemas_and_history_translate_without_weakening_protocol():
    client = _client()
    request = client.build_anthropic_request(
        deployment=DEPLOYMENT,
        messages=[
            {"role": "user", "content": "Calculate 6*7"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"6*7"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"result":42}',
            },
        ],
        params={
            "max_tokens": 1024,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "Evaluate arithmetic.",
                        "parameters": {
                            "type": "object",
                            "properties": {"expression": {"type": "string"}},
                            "required": ["expression"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
    )

    assert request.json["tools"] == [
        {
            "name": "calculator",
            "description": "Evaluate arithmetic.",
            "input_schema": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        }
    ]
    assert request.json["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }
    assert request.json["messages"][1]["content"] == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "calculator",
            "input": {"expression": "6*7"},
        }
    ]
    assert request.json["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": '{"result":42}',
        }
    ]


def test_tool_choice_none_withholds_the_tool_schema_from_claude():
    request = _client().build_anthropic_request(
        deployment=DEPLOYMENT,
        messages=[{"role": "user", "content": "Do not use tools."}],
        params={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "none",
        },
    )
    assert "tools" not in request.json
    assert "tool_choice" not in request.json


async def test_nonstream_response_translates_text_tools_and_usage():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "calculator",
                        "input": {"expression": "6*7"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 2,
                    "output_tokens": 4,
                },
            },
        )

    client = _client(handler)
    result = await client.complete(
        deployment=DEPLOYMENT,
        messages=[{"role": "user", "content": "Calculate 6*7"}],
    )

    assert captured["body"]["model"] == DEPLOYMENT
    message = result["choices"][0]["message"]
    assert message["content"] == "Checking."
    assert message["tool_calls"][0]["function"] == {
        "name": "calculator",
        "arguments": '{"expression":"6*7"}',
    }
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    await client._http.aclose()  # type: ignore[union-attr]


async def test_claude_stream_runs_a_governed_agent_tool_loop_end_to_end():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            stream = _sse(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 8, "output_tokens": 0}},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_calc",
                        "name": "calculator",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"expression":"6*7"}',
                    },
                },
                {
                    "type": "message_delta",
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            )
        else:
            stream = _sse(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 12, "output_tokens": 0}},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "The answer is 42."},
                },
                {
                    "type": "message_delta",
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            )
        return httpx.Response(
            200, text=stream, headers={"content-type": "text/event-stream"}
        )

    client = _client(handler)
    registry, executor = build_tools()
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    result = await run_agent_turn(
        deployment=DEPLOYMENT,
        messages=[
            {"role": "system", "content": "Use the calculator."},
            {"role": "user", "content": "What is 6*7?"},
        ],
        tool_names=["calculator"],
        gateway=client,
        registry=registry,
        executor=executor,
        ctx=ToolContext(),
        on_delta=on_delta,
    )

    assert result.text == "The answer is 42."
    assert result.iterations == 2
    assert deltas == ["The answer is 42."]
    assert [step.kind for step in result.steps] == ["tool_result", "final"]
    assert result.steps[0].tool == "calculator"
    second_messages = requests[1]["messages"]
    assert any(
        block.get("type") == "tool_use"
        for message in second_messages
        for block in message["content"]
    )
    assert any(
        block.get("type") == "tool_result"
        for message in second_messages
        for block in message["content"]
    )
    assert result.usage.prompt == 20
    assert result.usage.completion == 9
    await client._http.aclose()  # type: ignore[union-attr]
