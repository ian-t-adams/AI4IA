"""HTTP tests for user-directory enrichment of the admin usage endpoints.

The admin ``by-user`` and ``user-agents`` panels can resolve the hashed ``userId``
to a display name + email via the admin-only directory when ``identify=true`` is
requested, attaching optional ``displayName``/``email`` and degrading to ``null``
(the UI falls back to the short hash) when de-identified or unknown. Enrichment is
read-only and must not change the aggregation, and the existing ``require_admin``
gating is unchanged (covered exhaustively in test_admin_usage_api.py).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.directory.model import UserDirectoryEntry
from ai4ia_api.main import create_app
from ai4ia_api.usage.models import UsageRecord
from tests.conftest import FakeGateway, make_settings

ADMIN = {"X-Dev-User": "alice"}
NON_ADMIN = {"X-Dev-User": "carol"}


def _client(**overrides) -> TestClient:
    app = create_app(make_settings(admin_subjects="alice", **overrides))
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


def _seed_usage(client: TestClient, records: list[UsageRecord]) -> None:
    repo = client.app.state.admin_usage._repo
    for r in records:
        repo._by_user.setdefault(r.userId, []).append(r)


def _seed_directory(client: TestClient, uid: str, name: str | None, email: str | None) -> None:
    """Seed a directory entry directly (deterministic — no reliance on capture)."""
    repo = client.app.state.user_directory._repo
    repo._by_user[uid] = UserDirectoryEntry.build(uid, name, email)


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


# ---- by-user enrichment ----


def test_by_user_defaults_to_deidentified_even_when_directory_known(client):
    _seed_usage(client, [_rec("bob", totalTokens=100)])
    _seed_directory(client, "bob", "Bob Builder", "bob@build.test")
    body = client.get("/api/admin/usage/by-user", headers=ADMIN).json()
    row = next(r for r in body["byUser"] if r["userId"] == "bob")
    assert row["displayName"] is None
    assert row["email"] is None


def test_by_user_attaches_name_and_email_when_identified(client):
    _seed_usage(client, [_rec("bob", totalTokens=100), _rec("carol", totalTokens=5)])
    _seed_directory(client, "bob", "Bob Builder", "bob@build.test")
    body = client.get("/api/admin/usage/by-user?identify=true", headers=ADMIN).json()
    rows = {r["userId"]: r for r in body["byUser"]}
    assert rows["bob"]["displayName"] == "Bob Builder"
    assert rows["bob"]["email"] == "bob@build.test"
    # Unknown user degrades to null -> UI shows the short hash.
    assert rows["carol"]["displayName"] is None
    assert rows["carol"]["email"] is None


def test_by_user_does_not_change_aggregation(client):
    _seed_usage(client, [_rec("bob", totalTokens=100), _rec("carol", totalTokens=5)])
    _seed_directory(client, "bob", "Bob Builder", "bob@build.test")
    body = client.get("/api/admin/usage/by-user", headers=ADMIN).json()
    assert body["totalUsers"] == 2
    rows = {r["userId"]: r for r in body["byUser"]}
    assert rows["bob"]["totalTokens"] == 100
    assert rows["carol"]["totalTokens"] == 5


def test_by_user_degrades_when_directory_disabled():
    c = _client(user_directory_enabled=False)
    try:
        _seed_usage(c, [_rec("bob", totalTokens=100)])
        _seed_directory(c, "bob", "Bob Builder", "bob@build.test")
        body = c.get("/api/admin/usage/by-user?identify=true", headers=ADMIN).json()
        row = next(r for r in body["byUser"] if r["userId"] == "bob")
        # Disabled -> resolve returns {} -> name stays null even though seeded.
        assert row["displayName"] is None
        assert row["email"] is None
    finally:
        c.__exit__(None, None, None)


# ---- user-agents enrichment ----


def test_user_agents_defaults_to_deidentified_even_when_directory_known(client):
    _seed_usage(client, [_rec("bob", agent="coder", totalTokens=100)])
    _seed_directory(client, "bob", "Bob Builder", "bob@build.test")
    body = client.get("/api/admin/usage/user-agents", headers=ADMIN).json()
    cell = next(c for c in body["userAgents"] if (c["userId"], c["agent"]) == ("bob", "coder"))
    assert cell["displayName"] is None
    assert cell["email"] is None


def test_user_agents_attaches_name_and_email_when_identified(client):
    _seed_usage(
        client,
        [
            _rec("bob", agent="coder", totalTokens=100),
            _rec("carol", agent="research", totalTokens=10),
        ],
    )
    _seed_directory(client, "bob", "Bob Builder", "bob@build.test")
    body = client.get("/api/admin/usage/user-agents?identify=true", headers=ADMIN).json()
    cells = {(c["userId"], c["agent"]): c for c in body["userAgents"]}
    assert cells[("bob", "coder")]["displayName"] == "Bob Builder"
    assert cells[("bob", "coder")]["email"] == "bob@build.test"
    # Unknown user degrades to null.
    assert cells[("carol", "research")]["displayName"] is None
    assert cells[("carol", "research")]["email"] is None


def test_user_agents_preserves_cross_tab_math(client):
    _seed_usage(
        client,
        [
            _rec("bob", agent="coder", totalTokens=100),
            _rec("bob", agent="coder", status="error", billable=False, totalTokens=0),
        ],
    )
    body = client.get("/api/admin/usage/user-agents", headers=ADMIN).json()
    cell = next(c for c in body["userAgents"] if (c["userId"], c["agent"]) == ("bob", "coder"))
    assert cell["requests"] == 2
    assert cell["erroredRequests"] == 1


# ---- gating unchanged for the enriched routes ----


def test_enriched_routes_still_forbid_non_admin(client):
    for route in (
        "/api/admin/usage/by-user",
        "/api/admin/usage/by-user?identify=true",
        "/api/admin/usage/user-agents",
        "/api/admin/usage/user-agents?identify=true",
    ):
        assert client.get(route, headers=NON_ADMIN).status_code == 403


def test_capture_populates_directory_for_signed_in_user(client):
    # A dev request carries name/email, so simply calling an authenticated endpoint
    # captures the caller into the directory going forward. Capture is fire-and-
    # forget on the server's loop thread, so poll the (in-memory) directory until
    # the write lands before asserting the enriched admin read.
    uid = client.get("/api/entitlement", headers={"X-Dev-User": "dave"}).json()["userId"]
    repo = client.app.state.user_directory._repo
    for _ in range(50):
        if uid in repo._by_user:
            break
        time.sleep(0.02)
    assert uid in repo._by_user, "capture did not populate the directory"

    _seed_usage(client, [_rec(uid, totalTokens=42)])
    body = client.get("/api/admin/usage/by-user", headers=ADMIN).json()
    row = next(r for r in body["byUser"] if r["userId"] == uid)
    assert row["displayName"] is None
    assert row["email"] is None

    identified = client.get("/api/admin/usage/by-user?identify=true", headers=ADMIN).json()
    row = next(r for r in identified["byUser"] if r["userId"] == uid)
    assert row["displayName"] == "dave"
    assert row["email"] == "dave@example.com"
