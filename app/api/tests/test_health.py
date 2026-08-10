import asyncio

import pytest

from ai4ia_api.routers.health import SessionStoreReadiness
from tests.conftest import make_settings
from ai4ia_api.main import create_app


def test_health_live(client):
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_ready_checks_the_session_store(client):
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "stage": "session_store"}


def test_health_ready_fails_closed_without_leaking_dependency_detail(client):
    class Unavailable:
        async def check(self):
            return False

    client.app.state.session_readiness = Unavailable()
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "stage": "session_store"}


@pytest.mark.anyio
async def test_readiness_is_cached_but_rechecks_after_ttl():
    now = [0.0]

    class Repo:
        def __init__(self):
            self.calls = 0

        async def check_ready(self):
            self.calls += 1

    repo = Repo()
    probe = SessionStoreReadiness(
        repo,
        success_ttl_seconds=10,
        failure_ttl_seconds=1,
        monotonic=lambda: now[0],
    )
    assert await probe.check() is True
    assert await probe.check() is True
    assert repo.calls == 1
    now[0] = 11
    assert await probe.check() is True
    assert repo.calls == 2


@pytest.mark.anyio
async def test_readiness_timeout_is_bounded_and_failure_cache_is_short():
    now = [0.0]

    class Repo:
        def __init__(self):
            self.calls = 0

        async def check_ready(self):
            self.calls += 1
            await asyncio.sleep(1)

    repo = Repo()
    probe = SessionStoreReadiness(
        repo,
        timeout_seconds=0.001,
        success_ttl_seconds=10,
        failure_ttl_seconds=1,
        monotonic=lambda: now[0],
    )
    assert await probe.check() is False
    assert await probe.check() is False
    assert repo.calls == 1
    now[0] = 2
    assert await probe.check() is False
    assert repo.calls == 2


def test_openapi_is_environment_aware_and_explicitly_overridable():
    local = create_app(make_settings())
    assert local.openapi_url == "/openapi.json"
    assert local.docs_url == "/docs"

    production = make_settings(
        env="prod",
        auth_provider="entra",
        allow_dev_auth=False,
        entra_tenant_id="tenant",
        entra_audience="audience",
        model_gateway_auth_mode="api_key",
        model_gateway_api_key="key",
    )
    hidden = create_app(production)
    assert hidden.openapi_url is None
    assert hidden.docs_url is None

    explicit = create_app(production.model_copy(update={"openapi_enabled": True}))
    assert explicit.openapi_url == "/openapi.json"
    assert explicit.docs_url == "/docs"
