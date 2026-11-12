"""Default-off, process-local shared-gateway chat-pilot isolation controls."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.auth.dependencies import get_current_user
from ai4ia_api.catalog import DeploymentOption, ModelCatalog, load_catalog
from ai4ia_api.chat_pilot import IsolatedChatPilot
from ai4ia_api.config import Settings
from ai4ia_api.gateway.client import ModelGatewayClient, ModelGatewayError
from ai4ia_api.main import create_app
from ai4ia_api.memory.service import NoopMemoryService
from tests.test_shared_gateway_staging import (
    CALLER_CLIENT_ID,
    _manifest,
    _settings,
    _token,
)

PILOT_WEB_CLIENT_ID = "00000000-0000-0000-0000-000000000098"


@pytest.fixture(autouse=True)
def _deny_real_http_transport(monkeypatch):
    async def deny_async(*_args, **_kwargs):
        pytest.fail("Real HTTP transport is prohibited in pilot isolation tests.")

    def deny_sync(*_args, **_kwargs):
        pytest.fail("Real HTTP transport is prohibited in pilot isolation tests.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)


def _pilot_configuration(
    *, api: str = "chat", **overrides
) -> tuple[Settings, ModelCatalog, dict]:
    values = {
        "chat_pilot_isolation_enabled": True,
        "chat_pilot_allowed_oid": "00000000-0000-0000-0000-000000000099",
        "chat_pilot_allowed_azp": PILOT_WEB_CLIENT_ID,
        "entra_audience": "api://97ce0344-1bef-4ab6-94bb-be938e0c40d9",
        "allow_dev_auth": False,
        "session_store": "memory",
        "cosmos_endpoint": None,
        "memory_store": "disabled",
        "resource_metrics_enabled": False,
        "entitlements_enabled": False,
        "user_directory_enabled": False,
        "model_gateway_url": "",
        "model_gateway_auth_mode": "none",
        "model_gateway_api_key": None,
    }
    values.update(overrides)
    settings = _settings(enabled=True, manifest=_manifest()).model_copy(update=values)
    catalog = load_catalog(
        settings.model_catalog_path,
        settings.data_residency,
        settings.claude_runtime_enabled,
    ).model_copy(deep=True)
    entry = catalog.get("gpt-5.2")
    assert entry is not None
    entry.api = api
    deployment = catalog.resolve_deployment("gpt-5.2")
    assert deployment is not None
    manifest = _manifest()
    manifest["operations"] = [{
        "method": "POST",
        "path": "/chat/completions",
        "stream": "allowed",
    }]
    manifest["models"] = {
        "gpt-5.2": {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [{
                "deployment": deployment.deploymentName,
                "sku": deployment.sku,
            }],
        },
    }
    settings = settings.model_copy(update={
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })
    return settings, catalog, manifest


def _pilot_settings(**overrides) -> Settings:
    return _pilot_configuration(**overrides)[0]


@pytest.fixture(autouse=True)
def isolate_pilot_process_environment(monkeypatch):
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "WEB_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_pilot_is_default_off_and_rejects_external_state_and_enabled_features():
    ordinary = Settings(_env_file=None)
    assert ordinary.chat_pilot_isolation_enabled is False
    ordinary.validate_runtime()

    unsafe = (
        {"cosmos_endpoint": "https://prod.invalid"},
        {"document_blob_account_url": "https://prod.invalid"},
        {"image_blob_account_url": "https://prod.invalid"},
        {"search_endpoint": "https://prod.invalid"},
        {"applicationinsights_connection_string": "InstrumentationKey=synthetic"},
        {"auth_provider": "dev"},
        {"chat_pilot_allowed_oid": None},
        {"chat_pilot_allowed_azp": None},
        {"chat_pilot_allowed_oid": "not-a-guid"},
        {"chat_pilot_allowed_oid": PILOT_WEB_CLIENT_ID},
        {"chat_pilot_allowed_oid": "97ce0344-1bef-4ab6-94bb-be938e0c40d9"},
        {"allow_dev_auth": True},
        {"shared_gateway_enabled": False},
        {"durable_workflows_enabled": True},
        {"document_understanding_enabled": True},
        {"resource_metrics_enabled": True},
    )
    for update in unsafe:
        with pytest.raises(RuntimeError, match="CHAT_PILOT_ISOLATION"):
            _pilot_settings(**update).validate_runtime()


def test_pilot_startup_requires_only_the_resolved_shared_gateway_route():
    settings = _pilot_settings(
        model_gateway_url="",
        model_gateway_auth_mode="none",
        model_gateway_api_key=None,
        model_gateway_allowed_hosts=None,
    )
    settings.validate_runtime()

    missing_contract = settings.model_copy(update={
        "shared_gateway_activation_manifest": None,
    })
    with pytest.raises(RuntimeError, match="resolved shared gateway"):
        missing_contract.validate_runtime()


@pytest.mark.parametrize(
    "legacy_config",
    [
        {"model_gateway_auth_mode": "api_key"},
        {"model_gateway_auth_mode": "bearer"},
        {"model_gateway_api_key": "synthetic-legacy-key"},
    ],
)
def test_pilot_rejects_legacy_gateway_auth(legacy_config):
    with pytest.raises(RuntimeError, match="CHAT_PILOT_ISOLATION"):
        _pilot_settings(**legacy_config).validate_runtime()


def test_nonpilot_still_requires_legacy_gateway_auth_outside_local():
    ordinary = _pilot_settings(
        chat_pilot_isolation_enabled=False,
        shared_gateway_enabled=False,
        model_gateway_auth_mode="none",
        model_gateway_api_key=None,
    )
    with pytest.raises(RuntimeError, match="Model gateway auth must not be 'none'"):
        ordinary.validate_runtime()


def test_unsafe_pilot_fails_before_logging_or_telemetry(monkeypatch):
    import ai4ia_api.main as main

    logging = []
    telemetry = []
    monkeypatch.setattr(main, "configure_logging", lambda *_args: logging.append(True))
    monkeypatch.setattr(main, "configure_telemetry", lambda *_args: telemetry.append(True))
    with pytest.raises(RuntimeError, match="CHAT_PILOT_ISOLATION"):
        create_app(_pilot_settings(cosmos_endpoint="https://prod.invalid"))
    assert logging == []
    assert telemetry == []


def test_pilot_rejects_mismatched_caller_manifest_before_side_effects(monkeypatch):
    import ai4ia_api.main as main

    settings, _catalog, manifest = _pilot_configuration()
    manifest["auth"]["callerClientId"] = "99999999-9999-4999-8999-999999999999"
    settings = settings.model_copy(update={
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })
    events = []

    def reject_http_client(*_args, **_kwargs):
        pytest.fail("pilot startup must reject the manifest before creating clients")

    monkeypatch.setattr(main, "configure_logging", lambda *_args: events.append("logging"))
    monkeypatch.setattr(
        main, "configure_telemetry", lambda *_args: events.append("telemetry")
    )
    monkeypatch.setattr(main.httpx, "AsyncClient", reject_http_client)

    with pytest.raises(RuntimeError, match="API identity is invalid"):
        create_app(settings)

    assert events == []


@pytest.mark.parametrize("invalid_auth", ["missing-api-identity", "non-managed-identity"])
def test_pilot_rejects_unbound_gateway_identity_before_side_effects(
    invalid_auth, monkeypatch,
):
    import ai4ia_api.main as main

    settings, _catalog, manifest = _pilot_configuration()
    if invalid_auth == "missing-api-identity":
        settings = settings.model_copy(update={"api_identity_client_id": None})
    else:
        manifest["auth"]["credential"] = "api-key"
        settings = settings.model_copy(update={
            "shared_gateway_activation_manifest": json.dumps(manifest),
        })

    events = []
    monkeypatch.setattr(main, "configure_logging", lambda *_args: events.append("logging"))
    monkeypatch.setattr(
        main, "configure_telemetry", lambda *_args: events.append("telemetry")
    )
    monkeypatch.setattr(
        main.httpx, "AsyncClient",
        lambda *_args, **_kwargs: pytest.fail(
            "pilot startup must reject an unbound gateway identity before creating clients"
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="token issuer, audience, version, credential or API identity",
    ):
        create_app(settings)

    assert events == []


@pytest.mark.parametrize(
    "fragment",
    [
        '"status": "resolved",',
        '"baseUrl": "https://gateway.example.invalid/openai/v1",',
    ],
)
def test_pilot_rejects_v20_duplicate_keys_at_startup_without_changing_nonpilot(
    fragment, monkeypatch,
):
    settings, _catalog, manifest = _pilot_configuration()
    raw = json.dumps(manifest)
    assert raw.count(fragment) == 1
    raw = raw.replace(fragment, f'{fragment} {fragment}', 1)
    settings = settings.model_copy(update={
        "shared_gateway_activation_manifest": raw,
    })
    events = []
    import ai4ia_api.main as main

    monkeypatch.setattr(main, "configure_logging", lambda *_args: events.append("logging"))
    monkeypatch.setattr(
        main, "configure_telemetry", lambda *_args: events.append("telemetry")
    )
    with pytest.raises(RuntimeError, match="duplicate JSON key"):
        create_app(settings)
    assert events == []

    ordinary = _settings(enabled=True, manifest=manifest).model_copy(update={
        "shared_gateway_activation_manifest": raw,
    })
    ordinary.validate_shared_gateway()


def test_pilot_refuses_environment_proxy_and_multiple_worker_overrides_before_logging(
    monkeypatch,
):
    import ai4ia_api.main as main

    events = []
    monkeypatch.setattr(main, "configure_logging", lambda *_args: events.append("logging"))
    monkeypatch.setattr(
        main, "configure_telemetry", lambda *_args: events.append("telemetry")
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid")
    with pytest.raises(RuntimeError, match="CHAT_PILOT_ISOLATION"):
        create_app(_pilot_settings())
    assert events == []

    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="CHAT_PILOT_ISOLATION"):
        create_app(_pilot_settings())
    assert events == []


def test_gateway_clients_disable_environment_proxy_only_for_pilot():
    async def exercise():
        pilot = ModelGatewayClient(
            _pilot_settings(),
            shared_gateway_token_provider=_token,
        )
        pilot_http, pilot_owned = pilot._client()
        assert pilot_owned
        assert pilot_http._trust_env is False
        await pilot_http.aclose()

        ordinary = ModelGatewayClient(_settings(enabled=False))
        ordinary_http, ordinary_owned = ordinary._client()
        assert ordinary_owned
        assert ordinary_http._trust_env is True
        await ordinary_http.aclose()

    asyncio.run(exercise())


def test_pilot_validates_before_telemetry_but_default_off_order_is_unchanged(monkeypatch):
    import ai4ia_api.main as main

    events = []
    original_validate = Settings.validate_runtime

    def validate(settings):
        events.append("validate")
        return original_validate(settings)

    monkeypatch.setattr(Settings, "validate_runtime", validate)
    monkeypatch.setattr(main, "configure_logging", lambda *_args: events.append("logging"))
    monkeypatch.setattr(
        main, "configure_telemetry", lambda *_args: events.append("telemetry")
    )

    ordinary = Settings(_env_file=None)
    app = create_app(ordinary)
    assert ordinary.chat_pilot_isolation_enabled is False
    assert IsolatedChatPilot not in [
        middleware.cls for middleware in app.user_middleware
    ]
    assert events == ["logging", "telemetry", "validate"]

    events.clear()
    pilot = _pilot_settings()
    create_app(pilot)
    assert events == ["validate", "logging", "telemetry"]


def test_pilot_rejects_ineligible_gateway_dispatch_without_legacy_fallback():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            gateway = ModelGatewayClient(
                _pilot_settings(),
                http_client=http,
                shared_gateway_token_provider=_token,
            )
            with pytest.raises(ModelGatewayError) as error:
                await gateway.complete(
                    deployment="synthetic-deployment",
                    messages=[{"role": "user", "content": "synthetic"}],
                    shared_gateway_eligible=False,
                )
            assert error.value.shared_gateway

    asyncio.run(exercise())
    assert requests == []


@pytest.mark.parametrize(
    ("api", "deployment"),
    [
        ("anthropic", "synthetic-deployment"),
        ("chat", "claude-synthetic-deployment"),
        ("mai", "synthetic-deployment"),
    ],
)
def test_pilot_rejects_anthropic_and_unsupported_stream_dispatch_before_any_send(
    api, deployment,
):
    requests = []
    token_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async def token(scope: str) -> str:
        token_calls.append(scope)
        return await _token(scope)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            gateway = ModelGatewayClient(
                _pilot_settings(),
                http_client=http,
                shared_gateway_token_provider=token,
            )
            with pytest.raises(ModelGatewayError, match="not eligible"):
                async for _ in gateway.stream(
                    deployment=deployment,
                    messages=[{"role": "user", "content": "synthetic"}],
                    api=api,
                    shared_gateway_eligible=True,
                    shared_gateway_sku="GlobalStandard",
                ):
                    pass

    asyncio.run(exercise())
    assert requests == []
    assert token_calls == []


def test_pilot_model_resolution_does_not_advertise_misclassified_anthropic_deployment():
    settings, _, manifest = _pilot_configuration()
    deployment = "claude-synthetic-deployment"
    manifest["models"] = {
        "gpt-5.2": {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [{"deployment": deployment, "sku": "GlobalStandard"}],
        },
    }
    settings = settings.model_copy(update={
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        ) as http:
            gateway = ModelGatewayClient(
                settings,
                http_client=http,
                shared_gateway_token_provider=_token,
            )
            assert gateway.chat_pilot_model_key(
                "chat", deployment, "GlobalStandard",
            ) is None

    asyncio.run(exercise())


def test_pilot_refuses_responses_before_request_or_token_acquisition():
    settings, catalog, manifest = _pilot_configuration()
    deployment = catalog.resolve_deployment("gpt-5.2")
    assert deployment is not None
    settings.validate_runtime()
    requests = []
    token_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    async def token(scope: str) -> str:
        token_calls.append(scope)
        return await _token(scope)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            gateway = ModelGatewayClient(
                settings,
                http_client=http,
                shared_gateway_token_provider=token,
            )
            assert gateway.chat_pilot_model_key(
                "responses", deployment.deploymentName, deployment.sku,
            ) is None
            with pytest.raises(ModelGatewayError, match="isolated shared-gateway chat"):
                await gateway.complete(
                    deployment=deployment.deploymentName,
                    messages=[{"role": "user", "content": "synthetic"}],
                    api="responses",
                    shared_gateway_eligible=True,
                    shared_gateway_sku=deployment.sku,
                )

    asyncio.run(exercise())
    assert requests == []
    assert token_calls == []


def test_pilot_requires_one_alias_and_one_exact_sku_binding():
    settings, catalog, manifest = _pilot_configuration()
    deployment = catalog.resolve_deployment("gpt-5.2")
    assert deployment is not None
    target = {"deployment": deployment.deploymentName, "sku": deployment.sku}
    manifest["models"] = {
        key: {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [target],
        }
        for key in ("alias-one", "alias-two")
    }
    with pytest.raises(RuntimeError, match="isolated chat pilot"):
        settings.model_copy(update={
            "shared_gateway_activation_manifest": json.dumps(manifest),
        }).validate_runtime()

    manifest["models"] = {
        "gpt-5.2": {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [
                {"deployment": deployment.deploymentName, "sku": "GlobalStandard"},
                {"deployment": deployment.deploymentName, "sku": "Standard"},
            ],
        }
    }
    scoped = settings.model_copy(update={
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })
    with pytest.raises(RuntimeError, match="isolated chat pilot"):
        scoped.validate_runtime()

    manifest["models"] = {
        "gpt-5.2": {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [{
                "deployment": deployment.deploymentName,
                "sku": deployment.sku,
            }],
        }
    }
    scoped = settings.model_copy(update={
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })
    scoped.validate_runtime()
    gateway = ModelGatewayClient(scoped, shared_gateway_token_provider=_token)
    assert gateway.chat_pilot_model_key(
        "chat", deployment.deploymentName, deployment.sku, model_id="gpt-5.2",
    ) == "gpt-5.2"
    assert gateway.chat_pilot_model_key(
        "chat", deployment.deploymentName, deployment.sku, model_id="other-model",
    ) is None
    assert gateway.chat_pilot_model_key(
        "chat", deployment.deploymentName, "ProvisionedManaged",
    ) is None


@pytest.mark.parametrize(
    "manifest_change",
    [
        "responses-operation",
        "extra-model",
        "deployment-name-key",
        "multiple-targets",
    ],
)
def test_pilot_startup_rejects_non_chat_or_extra_model_routes(manifest_change):
    settings, catalog, manifest = _pilot_configuration()
    deployment = catalog.resolve_deployment("gpt-5.2")
    assert deployment is not None
    if manifest_change == "responses-operation":
        manifest["operations"].append({
            "method": "POST", "path": "/responses", "stream": "allowed",
        })
        manifest["models"]["gpt-5.2"]["operations"].append("/responses")
    elif manifest_change == "extra-model":
        manifest["models"]["other-model"] = {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [{
                "deployment": deployment.deploymentName,
                "sku": deployment.sku,
            }],
        }
    elif manifest_change == "multiple-targets":
        manifest["models"]["gpt-5.2"]["targets"].append({
            "deployment": deployment.deploymentName,
            "sku": deployment.sku,
        })
    else:
        manifest["models"]["gpt-5.2"]["kind"] = "deployment-name"

    expected_error = (
        "deployment-name keys must match their target"
        if manifest_change == "deployment-name-key"
        else "isolated chat pilot"
    )
    with pytest.raises(RuntimeError, match=expected_error):
        settings.model_copy(update={
            "shared_gateway_activation_manifest": json.dumps(manifest),
        }).validate_runtime()


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"max_tokens": 256}, 200),
        ({}, 422),
        ({"max_tokens": 1024}, 422),
        ({"max_tokens": 4096}, 422),
        ({"max_tokens": 255}, 422),
        ({"max_tokens": 257}, 422),
        ({"max_tokens": "256"}, 422),
        ({"max_tokens": True}, 422),
        ({"max_tokens": 256, "max_completion_tokens": 256}, 422),
        ({"max_tokens": 256, "max_output_tokens": 4096}, 422),
        ({"max_output_tokens": 256}, 422),
        ('{"max_tokens":256,"max_tokens":256}', 422),
        ('{"max_tokens":256,"max_output_tokens":256}', 422),
    ],
)
def test_pilot_chat_dispatch_requires_exactly_256_output_tokens(
    monkeypatch, params, expected_status,
):
    settings, catalog, manifest = _pilot_configuration()
    model_id = "gpt-5.4-mini"
    deployment = "gpt-5.4-mini-slurmfactory-eastus2-glbl"
    entry = catalog.get(model_id)
    assert entry is not None
    assert entry.api == "chat"
    assert [
        (option.deploymentName, option.sku)
        for option in catalog.eligible_options(entry)
        if option.deploymentName == deployment and option.sku == "GlobalStandard"
    ] == [(deployment, "GlobalStandard")]
    manifest["operations"] = [{
        "method": "POST", "path": "/chat/completions", "stream": "allowed",
    }]
    manifest["models"] = {
        model_id: {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [{"deployment": deployment, "sku": "GlobalStandard"}],
        },
    }
    settings = settings.model_copy(update={
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        events = [
            {"choices": [{"delta": {"content": "synthetic answer"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"data: {json.dumps(event)}\n\n" for event in events
            ) + "data: [DONE]\n\n",
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import ai4ia_api.main as main

    real_gateway = ModelGatewayClient

    def gateway_factory(current: Settings, http_client=None):
        assert http_client is not None and http_client._trust_env is False
        return real_gateway(
            current, http_client=http,
            shared_gateway_token_provider=_token,
        )

    monkeypatch.setattr(main, "ModelGatewayClient", gateway_factory)
    monkeypatch.setattr(main, "load_catalog", lambda *_args: catalog.model_copy(deep=True))
    app = create_app(settings)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        internal_user_id="synthetic-owner",
        subject="synthetic-subject",
        issuer="https://login.example.invalid/",
        tenant_id="synthetic-tenant",
        provider="entra",
    )
    try:
        with TestClient(app) as client:
            response = client.get("/api/models")
            assert response.status_code == 200
            models = response.json()["models"]
            assert [model["id"] for model in models] == [model_id]
            assert [
                (option["deploymentName"], option["sku"])
                for option in models[0]["options"]
            ] == [(deployment, "GlobalStandard")]

            created = client.post(
                "/api/sessions",
                json={"model": model_id, "toolsEnabled": False},
            )
            assert created.status_code == 201
            messages_url = f"/api/sessions/{created.json()['id']}/messages"
            messages_before = client.get(messages_url)
            assert messages_before.status_code == 200
            chat_body = {
                "sessionId": created.json()["id"],
                "content": "synthetic prompt",
                "stream": True,
                "allowTools": False,
                "allowAutomaticMemory": False,
            }
            if isinstance(params, str):
                chat = client.post(
                    "/api/chat",
                    content=json.dumps(chat_body)[:-1] + ',"params":' + params + "}",
                    headers={"content-type": "application/json"},
                )
            else:
                chat = client.post("/api/chat", json={**chat_body, "params": params})
            assert chat.status_code == expected_status
            messages_after = client.get(messages_url)
            assert messages_after.status_code == 200
            if expected_status == 200:
                assert "synthetic answer" in chat.text
                assert len(requests) == 1
                sent = json.loads(requests[0].content)
                assert sent["model"] == model_id
                assert sent["max_completion_tokens"] == 256
                assert "max_tokens" not in sent
            else:
                assert messages_after.json() == messages_before.json()
    finally:
        asyncio.run(http.aclose())

    assert len(requests) == (1 if expected_status == 200 else 0)
    if expected_status == 200:
        assert requests[0].url.path == "/openai/v1/chat/completions"


@pytest.mark.parametrize(
    ("streaming", "shared_post"), [(True, False), (False, False), (False, True)],
)
@pytest.mark.parametrize(
    ("mutation", "replace"),
    [
        ({"max_completion_tokens": 257}, False),
        ({"max_output_tokens": 4096}, False),
        ({"max_output_tokens": 256}, False),
        ({"max_tokens": 256}, False),
        ({"max_tokens": 256}, True),
    ],
)
def test_pilot_rechecks_mutated_dispatch_payload_before_transport(
    monkeypatch, streaming, shared_post, mutation, replace,
):
    settings, _, _ = _pilot_configuration(model_gateway_url="https://gateway.example.invalid")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = ModelGatewayClient(settings, http_client=http, shared_gateway_token_provider=_token)
    req = gateway.build_request(
        deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
        messages=[{"role": "user", "content": "synthetic"}],
        params={"max_tokens": 256},
        stream=streaming,
    )

    import ai4ia_api.gateway.client as gateway_module

    original_admitted_dispatch = gateway_module.admitted_dispatch

    @asynccontextmanager
    async def mutate_dispatch_payload(*args, **kwargs):
        async with original_admitted_dispatch(*args, **kwargs) as admission:
            if replace:
                admission.payload.pop("max_completion_tokens")
            admission.payload.update(mutation)
            yield admission

    monkeypatch.setattr(gateway_module, "admitted_dispatch", mutate_dispatch_payload)

    async def dispatch() -> None:
        with pytest.raises(ModelGatewayError, match="exactly 256 output tokens"):
            if streaming:
                async with gateway._stream_request(
                    http,
                    req,
                    deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
                    evidence=None,
                ):
                    pytest.fail("mutated pilot payload reached the transport")
            else:
                await gateway._post(
                    http,
                    req.url,
                    surface="chat",
                    deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
                    payload=req.json,
                    json=req.json,
                    gateway_url="https://gateway.example.invalid" if shared_post else None,
                )

    try:
        asyncio.run(dispatch())
    finally:
        asyncio.run(http.aclose())
    assert requests == []


@pytest.mark.parametrize(
    "params",
    [
        {"max_output_tokens": 256},
        {"max_tokens": 256, "max_output_tokens": 4096},
        {"max_tokens": 256, "max_completion_tokens": 256},
        {"max_tokens": "256"},
        {"max_tokens": True},
    ],
)
def test_pilot_rejects_ambiguous_raw_gateway_params(params):
    settings, _, _ = _pilot_configuration()
    gateway = ModelGatewayClient(settings, shared_gateway_token_provider=_token)
    with pytest.raises(ModelGatewayError, match="exactly 256 output tokens"):
        gateway.build_request(
            deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
            messages=[],
            params=params,
        )


@pytest.mark.parametrize(
    ("streaming", "shared_post"), [(True, False), (False, False), (False, True)],
)
@pytest.mark.parametrize("pilot", [True, False])
@pytest.mark.parametrize(
    ("body", "valid"),
    [
        (b'{"max_completion_tokens":256}', True),
        (b'{"max_tokens":256}', False),
        (b'{"max_output_tokens":256}', False),
        (b'{"max_completion_tokens":256,"max_output_tokens":4096}', False),
        (b'{"max_completion_tokens":256,"max_output_tokens":256}', False),
        (b'{"max_completion_tokens":256,"max_completion_tokens":256}', False),
        (b'{"max_completion_tokens":257,"max_completion_tokens":256}', False),
        (b'{"max_completion_tokens":256,"max_tokens":256}', False),
        (b'{"max_tokens":256,"max_tokens":256}', False),
        (b'{"max_completion_tokens":"256"}', False),
        (b'{"max_completion_tokens":true}', False),
        (b'{"max_completion_tokens":256.0}', False),
        (b'{}', False),
    ],
)
def test_pilot_checks_serialized_attempt_after_claim(
    monkeypatch, streaming, shared_post, pilot, body, valid,
):
    import ai4ia_api.gateway.client as gateway_module

    settings, _, _ = _pilot_configuration(model_gateway_url="https://gateway.example.invalid")
    gateway = ModelGatewayClient(
        settings.model_copy(update={"chat_pilot_isolation_enabled": pilot}),
        shared_gateway_token_provider=_token,
    )
    requests: list[httpx.Request] = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200, json={}),
    ))
    req = gateway.build_request(
        deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
        messages=[],
        params={"max_tokens": 256},
        stream=streaming,
    )
    attempt = SimpleNamespace(body=json.dumps(req.json).encode())

    def claim(*_args):
        attempt.body = body
        return {}

    attempt.claim = claim
    attempt.check_response = lambda _response: None

    @asynccontextmanager
    async def prepare(*_args, **_kwargs):
        yield attempt

    @asynccontextmanager
    async def bounded_client(*_args):
        yield http

    monkeypatch.setattr(gateway_module, "prepare_attempt", prepare)
    monkeypatch.setattr(gateway_module, "bounded_http_client", bounded_client)

    async def dispatch():
        if streaming:
            async with gateway._stream_request(
                http, req, deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
                evidence=None,
            ):
                pass
        else:
            await gateway._post(
                http, req.url, surface="chat",
                deployment="gpt-5.4-mini-slurmfactory-eastus2-glbl",
                payload=req.json, json=req.json,
                gateway_url="https://gateway.example.invalid" if shared_post else None,
            )

    try:
        if pilot and not valid:
            with pytest.raises(ModelGatewayError, match="exactly 256 output tokens"):
                asyncio.run(dispatch())
            assert requests == []
        else:
            asyncio.run(dispatch())
            assert len(requests) == 1
            assert requests[0].content == body
    finally:
        asyncio.run(http.aclose())


@pytest.mark.parametrize("upstream_status", [200, 429])
def test_pilot_streaming_chat_uses_startup_validated_contract(
    monkeypatch, upstream_status
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if upstream_status != 200:
            return httpx.Response(
                upstream_status,
                headers={"retry-after": "17"},
                json={"error": {"message": "synthetic private failure"}},
            )
        events = [
            {"choices": [{"delta": {"content": "synthetic answer"}}]},
            {"choices": [{"finish_reason": "stop"}]},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"data: {json.dumps(event)}\n\n" for event in events
            ) + "data: [DONE]\n\n",
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings, catalog, manifest = _pilot_configuration()
    import ai4ia_api.main as main

    real_gateway = ModelGatewayClient
    memory = NoopMemoryService()
    memory.warmup = AsyncMock()
    recovery_calls = []
    gateway_manifests = []

    class SyntheticIngestor:
        async def recover_interrupted(self):
            recovery_calls.append(True)

        async def close(self):
            return None

    def gateway_factory(current: Settings, http_client=None):
        gateway_manifests.append(current.shared_gateway_activation_manifest)
        assert http_client is not None
        assert http_client._trust_env is False
        return real_gateway(
            current,
            http_client=http,
            shared_gateway_token_provider=_token,
        )

    monkeypatch.setattr(main, "ModelGatewayClient", gateway_factory)
    monkeypatch.setattr(
        main, "load_catalog", lambda *_args: catalog.model_copy(deep=True)
    )
    monkeypatch.setattr(main, "build_memory_service", lambda *_args, **_kwargs: memory)
    monkeypatch.setattr(
        main,
        "build_document_ingestor",
        lambda *_args, **_kwargs: SyntheticIngestor(),
    )
    monkeypatch.setattr(main, "build_document_retrieval", lambda *_args, **_kwargs: None)
    app = create_app(settings)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        internal_user_id="synthetic-owner",
        subject="synthetic-subject",
        issuer="https://login.example.invalid/",
        tenant_id="synthetic-tenant",
        provider="entra",
    )

    with TestClient(app) as client:
        assert app.state.session_repo.__class__.__name__.startswith("InMemory")
        assert app.state.durable_workflows is None
        entry = app.state.catalog.get("gpt-5.2")
        assert entry is not None
        assert entry.api == "chat"
        deployment = app.state.catalog.resolve_deployment("gpt-5.2")
        assert deployment is not None
        assert app.state.settings is settings
        assert app.state.settings.shared_gateway_activation_manifest == json.dumps(manifest)
        assert app.state.gateway is not None
        models = client.get("/api/models")
        assert models.status_code == 200
        assert [model["id"] for model in models.json()["models"]] == ["gpt-5.2"]

        created = client.post(
            "/api/sessions",
            json={"model": "gpt-5.2", "toolsEnabled": False},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]
        response = client.post("/api/chat", json={
            "sessionId": session_id,
            "content": "synthetic prompt",
            "stream": True,
            "allowTools": False,
            "allowAutomaticMemory": False,
            "params": {"max_tokens": 256},
        })
        assert response.status_code == 200
        if upstream_status == 200:
            assert "synthetic answer" in response.text
        else:
            assert "error" in response.text
        if upstream_status == 429:
            assert "retryAfterSeconds" in response.text
            assert "17" in response.text
            assert "synthetic private failure" not in response.text
        messages = client.get(f"/api/sessions/{session_id}/messages")
        assert messages.status_code == 200
        assistant = [message for message in messages.json() if message["role"] == "assistant"][-1]
        assert assistant["status"] == ("complete" if upstream_status == 200 else "error")
        assert client.get("/api/documents").status_code == 404
        assert client.get("/api/sessions/initializations").status_code == 404
        assert client.get("/api/sessions/deletions").status_code == 404
        assert client.post("/api/sessions", json={"toolsEnabled": True}).status_code == 422
        assert client.post("/api/chat", json={
            "sessionId": session_id,
            "content": "ordinary prompt",
        }).status_code == 422
        assert client.post(f"/api/sessions/{session_id}", json={}).status_code == 404

    assert len(requests) == 1
    assert requests[0].url.host == "gateway.example.invalid"
    assert requests[0].url.path == "/openai/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer synthetic-access-token"
    assert requests[0].headers.get("s7p-key") is None
    assert gateway_manifests == [json.dumps(manifest)]
    assert memory.warmup.await_count == 0
    assert recovery_calls == []
    assert CALLER_CLIENT_ID == settings.api_identity_client_id
    asyncio.run(http.aclose())


@pytest.mark.parametrize(
    ("stream", "failure"),
    [
        (False, 400),
        (True, 400),
        (False, 500),
        (True, 500),
        (False, 302),
        (True, 302),
        (True, "truncated"),
        (True, "read_error"),
    ],
)
def test_pilot_gateway_failures_persist_without_redirect_retry_or_fallback(
    monkeypatch, stream, failure,
):
    settings, catalog, _manifest = _pilot_configuration()
    requests: list[httpx.Request] = []
    token_calls: list[str] = []
    private_detail = "synthetic-private-upstream-detail"
    redirect_host = "redirect.example.invalid"

    class PartialStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            event = {"choices": [{"delta": {"content": "partial"}}]}
            yield f"data: {json.dumps(event)}\n\n".encode()
            if failure == "read_error":
                raise httpx.ReadError(private_detail)

        async def aclose(self):
            self.closed = True

    partial_stream = PartialStream()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) != 1:
            pytest.fail("pilot must not follow redirects, retry, or use the legacy route")
        if failure in {"truncated", "read_error"}:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=partial_stream,
            )
        if failure == 302:
            return httpx.Response(
                302,
                headers={"location": f"https://{redirect_host}/{private_detail}"},
                content=private_detail.encode(),
            )
        return httpx.Response(failure, content=private_detail.encode())

    async def token(scope: str) -> str:
        token_calls.append(scope)
        return await _token(scope)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        trust_env=False,
    )
    import ai4ia_api.main as main

    def gateway_factory(current: Settings, http_client=None):
        assert http_client is not None and http_client._trust_env is False
        return ModelGatewayClient(
            current, http_client=http, shared_gateway_token_provider=token,
        )

    monkeypatch.setattr(main, "ModelGatewayClient", gateway_factory)
    monkeypatch.setattr(main, "load_catalog", lambda *_args: catalog.model_copy(deep=True))
    try:
        app = create_app(settings)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            internal_user_id="synthetic-owner",
            subject="synthetic-subject",
            issuer="https://login.example.invalid/",
            tenant_id="synthetic-tenant",
            provider="entra",
        )
        with TestClient(app) as client:
            created = client.post(
                "/api/sessions", json={"model": "gpt-5.2", "toolsEnabled": False},
            )
            assert created.status_code == 201
            session_id = created.json()["id"]
            response = client.post("/api/chat", json={
                "sessionId": session_id,
                "content": "synthetic prompt",
                "stream": stream,
                "allowTools": False,
                "allowAutomaticMemory": False,
                "params": {"max_tokens": 256},
            })
            assert response.status_code == (200 if stream else 502)
            assert private_detail not in response.text
            assert redirect_host not in response.text
            if stream:
                assert '"error"' in response.text
                assert "data: [DONE]" not in response.text
            messages = client.get(f"/api/sessions/{session_id}/messages")
            assert messages.status_code == 200
            assert private_detail not in messages.text
            assert redirect_host not in messages.text
            assistants = [
                message for message in messages.json() if message["role"] == "assistant"
            ]
            assert len(assistants) == 1
            assert assistants[0]["status"] == "error"
            if failure in {"truncated", "read_error"}:
                assert "partial" in response.text
                assert assistants[0]["content"] == "partial"
                assert partial_stream.closed
    finally:
        asyncio.run(http.aclose())

    assert len(requests) == 1
    assert len(token_calls) == 1
    request = requests[0]
    assert request.url.host == "gateway.example.invalid"
    assert request.url.path == "/openai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer synthetic-access-token"
    assert "s7p-key" not in request.headers
    assert "s7p-iterator" not in request.headers


@pytest.mark.parametrize(
    ("stream", "region", "data_zone", "residency", "expected_status", "expected_deployment"),
    [
        (False, None, None, "eu", 200, "synthetic-allowed"),
        (True, None, None, "eu", 200, "synthetic-allowed"),
        (False, "synthetic-east", None, "eu", 200, "synthetic-allowed"),
        (True, "synthetic-east", None, "eu", 200, "synthetic-allowed"),
        (False, None, "eu", "eu", 200, "synthetic-allowed"),
        (False, "synthetic-west", None, "eu", 400, None),
        (True, "synthetic-west", None, "eu", 400, None),
        (False, None, "us", "eu", 400, None),
        (False, None, None, "us", 400, None),
    ],
)
def test_pilot_chat_dispatch_uses_only_manifest_advertised_catalog_options(
    monkeypatch, stream, region, data_zone, residency, expected_status,
    expected_deployment,
):
    settings, catalog, manifest = _pilot_configuration()
    entry = catalog.get("gpt-5.2")
    assert entry is not None
    entry.options = [
        DeploymentOption(
            region="synthetic-west",
            dataZone="eu",
            sku="Standard",
            deploymentName="synthetic-unlisted",
        ),
        DeploymentOption(
            region="synthetic-east",
            dataZone="eu",
            sku="Standard",
            deploymentName="synthetic-allowed",
        ),
    ]
    catalog.residencyPolicy = residency
    manifest["operations"] = [
        {"method": "POST", "path": "/chat/completions", "stream": "allowed"},
    ]
    manifest["models"] = {
        "gpt-5.2": {
            "kind": "alias",
            "operations": ["/chat/completions"],
            "targets": [{
                "deployment": "synthetic-allowed",
                "sku": "Standard",
            }],
        },
    }
    settings = settings.model_copy(update={
        "data_residency": residency,
        "shared_gateway_activation_manifest": json.dumps(manifest),
    })

    requests: list[httpx.Request] = []
    token_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get("stream", False):
            events = [
                {"choices": [{"delta": {"content": "synthetic answer"}}]},
                {"choices": [{"finish_reason": "stop"}]},
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(f"data: {json.dumps(event)}\n\n" for event in events)
                + "data: [DONE]\n\n",
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "synthetic answer"}}]},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import ai4ia_api.main as main

    real_gateway = ModelGatewayClient

    async def token(scope: str) -> str:
        token_calls.append(scope)
        return await _token(scope)

    def gateway_factory(current: Settings, http_client=None):
        assert http_client is not None and http_client._trust_env is False
        return real_gateway(
            current,
            http_client=http,
            shared_gateway_token_provider=token,
        )

    monkeypatch.setattr(
        main, "load_catalog", lambda *_args: catalog.model_copy(deep=True)
    )
    monkeypatch.setattr(main, "ModelGatewayClient", gateway_factory)
    app = create_app(settings)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        internal_user_id="synthetic-owner",
        subject="synthetic-subject",
        issuer="https://login.example.invalid/",
        tenant_id="synthetic-tenant",
        provider="entra",
    )

    try:
        with TestClient(app) as client:
            listed = client.get("/api/models")
            assert listed.status_code == 200
            listed_entry = next(
                (
                    model for model in listed.json()["models"]
                        if model["id"] == "gpt-5.2"
                ),
                None,
            )
            if residency == "us":
                assert listed_entry is None
            else:
                assert listed_entry is not None
                assert [
                    option["deploymentName"] for option in listed_entry["options"]
                ] == ["synthetic-allowed"]

            created = client.post(
                "/api/sessions",
                json={"model": "gpt-5.2", "toolsEnabled": False},
            )
            assert created.status_code == 201
            body = {
                "sessionId": created.json()["id"],
                "content": "synthetic prompt",
                "stream": stream,
                "allowTools": False,
                "allowAutomaticMemory": False,
                "params": {"max_tokens": 256},
            }
            if region is not None:
                body["region"] = region
            if data_zone is not None:
                body["dataZone"] = data_zone
            response = client.post("/api/chat", json=body)
            assert response.status_code == expected_status
            if expected_deployment is not None:
                assert "synthetic answer" in response.text
    finally:
        asyncio.run(http.aclose())

    expected_sends = int(expected_deployment is not None)
    assert len(requests) == expected_sends
    assert len(token_calls) == expected_sends
    if requests:
        request = requests[0]
        assert request.url.host == "gateway.example.invalid"
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer synthetic-access-token"
        assert request.headers.get("s7p-key") is None
        assert json.loads(request.content)["model"] == "gpt-5.2"
