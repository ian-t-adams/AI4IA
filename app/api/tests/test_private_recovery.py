"""Exercise the actual factory, staging middleware and runtime readiness seam."""
import asyncio
import socket
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ai4ia_api import main
from ai4ia_api.routers.health import SessionStoreReadiness
from tests.conftest import make_settings


@pytest.mark.parametrize("candidate", [False, True])
def test_factory_staging_denies_work_but_keeps_authenticated_options(monkeypatch, candidate):
    ingestor = type("Ingestor", (), {
        "recover_interrupted": AsyncMock(), "close": AsyncMock(),
    })()
    worker = type("Worker", (), {
        "__init__": lambda self, **kwargs: None,
        "start": AsyncMock(), "stop": AsyncMock(),
    })
    from ai4ia_api.workflows import durable

    monkeypatch.setattr(main, "build_document_ingestor", lambda *args, **kwargs: ingestor)
    monkeypatch.setattr(main, "build_document_retrieval", lambda *args, **kwargs: None)
    monkeypatch.setattr(durable, "DurableWorkflowService", worker)
    settings = make_settings(
        private_recovery_candidate=candidate, durable_workflows_enabled=True,
        durable_task_endpoint="https://scheduler.test", durable_task_hub_name="test",
    )
    app = main.create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        from ai4ia_api.auth.base import AuthError

        original = app.state.auth_provider

        class Reject:
            async def authenticate(self, credentials):
                raise AuthError("Authentication required.")

        app.state.auth_provider = Reject()
        assert client.get("/api/decisions/options").status_code == 401
        app.state.auth_provider = original
        options = client.get("/api/decisions/options", headers={"X-Dev-User": "synthetic"})
        assert options.status_code == 200
        assert "models" in options.json()
        assert ingestor.recover_interrupted.await_count == (0 if candidate else 1)
        assert worker.start.await_count == (0 if candidate else 1)
        # The real directory service stays enabled normally but cannot write on
        # the sole authenticated staging read.
        assert app.state.user_directory.enabled is (not candidate)
        ordinary = client.get("/api/models", headers={"X-Dev-User": "synthetic"})
        assert ordinary.status_code == (503 if candidate else 200)
        denied = client.post("/api/decisions/run", json={}, headers={"X-Dev-User": "synthetic"})
        assert denied.status_code == (503 if candidate else 422)
        if candidate:
            assert denied.json()["code"] == "private_recovery_candidate"
            assert denied.headers["cache-control"] == "no-store"
    assert ingestor.close.await_count == 1
    assert worker.stop.await_count == (0 if candidate else 1)


def test_candidate_websocket_never_reaches_relay():
    app = main.create_app(make_settings(private_recovery_candidate=True))
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/api/voice/live"):
                pytest.fail("Candidate accepted a voice connection")
        assert closed.value.code == 1008


@pytest.mark.anyio
@pytest.mark.parametrize("addresses,expected", [
    (["10.241.0.68"], True),
    (["10.241.0.68", "20.1.1.1"], False),
    (["20.1.1.1"], False), (["127.0.0.1"], False),
    (["169.254.169.254"], False), (["::1"], False), ([], False),
])
async def test_private_dns_precedes_canonical_store_read(monkeypatch, addresses, expected):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    repo = type("Repo", (), {"check_ready": AsyncMock()})()
    probe = SessionStoreReadiness(
        repo, private_cosmos_endpoint="https://synthetic.documents.azure.com:443/",
    )
    assert await probe.check() is expected
    assert repo.check_ready.await_count == int(expected)
    resolver.assert_awaited_once_with("synthetic.documents.azure.com", 443, type=socket.SOCK_STREAM)
    # The unchanged legacy mode does not acquire a new network prerequisite.
    assert await SessionStoreReadiness(repo).check() is True
    assert repo.check_ready.await_count == int(expected) + 1


@pytest.mark.anyio
async def test_private_dns_timeout_and_store_failure_remain_unready(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", slow)
    repo = type("Repo", (), {"check_ready": AsyncMock()})()
    probe = SessionStoreReadiness(
        repo, private_cosmos_endpoint="https://synthetic.documents.azure.com/",
        timeout_seconds=0.001,
    )
    assert await probe.check() is False
    repo.check_ready.assert_not_awaited()
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.241.0.68", 443)),
    ]))
    repo.check_ready.side_effect = RuntimeError("synthetic private Cosmos failure")
    assert await SessionStoreReadiness(
        repo, private_cosmos_endpoint="https://synthetic.documents.azure.com/",
    ).check() is False
    repo.check_ready.assert_awaited_once()
