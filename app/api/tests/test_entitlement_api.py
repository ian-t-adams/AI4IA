"""Entitlement HTTP surface + end-to-end chat enforcement.

Asserts the product contract: every user is unlimited by default (self endpoint
shows ``isUnlimited``), the admin management API is properly gated (and not
spoofable under dev auth in a deployed env), and an admin-set limit actually
turns into a 429/403 on POST /api/chat — then lifts cleanly on DELETE.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.main import create_app
from tests.conftest import FakeGateway, make_settings

ADMIN = {"X-Dev-User": "alice"}


def _client(**settings_overrides) -> TestClient:
    app = create_app(make_settings(**settings_overrides))
    c = TestClient(app)
    c.__enter__()
    c.app.state.gateway = FakeGateway()
    return c


@pytest.fixture
def client():
    """Local env, alice is an admin via the subject allowlist."""
    c = _client(admin_subjects="alice")
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _new_session(client, headers=None) -> str:
    resp = client.post(
        "/api/sessions", json={"title": "Chat", "model": "gpt-5.2"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _internal_id(client, headers) -> str:
    return client.get("/api/entitlement", headers=headers).json()["userId"]


# ---- self endpoint ----


def test_self_entitlement_is_unlimited_by_default(client):
    body = client.get("/api/entitlement", headers={"X-Dev-User": "bob"}).json()
    assert body["isUnlimited"] is True
    assert body["source"] == "default"
    assert body["disabled"] is False


# ---- admin gating ----


def test_admin_endpoints_reject_non_admin(client):
    # carol is not in the allowlist
    r = client.get("/api/admin/entitlements", headers={"X-Dev-User": "carol"})
    assert r.status_code == 403


def test_admin_allowlisted_user_can_list(client):
    r = client.get("/api/admin/entitlements", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == []


def test_admin_put_get_delete_roundtrip(client):
    uid = _internal_id(client, {"X-Dev-User": "target"})
    put = client.put(
        f"/api/admin/entitlements/{uid}",
        json={"tokensPerDay": 5000, "note": "trial"},
        headers=ADMIN,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["source"] == "override"
    assert body["tokensPerDay"] == 5000
    assert body["isUnlimited"] is False

    got = client.get(f"/api/admin/entitlements/{uid}", headers=ADMIN).json()
    assert got["tokensPerDay"] == 5000
    assert got["updatedBy"] == "alice"

    listed = client.get("/api/admin/entitlements", headers=ADMIN).json()
    assert any(e["userId"] == uid for e in listed)

    dele = client.delete(f"/api/admin/entitlements/{uid}", headers=ADMIN)
    assert dele.status_code == 204
    after = client.get(f"/api/admin/entitlements/{uid}", headers=ADMIN).json()
    assert after["isUnlimited"] is True
    assert after["source"] == "default"


def test_put_rejects_negative_limit(client):
    uid = _internal_id(client, {"X-Dev-User": "target"})
    r = client.put(
        f"/api/admin/entitlements/{uid}",
        json={"requestsPerMinute": -1},
        headers=ADMIN,
    )
    assert r.status_code == 422


def test_admin_can_set_and_read_the_sandbox_execution_cap(client):
    """The Code Interpreter allowance has to be settable and visible through the
    same admin surface as every other limit, or an operator cannot use it. A
    sandbox is billed per session, so this is its own axis rather than a token
    or cost figure (audit P1-2)."""
    uid = _internal_id(client, {"X-Dev-User": "target"})
    put = client.put(
        f"/api/admin/entitlements/{uid}",
        json={"computeExecutionsPerDay": 25},
        headers=ADMIN,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["computeExecutionsPerDay"] == 25
    # Setting only this limit must take the user OFF the unlimited fast path,
    # otherwise the cap could never be reached.
    assert body["isUnlimited"] is False
    # ...and the user's own read shows it too.
    mine = client.get("/api/entitlement", headers={"X-Dev-User": "target"}).json()
    assert mine["computeExecutionsPerDay"] == 25
    assert mine["isUnlimited"] is False


def test_put_rejects_a_negative_sandbox_cap(client):
    uid = _internal_id(client, {"X-Dev-User": "target"})
    r = client.put(
        f"/api/admin/entitlements/{uid}",
        json={"computeExecutionsPerDay": -1},
        headers=ADMIN,
    )
    assert r.status_code == 422


# ---- admin gating under spoofable dev auth (deployed env) ----


def test_deployed_dev_admin_requires_secret():
    c = _client(env="dev", admin_subjects="alice", admin_api_secret="s3cret")
    try:
        # Allowlist alone is not enough — identity is spoofable here.
        assert c.get("/api/admin/entitlements", headers=ADMIN).status_code == 403
        # Correct secret authorizes.
        ok = c.get(
            "/api/admin/entitlements",
            headers={**ADMIN, "X-Admin-Secret": "s3cret"},
        )
        assert ok.status_code == 200
        # Wrong secret is rejected.
        bad = c.get(
            "/api/admin/entitlements",
            headers={**ADMIN, "X-Admin-Secret": "nope"},
        )
        assert bad.status_code == 403
    finally:
        c.__exit__(None, None, None)


def test_deployed_dev_admin_fails_closed_without_secret_configured():
    c = _client(env="dev", admin_subjects="alice")  # no admin_api_secret
    try:
        assert c.get("/api/admin/entitlements", headers=ADMIN).status_code == 403
    finally:
        c.__exit__(None, None, None)


def test_local_admin_secret_is_second_factor():
    """Under local/entra auth, a configured secret is required ON TOP of the
    identity allowlist — identity alone is not enough once a secret exists."""
    c = _client(admin_subjects="alice", admin_api_secret="s3cret")
    try:
        # Allowlisted identity but no secret -> rejected.
        assert c.get("/api/admin/entitlements", headers=ADMIN).status_code == 403
        # Identity + correct secret -> allowed.
        ok = c.get(
            "/api/admin/entitlements",
            headers={**ADMIN, "X-Admin-Secret": "s3cret"},
        )
        assert ok.status_code == 200
        # Correct secret but non-allowlisted identity -> still rejected (local
        # auth is trustworthy, so identity must also match).
        no_id = c.get(
            "/api/admin/entitlements",
            headers={"X-Dev-User": "carol", "X-Admin-Secret": "s3cret"},
        )
        assert no_id.status_code == 403
    finally:
        c.__exit__(None, None, None)


# ---- end-to-end chat enforcement ----


def test_chat_blocked_by_hard_rate_limit_then_unblocked(client):
    headers = {"X-Dev-User": "limited"}
    uid = _internal_id(client, headers)
    sid = _new_session(client, headers=headers)

    # Hard block: requestsPerMinute=0 denies regardless of ledger contents.
    client.put(
        f"/api/admin/entitlements/{uid}",
        json={"requestsPerMinute": 0},
        headers=ADMIN,
    )
    blocked = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "hi", "stream": False},
        headers=headers,
    )
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After") == "60"

    # Lifting the override restores access.
    client.delete(f"/api/admin/entitlements/{uid}", headers=ADMIN)
    ok = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "hi", "stream": False},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text


def test_disabled_user_is_forbidden_from_chat(client):
    headers = {"X-Dev-User": "banned"}
    uid = _internal_id(client, headers)
    sid = _new_session(client, headers=headers)
    client.put(
        f"/api/admin/entitlements/{uid}",
        json={"disabled": True},
        headers=ADMIN,
    )
    r = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "hi", "stream": False},
        headers=headers,
    )
    assert r.status_code == 403


def test_commands_still_work_for_blocked_user(client):
    """A limited user keeps local /commands (they never reach a model)."""
    headers = {"X-Dev-User": "limited2"}
    uid = _internal_id(client, headers)
    sid = _new_session(client, headers=headers)
    client.put(
        f"/api/admin/entitlements/{uid}",
        json={"requestsPerMinute": 0},
        headers=ADMIN,
    )
    r = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "/help", "stream": False},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_unlimited_user_chats_normally(client):
    headers = {"X-Dev-User": "free"}
    sid = _new_session(client, headers=headers)
    r = client.post(
        "/api/chat",
        json={"sessionId": sid, "content": "hi", "stream": False},
        headers=headers,
    )
    assert r.status_code == 200, r.text
