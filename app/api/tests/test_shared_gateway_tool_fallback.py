"""Synthetic end-to-end route pinning for tool-capable chat turns."""

from __future__ import annotations

import ipaddress
import json
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.auth.dependencies import get_current_user
from ai4ia_api.config import Settings
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from tests.test_chat_websearch_api import FakeWebClient, _inject_web
from tests.test_shared_gateway_staging import (
    _manifest,
    _settings,
    _token,
)


@pytest.fixture(autouse=True)
def isolate_environment_and_network(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_dns = socket.getaddrinfo

    def deny_non_loopback(address):
        try:
            is_loopback = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise AssertionError("Synthetic routing tests cannot open external sockets")

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            deny_non_loopback(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            deny_non_loopback(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        deny_non_loopback((host,))
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(
        Settings,
        "settings_customise_sources",
        classmethod(lambda cls, settings_cls, init_settings, **kwargs: (init_settings,)),
    )


def _frames(response):
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _sse(api: str, *, text: str | None = None, empty: bool = False) -> bytes:
    if api == "responses":
        if empty:
            events = [{"type": "response.completed", "response": {"output": []}}]
        else:
            response = {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text or ""}],
                    }
                ]
            }
            events = [
                {"type": "response.output_text.delta", "delta": text or ""},
                {"type": "response.completed", "response": response},
            ]
    elif empty:
        events = [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    else:
        events = [{"choices": [{"delta": {"content": text or ""}}]}]
    return (
        "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        + "data: [DONE]\n\n"
    ).encode()


def _success(api: str, *, stream: bool, text: str = "Synthetic fallback answer"):
    if stream:
        return httpx.Response(200, stream=httpx.ByteStream(_sse(api, text=text)))
    if api == "responses":
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": text}}]},
    )


def _tool_call(api: str):
    if api == "responses":
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "synthetic-call",
                        "name": "web_search",
                        "arguments": json.dumps({"query": "synthetic headlines"}),
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "synthetic-call",
                                "type": "function",
                                "function": {
                                    "name": "web_search",
                                    "arguments": json.dumps(
                                        {"query": "synthetic headlines"}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        },
    )


@pytest.mark.parametrize("api,model", [("chat", "gpt-5.2"), ("responses", "gpt-5.4-pro")])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("first_result", ["503", "empty"])
def test_tool_turn_fallback_stays_on_legacy_gateway(
    api, model, stream, enabled, first_result
):
    _exercise_tool_turn(
        api=api,
        model=model,
        stream=stream,
        enabled=enabled,
        first_result=first_result,
    )


@pytest.mark.parametrize("api,model", [("chat", "gpt-5.2"), ("responses", "gpt-5.4-pro")])
@pytest.mark.parametrize("enabled", [False, True])
def test_successful_tool_loop_stays_on_legacy_gateway(api, model, enabled):
    _exercise_tool_turn(
        api=api,
        model=model,
        stream=False,
        enabled=enabled,
        first_result="tool",
    )


def _exercise_tool_turn(*, api, model, stream, enabled, first_result):
    manifest = _manifest()
    settings = _settings(enabled=enabled, manifest=manifest).model_copy(
        update={
            "model_gateway_auth_mode": "api_key",
            "model_gateway_api_key": "synthetic-proxy-key",
            "model_gateway_url": "https://existing-gateway.example.invalid/openai",
            "model_gateway_api_key_header": "S7P-KEY",
        }
    )
    app = create_app(settings)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        internal_user_id="synthetic-owner",
        subject="synthetic-subject",
        issuer="synthetic-issuer",
        provider="entra",
    )
    requests = []
    token_scopes = []
    web = FakeWebClient()

    async def token(scope):
        token_scopes.append(scope)
        return await _token(scope)

    def handler(request):
        requests.append(request)
        index = len(requests)
        request_body = json.loads(request.content)
        request_stream = request_body.get("stream") is True
        if first_result == "503" and index == 1:
            return httpx.Response(503, content=b"synthetic upstream failure")
        if first_result == "empty" and index == 1:
            if request_stream:
                return httpx.Response(
                    200,
                    stream=httpx.ByteStream(_sse(api, empty=True)),
                )
            if api == "responses":
                return httpx.Response(200, json={"output": []})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": ""}}]},
            )
        if first_result == "tool" and index == 1:
            return _tool_call(api)
        if first_result == "tool" and index == 2:
            return _success(api, stream=False, text="Synthetic tool answer")
        return _success(api, stream=request_stream)

    with TestClient(app) as client:
        _inject_web(client, web)
        entry = app.state.catalog.get(model)
        assert entry is not None
        assert entry.api == api
        deployment = app.state.catalog.resolve_deployment(model)
        assert deployment is not None
        manifest["models"] = {
            deployment.deploymentName: {
                "kind": "deployment-name",
                "operations": ["/chat/completions", "/responses"],
                "targets": [
                    {"deployment": deployment.deploymentName, "sku": deployment.sku}
                ],
            }
        }
        settings = settings.model_copy(
            update={"shared_gateway_activation_manifest": json.dumps(manifest)}
        )
        app.state.settings = settings
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.gateway = ModelGatewayClient(
            settings,
            http_client=http,
            shared_gateway_token_provider=token,
        )
        try:
            created = client.post(
                "/api/sessions",
                json={"title": "Synthetic", "model": model},
            )
            assert created.status_code == 201, created.text
            response = client.post(
                "/api/chat",
                json={
                    "sessionId": created.json()["id"],
                    "content": "Search synthetic headlines.",
                    "stream": stream,
                    "allowTools": True,
                    "allowAutomaticMemory": False,
                },
            )
        finally:
            assert client.portal is not None
            client.portal.call(http.aclose)

    assert response.status_code == 200, response.text
    assert len(requests) == 2
    legacy_host = "existing-gateway.example.invalid"
    assert {request.url.host for request in requests} == {legacy_host}
    expected_path = (
        "/openai/responses"
        if api == "responses"
        else f"/openai/deployments/{deployment.deploymentName}/chat/completions"
    )
    assert all(
        request.url.path.endswith(expected_path)
        for request in requests
    )
    assert all("authorization" not in request.headers for request in requests)
    assert token_scopes == []
    if first_result == "tool":
        assert len(web.calls) == 1
        assert "Synthetic tool answer" in response.text
        assert json.loads(requests[0].content).get("tools")
    else:
        assert web.calls == []
        assert "Synthetic fallback answer" in response.text
        assert json.loads(requests[0].content).get("tools")
        if stream:
            assert _frames(response)[0].get("metadata") is not None
