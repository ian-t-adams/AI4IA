"""Admin usage/metrics HTTP surface: gating + correctness + graceful degrade.

The security contract: EVERY ``/api/admin/usage/*`` and ``/api/admin/metrics/*``
route is behind ``require_admin`` (admin 200 / non-admin 403 / anon 401). The one
exception is ``/api/admin/whoami`` (auth-only) which returns an ``isAdmin`` boolean
so the web client can *hide* a nav entry — it never gates anything.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthError
from ai4ia_api.main import create_app
from ai4ia_api.usage.models import UsageRecord
from tests.conftest import FakeGateway, make_settings

ADMIN = {"X-Dev-User": "alice"}
NON_ADMIN = {"X-Dev-User": "carol"}

# Every admin-gated GET route (whoami is intentionally excluded — auth-only).
GATED_ROUTES = [
    "/api/admin/usage/summary",
    "/api/admin/usage/by-model",
    "/api/admin/usage/by-day",
    "/api/admin/usage/agents",
    "/api/admin/usage/user-agents",
    "/api/admin/usage/distributions",
    "/api/admin/usage/by-user",
    "/api/admin/metrics/resources",
]


def _client(**settings_overrides) -> TestClient:
    app = create_app(make_settings(admin_subjects="alice", **settings_overrides))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = FakeGateway()
    return c


@pytest.fixture
def client():
    c = _client()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _seed(client, records: list[UsageRecord]) -> None:
    """Seed the shared in-memory ledger the admin service reads from."""
    repo = client.app.state.admin_usage._repo
    for r in records:
        repo._by_user.setdefault(r.userId, []).append(r)


def _rec(user: str, **kw) -> UsageRecord:
    base = dict(
        userId=user,
        sessionId="s1",
        model="gpt-5.2",
        deployment="dep",
        status="complete",
        billable=True,
        usageKnown=True,
        promptTokens=10,
        completionTokens=5,
        totalTokens=15,
        costKnown=True,
        estCostMicroUsd=1000,
        createdAt=datetime.now(timezone.utc),
    )
    base.update(kw)
    return UsageRecord(**base)


class _RaisingAuth:
    """Auth provider that always rejects — simulates an anonymous request."""

    async def authenticate(self, credentials):
        raise AuthError("missing credentials")


# ---- gating: admin 200 / non-admin 403 / anon 401 on EVERY route ----


@pytest.mark.parametrize("route", GATED_ROUTES)
def test_admin_route_allows_admin(client, route):
    assert client.get(route, headers=ADMIN).status_code == 200


@pytest.mark.parametrize("route", GATED_ROUTES)
def test_admin_route_forbids_non_admin(client, route):
    assert client.get(route, headers=NON_ADMIN).status_code == 403


@pytest.mark.parametrize("route", GATED_ROUTES)
def test_admin_route_rejects_anon(client, route):
    client.app.state.auth_provider = _RaisingAuth()
    assert client.get(route).status_code == 401


# ---- whoami (auth-only; powers cosmetic UI hide) ----


def test_whoami_true_for_admin(client):
    body = client.get("/api/admin/whoami", headers=ADMIN).json()
    assert body["isAdmin"] is True
    assert body["subject"]


def test_whoami_false_for_non_admin(client):
    body = client.get("/api/admin/whoami", headers=NON_ADMIN).json()
    assert body["isAdmin"] is False


def test_whoami_rejects_anon(client):
    client.app.state.auth_provider = _RaisingAuth()
    assert client.get("/api/admin/whoami").status_code == 401


# ---- aggregation correctness over a seeded store ----


def test_summary_reflects_seeded_ledger(client):
    _seed(
        client,
        [
            _rec("alice", totalTokens=15, estCostMicroUsd=1000, agent="research"),
            _rec("bob", totalTokens=30, estCostMicroUsd=2000, agent="research"),
            _rec("bob", model="o3", totalTokens=10, estCostMicroUsd=500),
        ],
    )
    body = client.get("/api/admin/usage/summary", headers=ADMIN).json()
    assert body["activeUsers"] == 2
    assert body["totalRequests"] == 3
    assert body["totalTokens"] == 55
    assert body["totalCostMicroUsd"] == 3500
    assert body["distinctModels"] == 2
    assert body["distinctAgents"] == 1


def test_by_model_and_by_user_and_agents(client):
    _seed(
        client,
        [
            _rec("alice", model="big", totalTokens=100, agent="research"),
            _rec("bob", model="small", totalTokens=5),
        ],
    )
    by_model = client.get("/api/admin/usage/by-model", headers=ADMIN).json()
    assert by_model["byModel"][0]["model"] == "big"

    by_user = client.get("/api/admin/usage/by-user", headers=ADMIN).json()
    assert by_user["totalUsers"] == 2
    assert by_user["byUser"][0]["userId"] == "alice"
    # No entitlement override seeded -> the unlimited default (entitlement None).
    assert by_user["byUser"][0]["entitlement"] is None

    agents = client.get("/api/admin/usage/agents", headers=ADMIN).json()
    assert agents["agents"][0]["agent"] == "research"


def test_agents_surface_error_and_cancelled_counts(client):
    _seed(
        client,
        [
            _rec("alice", agent="research", status="complete", totalTokens=10),
            _rec("bob", agent="research", status="error", billable=False, totalTokens=0),
            _rec("bob", agent="research", status="cancelled", billable=False, totalTokens=0),
        ],
    )
    agents = client.get("/api/admin/usage/agents", headers=ADMIN).json()
    research = next(a for a in agents["agents"] if a["agent"] == "research")
    assert research["requests"] == 3
    assert research["erroredRequests"] == 1
    assert research["cancelledRequests"] == 1


def test_user_agents_cross_tab(client):
    _seed(
        client,
        [
            _rec("alice", agent="research", totalTokens=10),
            _rec("alice", agent="research", status="error", billable=False, totalTokens=0),
            _rec("bob", agent="coder", totalTokens=100),
            _rec("bob", totalTokens=999),  # plain turn -> excluded
        ],
    )
    body = client.get("/api/admin/usage/user-agents", headers=ADMIN).json()
    cells = {(c["userId"], c["agent"]): c for c in body["userAgents"]}
    assert set(cells) == {("alice", "research"), ("bob", "coder")}
    assert cells[("alice", "research")]["requests"] == 2
    assert cells[("alice", "research")]["erroredRequests"] == 1
    # Heaviest cell first (bob/coder, 100 tokens).
    assert (body["userAgents"][0]["userId"], body["userAgents"][0]["agent"]) == ("bob", "coder")


def test_distributions_rollups(client):
    _seed(
        client,
        [
            _rec("alice", region="eastus", dataZone="us", deployment="d1", status="complete", totalTokens=10),
            _rec("bob", region="eastus", dataZone="us", deployment="d2", status="error", billable=False, totalTokens=0),
            _rec("carol", region="westus", dataZone="eu", deployment="d1", status="cancelled", billable=False, totalTokens=0),
        ],
    )
    body = client.get("/api/admin/usage/distributions", headers=ADMIN).json()
    assert {b["key"]: b["requests"] for b in body["byRegion"]} == {"eastus": 2, "westus": 1}
    assert {b["key"]: b["requests"] for b in body["byDataZone"]} == {"us": 2, "eu": 1}
    assert {b["key"]: b["requests"] for b in body["byDeployment"]} == {"d1": 2, "d2": 1}
    assert {b["key"]: b["requests"] for b in body["byStatus"]} == {
        "complete": 1,
        "error": 1,
        "cancelled": 1,
    }
    region_eastus = next(b for b in body["byRegion"] if b["key"] == "eastus")
    assert region_eastus["erroredRequests"] == 1


def test_distributions_honours_window(client):
    body = client.get("/api/admin/usage/distributions?days=7", headers=ADMIN).json()
    assert body["sinceDays"] == 7
    # Window is clamped to MAX_ADMIN_DAYS.
    over = client.get("/api/admin/usage/distributions?days=9999", headers=ADMIN)
    assert over.status_code == 422


def test_by_user_joins_entitlement_override(client):
    # The ledger keys on the internal user id (not the raw subject), so resolve it
    # first and seed under that id — matching how record_completion writes.
    uid = client.get("/api/entitlement", headers={"X-Dev-User": "target"}).json()["userId"]
    _seed(client, [_rec(uid, totalTokens=15)])
    put = client.put(
        f"/api/admin/entitlements/{uid}",
        json={"tokensPerDay": 5000},
        headers=ADMIN,
    )
    assert put.status_code == 200, put.text
    body = client.get("/api/admin/usage/by-user", headers=ADMIN).json()
    row = next(r for r in body["byUser"] if r["userId"] == uid)
    assert row["entitlement"] is not None
    assert row["entitlement"]["tokensPerDay"] == 5000


# ---- resource metrics degrade gracefully when no resource ids configured ----


def test_resource_metrics_unavailable_without_ids(client):
    body = client.get("/api/admin/metrics/resources", headers=ADMIN).json()
    assert {p["key"] for p in body["panels"]} == {
        "search",
        "postgres",
        "cosmos",
        "containerApp",
    }
    assert all(p["status"] == "unavailable" for p in body["panels"])
    assert all(p["detail"] for p in body["panels"])


def test_resource_metrics_disabled_returns_unavailable():
    c = _client(resource_metrics_enabled=False)
    try:
        body = c.get("/api/admin/metrics/resources", headers=ADMIN).json()
        assert all(p["status"] == "unavailable" for p in body["panels"])
        assert all("disabled" in p["detail"].lower() for p in body["panels"])
    finally:
        c.__exit__(None, None, None)


# ---- gating under spoofable dev auth in a deployed env ----


def test_deployed_dev_admin_requires_secret():
    c = _client(env="dev", admin_api_secret="s3cret")
    try:
        # Allowlist alone is not enough — identity is spoofable in a deployed env.
        assert c.get("/api/admin/usage/summary", headers=ADMIN).status_code == 403
        ok = c.get(
            "/api/admin/usage/summary",
            headers={**ADMIN, "X-Admin-Secret": "s3cret"},
        )
        assert ok.status_code == 200
        # whoami reflects the same gate.
        who = c.get(
            "/api/admin/whoami",
            headers={**ADMIN, "X-Admin-Secret": "s3cret"},
        ).json()
        assert who["isAdmin"] is True
        assert c.get("/api/admin/whoami", headers=ADMIN).json()["isAdmin"] is False
    finally:
        c.__exit__(None, None, None)
