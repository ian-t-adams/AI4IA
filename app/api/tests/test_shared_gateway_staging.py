"""Default-off shared gateway v2 transport and privacy boundary controls."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ai4ia_api import logging_setup
from ai4ia_api.gateway.client import GatewayRequest, ModelGatewayClient, ModelGatewayError
from ai4ia_api.main import create_app
from tests.conftest import make_settings

TENANT_ID = "11111111-2222-4333-8444-555555555555"
CALLER_CLIENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
AUDIENCE = "api://synthetic-gateway"
SCOPE = f"{AUDIENCE}/.default"
DEPLOYMENT = "synthetic-deployment"
TOKEN = "synthetic-access-token"


def _manifest(*, status: str = "resolved", terminal_usage: str = "unverified") -> dict:
    return {
        "contractVersion": "2.0.0",
        "status": status,
        "gateway": {
            "baseUrl": "https://gateway.example.invalid/openai/v1",
            "allowedHosts": ["gateway.example.invalid"],
            "pathPrefix": "/openai/v1",
        },
        "auth": {
            "tenantId": TENANT_ID,
            "issuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
            "audience": AUDIENCE,
            "scope": SCOPE,
            "accessTokenVersion": 2,
            "callerClientId": CALLER_CLIENT_ID,
            "credential": "managed-identity",
        },
        "operations": [
            {"method": "POST", "path": "/chat/completions", "stream": "allowed"},
            {"method": "POST", "path": "/responses", "stream": "allowed"},
        ],
        "models": {
            DEPLOYMENT: {
                "kind": "deployment-name",
                "operations": ["/chat/completions", "/responses"],
                "targets": [
                    {"deployment": DEPLOYMENT, "sku": "GlobalStandard"},
                ],
            },
        },
        "retry": {
            "owner": "gateway",
            "clientSdkMaxRetries": 0,
            "clientAppFailover": False,
            "replayAfterFirstByte": False,
            "retryAfterHonored": True,
        },
        "responses": {
            "storeRequired": False,
            "forbiddenFields": ["previous_response_id", "background", "conversation"],
        },
        "streaming": {
            "format": "text/event-stream",
            "cancellation": "client-disconnect",
            "terminalUsage": terminal_usage,
        },
        "correlation": {
            "responseHeader": "x-S7P-ID",
            "servedBackendGuaranteed": False,
        },
        "privacy": {
            "reviewCapture": "off",
            "userAttribution": "disabled",
            "forbiddenRequestHeaders": [
                "api-key",
                "x-LLMModel",
                "S7P-Model-Override",
                "x-backend-affinity",
                "x-S7PPriority",
                "x-PolicyCycleCounter",
                "S7PDEBUG",
                "X-MS-CLIENT-PRINCIPAL-ID",
                "X-UserID",
            ],
        },
        "bypass": {
            "automaticDirectFallback": False,
            "operatorBypass": "clear-base-url-with-logged-marker",
            "captureDenyPreservedOnBypass": True,
        },
    }


def _settings(*, enabled: bool = True, manifest: dict | None = None):
    return make_settings(
        env="dev",
        auth_provider="entra",
        entra_tenant_id=TENANT_ID,
        entra_audience="api://synthetic-ai4ia",
        model_gateway_url="https://existing-gateway.example.invalid",
        model_gateway_allowed_hosts="existing-gateway.example.invalid",
        shared_gateway_enabled=enabled,
        shared_gateway_activation_manifest=(
            json.dumps(manifest) if manifest is not None else None
        ),
        AZURE_CLIENT_ID=CALLER_CLIENT_ID,
    )


def _client(http: httpx.AsyncClient, *, manifest: dict | None = None) -> ModelGatewayClient:
    return ModelGatewayClient(
        _settings(manifest=manifest or _manifest()),
        http_client=http,
        shared_gateway_token_provider=_token,
    )


async def _token(scope: str) -> str:
    assert scope == SCOPE
    return TOKEN


def test_off_mode_preserves_current_request_even_with_manifest_configuration():
    original = ModelGatewayClient(_settings(enabled=False))
    disabled = ModelGatewayClient(_settings(enabled=False, manifest=_manifest()))
    for stream in (False, True):
        assert disabled.build_request(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            stream=stream,
            correlation_id="synthetic-correlation",
        ) == original.build_request(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            stream=stream,
            correlation_id="synthetic-correlation",
        )
    assert disabled.image_edit_url(DEPLOYMENT) == original.image_edit_url(DEPLOYMENT)
    assert disabled.transcription_url(DEPLOYMENT) == original.transcription_url(DEPLOYMENT)


async def test_off_mode_keeps_the_existing_http_route_and_auth_headers():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "existing route"}}],
        })

    async def unexpected_token(scope: str) -> str:
        raise AssertionError("OFF mode must not request a shared gateway token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelGatewayClient(
            _settings(enabled=False, manifest=_manifest()),
            http_client=http,
            shared_gateway_token_provider=unexpected_token,
        )
        await client.complete(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            correlation_id="synthetic-correlation",
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )

    assert len(requests) == 1
    assert requests[0].url.host == "existing-gateway.example.invalid"
    assert requests[0].url.path.endswith(
        f"/deployments/{DEPLOYMENT}/chat/completions"
    )
    assert requests[0].headers["x-correlation-id"] == "synthetic-correlation"
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "manifest",
    [None, _manifest(status="schema-only")],
)
def test_enablement_requires_a_resolved_v2_manifest(manifest, monkeypatch):
    settings = _settings(manifest=manifest)
    with pytest.raises(RuntimeError, match="resolved shared gateway contract"):
        settings.validate_shared_gateway()
    with pytest.raises(RuntimeError, match="resolved shared gateway contract"):
        ModelGatewayClient(settings)

    with pytest.raises(RuntimeError, match="resolved shared gateway contract"):
        create_app(settings)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("auth", "scope"), "api://wrong/.default"),
        (("auth", "callerClientId"), "99999999-9999-4999-8999-999999999999"),
        (("gateway", "baseUrl"), "https://unlisted.example.invalid/openai/v1"),
        (("retry", "clientSdkMaxRetries"), 1),
        (("privacy", "userAttribution"), "enabled"),
    ],
)
def test_enablement_refuses_manifest_contract_mismatches(path, replacement):
    manifest = _manifest()
    manifest[path[0]][path[1]] = replacement
    with pytest.raises(RuntimeError):
        _settings(manifest=manifest).validate_shared_gateway()


def test_manifest_errors_do_not_echo_host_ids_or_credentials():
    manifest = _manifest(status="schema-only")
    settings = _settings(manifest=manifest).model_copy(update={
        "model_gateway_api_key": "synthetic-proxy-secret",
    })
    with pytest.raises(RuntimeError) as error:
        settings.validate_shared_gateway()
    message = str(error.value)
    for value in (
        manifest["gateway"]["baseUrl"],
        manifest["auth"]["callerClientId"],
        manifest["auth"]["audience"],
        "synthetic-proxy-secret",
    ):
        assert value not in message


def test_manifest_has_a_bounded_utf8_size():
    raw = json.dumps(_manifest())
    at_limit = _settings().model_copy(update={
        "shared_gateway_activation_manifest": raw + " " * (65536 - len(raw)),
    })
    at_limit.validate_shared_gateway()
    over_limit = _settings().model_copy(update={
        "shared_gateway_activation_manifest": raw + " " * (65537 - len(raw)),
    })
    with pytest.raises(RuntimeError, match="exceeds 65536 UTF-8 bytes"):
        over_limit.validate_shared_gateway()


def test_manifest_accepts_lowercase_key_with_case_preserved_target():
    manifest = _manifest()
    manifest["models"][DEPLOYMENT]["targets"][0]["deployment"] = "Synthetic-Deployment"
    _settings(manifest=manifest).validate_shared_gateway()


def test_manifest_deployment_name_key_must_match_lowercased_target():
    manifest = _manifest()
    manifest["models"] = {
        "different-deployment": {
            "kind": "deployment-name",
            "operations": ["/chat/completions", "/responses"],
            "targets": [
                {"deployment": "Synthetic-Deployment", "sku": "GlobalStandard"},
            ],
        },
    }
    with pytest.raises(RuntimeError, match="deployment-name keys must match"):
        _settings(manifest=manifest).validate_shared_gateway()


async def test_shared_gateway_telemetry_contains_metadata_not_request_or_reply_content(monkeypatch):
    provider = TracerProvider(resource=Resource({"service.name": "ai4ia-api"}))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    monkeypatch.setattr(logging_setup, "_telemetry_configured", True)
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    monkeypatch.setenv("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    prompt = "synthetic-private-prompt-and-user"
    reply = "synthetic-private-reply"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "model": DEPLOYMENT,
            "choices": [{"message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        })

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            settings = _settings(manifest=_manifest()).model_copy(update={
                "applicationinsights_connection_string": "InstrumentationKey=synthetic",
            })
            client = ModelGatewayClient(
                settings,
                http_client=http,
                shared_gateway_token_provider=_token,
            )
            await client.complete(
                deployment=DEPLOYMENT,
                messages=[{"role": "user", "content": prompt}],
                shared_gateway_eligible=True,
                shared_gateway_sku="GlobalStandard",
            )
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        emitted = json.dumps({
            "name": spans[0].name,
            "attributes": dict(spans[0].attributes),
            "events": [event.name for event in spans[0].events],
        })
        assert prompt not in emitted
        assert reply not in emitted
        assert TOKEN not in emitted
    finally:
        provider.shutdown()


async def test_chat_uses_only_manifest_model_path_and_managed_identity_headers():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "model": DEPLOYMENT,
            "choices": [{"message": {"role": "assistant", "content": "synthetic reply"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        result = await client.complete(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            correlation_id="synthetic-correlation",
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )

    assert result["choices"][0]["message"]["content"] == "synthetic reply"
    assert "usage" not in result
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://gateway.example.invalid/openai/v1/chat/completions"
    assert json.loads(request.content)["model"] == DEPLOYMENT
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-correlation-id" not in request.headers
    assert "Ocp-Apim-Subscription-Key" not in request.headers
    for forbidden in _manifest()["privacy"]["forbiddenRequestHeaders"]:
        assert forbidden.lower() not in {name.lower() for name in request.headers}
    for gateway_identity_header in (
        "x-gw-app-id",
        "x-gw-partition",
        "x-ms-client-principal",
        "x-ms-client-principal-id",
        "x-userid",
    ):
        assert gateway_identity_header not in {name.lower() for name in request.headers}
    body = json.loads(request.content)
    assert not ({"partition", "app_id", "appid", "azp"} & body.keys())


async def test_alias_routing_requires_exact_lowercase_alias_operation_and_sku():
    requests: list[httpx.Request] = []
    manifest = _manifest()
    manifest["models"] = {
        "synthetic-alias": {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [
                {"deployment": DEPLOYMENT, "sku": "GlobalStandard"},
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "synthetic alias"}}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http, manifest=manifest).complete(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )

    assert result["choices"][0]["message"]["content"] == "synthetic alias"
    assert json.loads(requests[0].content)["model"] == "synthetic-alias"


async def test_unlisted_model_fails_without_falling_back_or_requesting_a_token():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": []})

    async def unexpected_token(scope: str) -> str:
        raise AssertionError("an unlisted model must be rejected before token acquisition")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelGatewayClient(
            _settings(manifest=_manifest()),
            http_client=http,
            shared_gateway_token_provider=unexpected_token,
        )
        with pytest.raises(ModelGatewayError, match="not eligible"):
            await client.complete(
                deployment="unlisted-deployment",
                messages=[],
                shared_gateway_eligible=True,
            )
    assert calls == []


async def test_ineligible_specialist_request_keeps_its_existing_route():
    async def unexpected_token(scope: str) -> str:
        raise AssertionError("an ineligible specialist route must not request a shared token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )
    ) as http:
        client = ModelGatewayClient(
            _settings(manifest=_manifest()),
            http_client=http,
            shared_gateway_token_provider=unexpected_token,
        )
        request = GatewayRequest(
            url="https://existing-gateway.example.invalid/providers/mistral/azure/ocr",
            headers={"api-key": "synthetic-provider-key"},
            json={"model": "mistral-ocr"},
        )
        selected, shared_url = await client._shared_request(
            request,
            deployment=DEPLOYMENT,
            api="mistral_ocr",
            eligible=False,
            sku="Standard",
        )

    assert selected is request
    assert selected.url == "https://existing-gateway.example.invalid/providers/mistral/azure/ocr"
    assert shared_url is None


async def test_shared_gateway_requires_the_exact_manifest_sku_before_token_acquisition():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": []})

    async def unexpected_token(scope: str) -> str:
        raise AssertionError("an unlisted SKU must be rejected before token acquisition")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelGatewayClient(
            _settings(manifest=_manifest()),
            http_client=http,
            shared_gateway_token_provider=unexpected_token,
        )
        with pytest.raises(ModelGatewayError, match="not eligible"):
            await client.complete(
                deployment=DEPLOYMENT,
                messages=[],
                shared_gateway_eligible=True,
                shared_gateway_sku="Standard",
            )
    assert calls == []


async def test_mixed_case_deployment_uses_lowercase_key_and_exact_target():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "synthetic reply"}}],
        })

    manifest = _manifest()
    manifest["models"] = {
        "synthetic-deployment": {
            "kind": "deployment-name",
            "operations": ["/chat/completions"],
            "targets": [
                {"deployment": "Synthetic-Deployment", "sku": "GlobalStandard"},
            ],
        },
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http, manifest=manifest).complete(
            deployment="Synthetic-Deployment",
            messages=[{"role": "user", "content": "synthetic prompt"}],
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )

    assert result["choices"][0]["message"]["content"] == "synthetic reply"
    assert len(requests) == 1
    assert str(requests[0].url) == "https://gateway.example.invalid/openai/v1/chat/completions"
    assert json.loads(requests[0].content)["model"] == "synthetic-deployment"


async def test_mixed_case_target_mismatch_fails_before_token_or_http():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": []})

    async def unexpected_token(scope: str) -> str:
        raise AssertionError("a case-mismatched deployment target must not be admitted")

    manifest = _manifest()
    manifest["models"] = {
        "synthetic-deployment": {
            "kind": "deployment-name",
            "operations": ["/chat/completions"],
            "targets": [
                {"deployment": "Synthetic-deployment", "sku": "GlobalStandard"},
            ],
        },
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelGatewayClient(
            _settings(manifest=manifest),
            http_client=http,
            shared_gateway_token_provider=unexpected_token,
        )
        with pytest.raises(ModelGatewayError, match="not eligible"):
            await client.complete(
                deployment="Synthetic-Deployment",
                messages=[{"role": "user", "content": "synthetic prompt"}],
                shared_gateway_eligible=True,
                shared_gateway_sku="GlobalStandard",
            )

    assert requests == []


async def test_responses_is_explicitly_non_storing_and_uses_v2_path():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "model": DEPLOYMENT,
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "synthetic response"}],
            }],
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        result = await client.complete(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            api="responses",
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )
    assert result["choices"][0]["message"]["content"] == "synthetic response"
    assert "usage" not in result
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://gateway.example.invalid/openai/v1/responses"
    assert body["model"] == DEPLOYMENT
    assert body["store"] is False
    assert not ({"previous_response_id", "background", "conversation"} & body.keys())


@pytest.mark.parametrize("forbidden", ["previous_response_id", "background", "conversation"])
async def test_stateful_responses_fields_are_rejected_before_token_or_http(forbidden):
    calls: list[httpx.Request] = []
    manifest = _manifest()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    async def unexpected_token(scope: str) -> str:
        raise AssertionError("stateful requests must fail before token acquisition")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ModelGatewayClient(
            _settings(manifest=manifest),
            http_client=http,
            shared_gateway_token_provider=unexpected_token,
        )
        request = GatewayRequest(
            url="https://existing.example.invalid/responses",
            headers={"Authorization": "untrusted"},
            json={"model": DEPLOYMENT, "store": False, forbidden: False},
        )
        with pytest.raises(ModelGatewayError, match="stateless"):
            await client._shared_request(
                request, deployment=DEPLOYMENT, api="responses", eligible=True,
                sku="GlobalStandard",
            )
    assert calls == []


async def test_gateway_status_retry_after_and_no_app_retry_or_fallback():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "17"}, text="synthetic throttling")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        with pytest.raises(ModelGatewayError) as error:
            await client.complete(
                deployment=DEPLOYMENT,
                messages=[],
                shared_gateway_eligible=True,
                shared_gateway_sku="GlobalStandard",
            )
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 17
    assert error.value.shared_gateway
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("api", "streaming", "path"),
    [
        ("chat", False, "/chat/completions"),
        ("chat", True, "/chat/completions"),
        ("responses", False, "/responses"),
        ("responses", True, "/responses"),
    ],
)
@pytest.mark.parametrize("status_code", [400, 401, 403])
async def test_gateway_rejections_are_propagated_without_retry_or_fallback(
    api: str, streaming: bool, path: str, status_code: int,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, text="synthetic gateway rejection")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        with pytest.raises(ModelGatewayError) as error:
            if streaming:
                async for _ in client.stream(
                    deployment=DEPLOYMENT,
                    messages=[{"role": "user", "content": "synthetic prompt"}],
                    api=api,
                    shared_gateway_eligible=True,
                    shared_gateway_sku="GlobalStandard",
                ):
                    pass
            else:
                await client.complete(
                    deployment=DEPLOYMENT,
                    messages=[{"role": "user", "content": "synthetic prompt"}],
                    api=api,
                    shared_gateway_eligible=True,
                    shared_gateway_sku="GlobalStandard",
                )

    assert error.value.status_code == status_code
    assert error.value.shared_gateway
    assert error.value.retry_after_seconds is None
    assert len(requests) == 1
    assert requests[0].url.host == "gateway.example.invalid"
    assert requests[0].url.path == f"/openai/v1{path}"


async def test_sse_stream_is_single_attempt_and_unverified_usage_stays_unknown():
    requests: list[httpx.Request] = []
    event_stream = (
        'data: {"choices":[{"delta":{"content":"synthetic"} }]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=event_stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        chunks = [
            chunk async for chunk in client.stream(
                deployment=DEPLOYMENT,
                messages=[{"role": "user", "content": "synthetic prompt"}],
                shared_gateway_eligible=True,
                shared_gateway_sku="GlobalStandard",
            )
        ]
    assert len(requests) == 1
    assert [chunk.delta for chunk in chunks if chunk.delta] == ["synthetic"]
    assert chunks[-1].done
    assert all(chunk.usage is None for chunk in chunks)


async def test_client_disconnect_closes_shared_gateway_sse_stream():
    closed = asyncio.Event()
    first_frame_sent = asyncio.Event()
    continue_stream = asyncio.Event()

    class DisconnectStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            first_frame_sent.set()
            await continue_stream.wait()
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            closed.set()
            continue_stream.set()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=DisconnectStream(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        stream = _client(http).stream(
            deployment=DEPLOYMENT,
            messages=[{"role": "user", "content": "synthetic prompt"}],
            shared_gateway_eligible=True,
            shared_gateway_sku="GlobalStandard",
        )
        first = await anext(stream)
        assert first.delta == "first"
        await stream.aclose()
    assert closed.is_set()
