"""Synthetic chat API boundary controls using the real gateway client."""

from __future__ import annotations

import json
import ipaddress
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.config import Settings
from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.auth.dependencies import get_current_user
from ai4ia_api.catalog import ModelCatalog, load_catalog
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from tests.test_shared_gateway_staging import (
    TOKEN, _catalog_manifest, _manifest, _settings, _token,
)


def _catalog_with_api_override(catalog: ModelCatalog, model_id: str, api: str) -> ModelCatalog:
    isolated = catalog.model_copy(deep=True)
    entry = isolated.get(model_id)
    assert entry is not None
    entry.api = api
    return isolated


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    original_dns = socket.getaddrinfo

    def check(address):
        try:
            allowed = ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise AssertionError("Synthetic chat controls must not open external sockets")

    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)

    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            check(address)
        return original_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    def getaddrinfo(host, *args, **kwargs):
        check((host,))
        return original_dns(host, *args, **kwargs)

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


def test_settings_sources_ignore_ambient_environment(monkeypatch):
    monkeypatch.setenv("AI4IA_ENV", "production")
    monkeypatch.setenv("AI4IA_SHARED_GATEWAY_ENABLED", "true")
    settings = Settings()
    assert settings.env == "local"
    assert settings.shared_gateway_enabled is False


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_external_socket_guard(method):
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="must not open external sockets"):
            getattr(sock, method)(("203.0.113.1", 443))


def test_external_dns_guard():
    with pytest.raises(AssertionError, match="must not open external sockets"):
        socket.getaddrinfo("gateway.example.invalid", 443)


@pytest.mark.parametrize("api", ["chat", "responses"])
def test_api_override_does_not_mutate_cached_catalog(api):
    settings = _settings(enabled=False, manifest=_manifest())
    catalog = load_catalog(
        settings.model_catalog_path,
        settings.data_residency,
        settings.claude_runtime_enabled,
    )
    cached_entry = catalog.get("gpt-5.2")
    assert cached_entry is not None
    assert cached_entry.api == "chat"

    isolated = _catalog_with_api_override(catalog, "gpt-5.2", api)

    isolated_entry = isolated.get("gpt-5.2")
    assert isolated_entry is not None
    assert isolated_entry.api == api
    assert cached_entry.api == "chat"


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("retry", "expected_retry"),
    [
        ("17", 17),
        ("0", 0),
        ("86400", 86400),
        ("Thu, 01 Jan 1970 00:00:00 GMT", 0),
        (None, None),
        ("invalid", None),
        ("86401", None),
        ("-1", None),
        ("1.5", None),
        ("NaN", None),
        ("Infinity", None),
        ("9" * 129, None),
        ("\u0661", None),
    ],
)
def test_chat_api_shared_throttling(api, stream, enabled, retry, expected_retry):
    _exercise(api, stream, enabled, 429, retry, expected_retry)


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("status", [200, 401, 503])
def test_chat_api_non_throttled_controls(api, stream, enabled, status):
    _exercise(api, stream, enabled, status, "17", None)


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_shared_redirect_uses_existing_sanitized_api_error(api, stream):
    _exercise(api, stream, True, 307, None, None)


def _exercise(api, stream, enabled, status, retry, expected_retry, *, fail_persistence=False):
    manifest, deployment_name = _catalog_manifest("gpt-5.2")
    settings = _settings(
        enabled=enabled, manifest=manifest, deployments=deployment_name,
    ).model_copy(
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
    tokens = []
    selected_partitions = []
    failed_writes = []

    async def token(scope):
        assert enabled, "OFF must not acquire shared credentials"
        tokens.append(scope)
        return await _token(scope)

    def handler(request):
        requests.append(request)
        headers = {"S7PREQUEUE": "true"}
        if retry is not None:
            headers["retry-after"] = retry.encode("utf-8")
        if enabled:
            operation = "/responses" if api == "responses" else "/chat/completions"
            routes = {
                "ai4ia": {
                    "token": TOKEN,
                    "models": {deployment.deploymentName.lower(): {operation}},
                },
                "shared": {
                    "token": "synthetic-shared-partition-token",
                    "models": {"shared-only": {"/chat/completions"}},
                },
            }
            authorization = request.headers.get("authorization")
            partition = next(
                (
                    name
                    for name, policy in routes.items()
                    if authorization == f"Bearer {policy['token']}"
                ),
                None,
            )
            if partition is None:
                return httpx.Response(401, headers=headers)
            selected_partitions.append(partition)
            request_model = json.loads(request.content).get("model", "").lower()
            if operation not in routes[partition]["models"].get(request_model, set()):
                return httpx.Response(403, headers=headers)
            assert partition == "ai4ia"
        if status != 200:
            return httpx.Response(
                status, headers=headers, content=b"PRIVATE upstream token endpoint body"
            )
        if api == "chat":
            data = {"choices": [{"message": {"content": "synthetic answer"}}]}
            events = [
                {"choices": [{"delta": {"content": "synthetic answer"}}]},
            ]
        else:
            data = {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "synthetic answer"},
                        ],
                    }
                ]
            }
            events = [
                {"type": "response.output_text.delta", "delta": "synthetic answer"},
                {"type": "response.completed", "response": data},
            ]
        success_headers = {"S7PREQUEUE": "true"}
        body = (
            "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            if stream
            else json.dumps(data)
        )
        return httpx.Response(
            200,
            headers=success_headers,
            stream=httpx.ByteStream(body.encode()),
        )

    with TestClient(app) as client:
        app.state.catalog = _catalog_with_api_override(
            app.state.catalog, "gpt-5.2", api
        )
        entry = app.state.catalog.get("gpt-5.2")
        assert entry is not None
        assert entry.api == api
        deployment = app.state.catalog.resolve_deployment("gpt-5.2")
        assert deployment is not None
        manifest["models"] = {
            deployment.deploymentName: {
                "kind": "deployment-name",
                "operations": ["/chat/completions", "/responses"],
                "targets": [{"deployment": deployment.deploymentName, "sku": deployment.sku}],
            },
        }
        settings = settings.model_copy(
            update={
                "shared_gateway_activation_manifest": json.dumps(manifest),
            }
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.settings = settings
        app.state.gateway = ModelGatewayClient(
            settings,
            http_client=http,
            shared_gateway_token_provider=token,
        )
        try:
            created = client.post("/api/sessions", json={"title": "Synthetic", "model": "gpt-5.2"})
            assert created.status_code == 201
            session_id = created.json()["id"]
            if fail_persistence:
                original_upsert = app.state.session_repo.upsert_message

                async def upsert(owner, message, **kwargs):
                    if message.role == "assistant" and message.status == "error":
                        failed_writes.append(message.id)
                        raise RuntimeError("synthetic persistence outage")
                    return await original_upsert(owner, message, **kwargs)

                app.state.session_repo.upsert_message = upsert
            response = client.post(
                "/api/chat",
                json={
                    "sessionId": session_id,
                    "content": "synthetic prompt",
                    "stream": stream,
                    "allowTools": False,
                    "allowAutomaticMemory": False,
                },
            )
            messages = client.get(f"/api/sessions/{session_id}/messages").json()
        finally:
            assert client.portal is not None
            client.portal.call(http.aclose)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert selected_partitions == (["ai4ia"] if enabled else [])
    operation = "/responses" if api == "responses" else "/chat/completions"
    assert requests[0].url.path == (
        f"/openai/v1{operation}"
        if enabled
        else "/openai/responses"
        if api == "responses"
        else f"/openai/deployments/{deployment.deploymentName}{operation}"
    )
    assert requests[0].url.host == (
        "gateway.example.invalid" if enabled else "existing-gateway.example.invalid"
    )
    assert len(tokens) == int(enabled)
    assert requests[0].headers.get("authorization") == (f"Bearer {TOKEN}" if enabled else None)
    assert response.headers.get("S7PREQUEUE") is None
    assert "S7PREQUEUE" not in response.text
    request_body = json.loads(requests[0].content)
    if api == "responses":
        assert request_body["store"] is False
        assert "input" in request_body
    else:
        assert "messages" in request_body
    assistant = [message for message in messages if message["role"] == "assistant"][-1]
    if not fail_persistence:
        assert assistant["status"] == ("complete" if status == 200 else "error")
    assert bool(failed_writes) == fail_persistence
    if status == 200:
        assert response.status_code == 200
        assert assistant["content"] == "synthetic answer"
        assert "synthetic answer" in response.text
        return
    for private in ("PRIVATE", TOKEN, "example.invalid"):
        assert private not in response.text
        assert private not in json.dumps(messages)
    if stream:
        assert response.status_code == 200
        frames = _frames(response)
        assert "metadata" in frames[0]
        error = next(frame for frame in frames if "error" in frame)
        assert "[DONE]" not in response.text
        if enabled and status == 429:
            assert error["status"] == 429
            if expected_retry is None:
                assert "retryAfterSeconds" not in error
            else:
                assert error["retryAfterSeconds"] == expected_retry
            assert "throttled" in error["error"]
            if expected_retry is not None:
                assert f"{expected_retry} seconds" in error["error"]
            else:
                assert "Try again later." in error["error"]
            if fail_persistence:
                assert error["persistenceFailed"] is True
                assert "could not be saved" in error["error"]
        else:
            assert error == {"error": "Chat completion failed."}
    else:
        assert response.status_code == (429 if enabled and status == 429 else 502)
        assert response.headers.get("retry-after") == (
            str(expected_retry)
            if enabled and status == 429 and expected_retry is not None
            else None
        )
        if enabled and status == 429:
            assert "throttled" in response.json()["detail"]


@pytest.mark.parametrize("api", ["chat", "responses"])
def test_shared_throttling_preserves_stream_persistence_failure(api):
    _exercise(api, True, True, 429, "17", 17, fail_persistence=True)
