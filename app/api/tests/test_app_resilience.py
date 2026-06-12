"""The global Azure-error handler degrades a managed-store outage to 503, not 500.

Regression for the live incident where Cosmos public-network access drifted to
'Disabled' (a tenant policy remediation side-effect): every Cosmos-backed endpoint
raised an unhandled azure-core error -> raw 500, which (pre PR #9) blanked the whole
web app. The handler now maps connectivity/auth/firewall(401/403)/throttling/5xx to
503 so the client can treat it as a transient, scoped failure while the catalog +
chat stay usable.
"""
from __future__ import annotations

import contextlib

from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
)
from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from tests.conftest import make_settings


class _BoomRepo:
    """Session repo whose reads fail like an unreachable Cosmos account."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def list_sessions(self, user_id: str):  # noqa: ARG002
        raise self._exc


@contextlib.contextmanager
def _client_with_repo(exc: Exception):
    # The override must be applied AFTER the lifespan startup (which builds the real
    # repo), so it is set inside the TestClient context — mirroring how conftest's
    # `client` fixture attaches the fake gateway.
    app = create_app(make_settings())
    with TestClient(app) as client:
        client.app.state.session_repo = _BoomRepo(exc)
        yield client



def test_cosmos_connectivity_failure_maps_to_503():
    # ServiceRequestError is what the SDK raises when the network path is severed
    # (exactly the public-network-disabled failure mode).
    with _client_with_repo(ServiceRequestError("connection refused")) as client:
        resp = client.get("/api/sessions")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Service temporarily unavailable"


def test_managed_identity_token_failure_maps_to_503():
    with _client_with_repo(ClientAuthenticationError("no token")) as client:
        resp = client.get("/api/sessions")
    assert resp.status_code == 503


def test_throttling_429_maps_to_503():
    exc = HttpResponseError("throttled")
    exc.status_code = 429
    with _client_with_repo(exc) as client:
        resp = client.get("/api/sessions")
    assert resp.status_code == 503


def test_cosmos_firewall_403_maps_to_503():
    # The documented incident: a tenant policy remediation flips Cosmos
    # publicNetworkAccess to 'Disabled', so every data-plane call returns
    # 403 Forbidden ("blocked by your Cosmos DB account firewall settings").
    # That is an operational/network problem, not a malformed-request bug, so
    # it must degrade to 503 rather than masquerade as an opaque 500.
    # (CosmosHttpResponseError is an HttpResponseError subclass; the handler
    # keys on the base type + status_code, so HttpResponseError stands in.)
    exc = HttpResponseError("blocked by firewall")
    exc.status_code = 403
    with _client_with_repo(exc) as client:
        resp = client.get("/api/sessions")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Service temporarily unavailable"


def test_data_plane_401_maps_to_503():
    # A data-plane 401 (token/identity not yet propagated) is operational, not a
    # request-shape bug, so it is transient too.
    exc = HttpResponseError("unauthorized")
    exc.status_code = 401
    with _client_with_repo(exc) as client:
        resp = client.get("/api/sessions")
    assert resp.status_code == 503


def test_unexpected_4xx_preserves_500_semantics():
    # A deterministic client/code error (e.g. a malformed query -> 400) is NOT a
    # transient availability problem, so it must not masquerade as "try again later".
    exc = HttpResponseError("bad request")
    exc.status_code = 400
    with _client_with_repo(exc) as client:
        resp = client.get("/api/sessions")
    assert resp.status_code == 500
