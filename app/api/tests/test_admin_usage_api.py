"""Admin usage/metrics HTTP surface: gating + correctness + graceful degrade.

The security contract: EVERY ``/api/admin/usage/*`` and ``/api/admin/metrics/*``
route is behind ``require_admin`` (admin 200 / non-admin 403 / anon 401). The one
exception is ``/api/admin/whoami`` (auth-only) which returns an ``isAdmin`` boolean
so the web client can *hide* a nav entry — it never gates anything.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_servers import UserMcpServer
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
    "/api/admin/usage/overview",
    "/api/admin/metrics/resources",
    "/api/admin/metrics/web-search",
    "/api/admin/metrics/official-mcp",
    "/api/admin/metrics/operations",
    "/api/admin/metrics/security",
]


def test_operations_window_is_bounded(client):
    assert client.get(
        "/api/admin/metrics/operations?minutes=14", headers=ADMIN
    ).status_code == 422
    assert client.get(
        "/api/admin/metrics/security?minutes=1441", headers=ADMIN
    ).status_code == 422


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
        provider="azure_openai",
        model="gpt-5.2",
        deployment="dep",
        target=None,
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


def test_admin_denial_emits_content_free_security_event(client, monkeypatch):
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ai4ia_api.auth.admin.emit_security_block",
        lambda category, reason, source: events.append((category, reason, source)),
    )
    assert client.get("/api/admin/metrics/security", headers=NON_ADMIN).status_code == 403
    assert events == [("admin_auth", "privileges_required", "admin_dependency")]


def test_auth_failure_emits_content_free_security_event(client, monkeypatch):
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ai4ia_api.auth.dependencies.emit_security_block",
        lambda category, reason, source: events.append((category, reason, source)),
    )
    client.app.state.auth_provider = _RaisingAuth()
    assert client.get("/api/admin/metrics/security").status_code == 401
    assert events == [("http_auth", "authentication_failed", "auth_dependency")]


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


def test_distributions_handle_null_deployment_and_surface_provider_rollup(client):
    _seed(
        client,
        [
            _rec(
                "alice",
                provider="speech_voice_live",
                deployment=None,
                target="managed_voice_live",
                status="complete",
                billable=False,
                usageKnown=False,
                usageComplete=False,
                promptTokens=None,
                completionTokens=None,
                totalTokens=None,
                costKnown=False,
                estCostMicroUsd=None,
            ),
        ],
    )
    body = client.get("/api/admin/usage/distributions", headers=ADMIN).json()
    assert {b["key"]: b["requests"] for b in body["byProvider"]} == {
        "speech_voice_live": 1
    }
    assert {b["key"]: b["requests"] for b in body["byDeployment"]} == {
        "(unknown)": 1
    }
    summary = client.get("/api/admin/usage/summary", headers=ADMIN).json()
    assert summary["distinctProviders"] == 1


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


# ---- consolidated overview (audit P1-15: one scan, not seven) ----


def test_overview_returns_every_panel_the_fan_out_used_to_fetch(client):
    _seed(
        client,
        [
            _rec("alice", model="big", totalTokens=100, agent="research", region="eastus", dataZone="us"),
            _rec("bob", model="small", totalTokens=5, agent="coder", region="westus"),
            _rec("bob", status="error", billable=False, totalTokens=0, deployment=None),
        ],
    )
    body = client.get("/api/admin/usage/overview", headers=ADMIN).json()

    assert body["summary"]["activeUsers"] == 2
    assert body["summary"]["totalRequests"] == 3
    assert body["byModel"][0]["model"] == "big"
    assert body["byDay"] and body["byDay"][0]["requests"] >= 1
    assert body["totalUsers"] == 2
    assert body["byUser"][0]["userId"] == "alice"
    # No entitlement override seeded -> the unlimited default (entitlement None).
    assert body["byUser"][0]["entitlement"] is None
    assert {a["agent"] for a in body["agents"]} == {"research", "coder"}
    assert {(c["userId"], c["agent"]) for c in body["userAgents"]} == {
        ("alice", "research"),
        ("bob", "coder"),
    }
    assert {b["key"]: b["requests"] for b in body["byRegion"]} == {
        "eastus": 1,
        "westus": 1,
        "(unknown)": 1,
    }
    assert {b["key"] for b in body["byDeployment"]} == {"dep", "(unknown)"}
    assert {b["key"]: b["requests"] for b in body["byStatus"]} == {"complete": 2, "error": 1}
    assert body["byProvider"][0]["key"] == "azure_openai"
    # Every section resolved from the single scan.
    assert body["partialSections"] == []
    assert body["scannedRecords"] == 3
    assert body["truncated"] is False


def test_overview_agrees_with_the_seven_endpoints_it_replaces(client):
    _seed(
        client,
        [
            _rec("alice", model="big", totalTokens=100, agent="research", region="eastus"),
            _rec("bob", model="small", totalTokens=5, agent="coder"),
            _rec("carol", status="cancelled", billable=False, costKnown=False, estCostMicroUsd=None),
        ],
    )
    overview = client.get("/api/admin/usage/overview", headers=ADMIN).json()
    summary = client.get("/api/admin/usage/summary", headers=ADMIN).json()
    by_model = client.get("/api/admin/usage/by-model", headers=ADMIN).json()
    by_day = client.get("/api/admin/usage/by-day", headers=ADMIN).json()
    by_user = client.get("/api/admin/usage/by-user", headers=ADMIN).json()
    agents = client.get("/api/admin/usage/agents", headers=ADMIN).json()
    user_agents = client.get("/api/admin/usage/user-agents", headers=ADMIN).json()
    distributions = client.get("/api/admin/usage/distributions", headers=ADMIN).json()

    window = {"fromTime", "toTime"}
    assert {k: v for k, v in overview["summary"].items() if k not in window} == {
        k: v for k, v in summary.items() if k not in window
    }
    assert overview["byModel"] == by_model["byModel"]
    assert overview["byDay"] == by_day["byDay"]
    assert overview["byUser"] == by_user["byUser"]
    assert overview["totalUsers"] == by_user["totalUsers"]
    assert overview["agents"] == agents["agents"]
    assert overview["userAgents"] == user_agents["userAgents"]
    for key in ("byRegion", "byDataZone", "byProvider", "byDeployment", "byStatus"):
        assert overview[key] == distributions[key]


def test_overview_never_renders_unknown_cost_as_zero(client):
    _seed(
        client,
        [
            _rec("alice", totalTokens=15, estCostMicroUsd=1000),
            # Billable, metered, but unpriced -> cost unknown, not zero.
            _rec("alice", totalTokens=15, costKnown=False, estCostMicroUsd=None),
            # No usage reported at all -> tokens unknown, not zero.
            _rec(
                "bob",
                usageKnown=False,
                promptTokens=None,
                completionTokens=None,
                totalTokens=None,
                costKnown=False,
                estCostMicroUsd=None,
            ),
        ],
    )
    body = client.get("/api/admin/usage/overview", headers=ADMIN).json()
    assert body["summary"]["costUnknownRequests"] == 2
    assert body["summary"]["unknownUsageRequests"] == 1
    assert body["summary"]["totalCostMicroUsd"] == 1000
    assert body["summary"]["totalTokens"] == 30
    alice = next(r for r in body["byUser"] if r["userId"] == "alice")
    assert alice["costKnown"] is False
    assert next(b for b in body["byModel"] if b["model"] == "gpt-5.2")["costKnown"] is False


def test_overview_honours_window_and_user_paging(client):
    _seed(client, [_rec(f"user-{i}", totalTokens=100 - i) for i in range(5)])

    body = client.get("/api/admin/usage/overview?days=7&limit=2&offset=1", headers=ADMIN).json()
    assert body["sinceDays"] == 7
    assert body["userLimit"] == 2
    assert body["userOffset"] == 1
    assert [r["userId"] for r in body["byUser"]] == ["user-1", "user-2"]
    assert body["totalUsers"] == 5

    # Same bounds as every other admin route.
    assert client.get("/api/admin/usage/overview?days=9999", headers=ADMIN).status_code == 422
    assert client.get("/api/admin/usage/overview?limit=0", headers=ADMIN).status_code == 422
    assert client.get("/api/admin/usage/overview?limit=201", headers=ADMIN).status_code == 422
    assert client.get("/api/admin/usage/overview?offset=-1", headers=ADMIN).status_code == 422


def test_overview_joins_entitlement_override(client):
    uid = client.get("/api/entitlement", headers={"X-Dev-User": "target"}).json()["userId"]
    _seed(client, [_rec(uid, totalTokens=15, agent="research")])
    assert client.put(
        f"/api/admin/entitlements/{uid}", json={"tokensPerDay": 5000}, headers=ADMIN
    ).status_code == 200
    body = client.get("/api/admin/usage/overview", headers=ADMIN).json()
    row = next(r for r in body["byUser"] if r["userId"] == uid)
    assert row["entitlement"]["tokensPerDay"] == 5000


def test_overview_is_de_identified_by_default_and_enriches_on_request(client):
    uid = client.get("/api/entitlement", headers={"X-Dev-User": "dave"}).json()["userId"]
    # Directory capture is fire-and-forget on the server loop; wait for it to land.
    repo = client.app.state.user_directory._repo
    for _ in range(50):
        if uid in repo._by_user:
            break
        time.sleep(0.02)
    assert uid in repo._by_user, "capture did not populate the directory"
    _seed(client, [_rec(uid, totalTokens=42, agent="research")])

    anonymous = client.get("/api/admin/usage/overview", headers=ADMIN).json()
    assert next(r for r in anonymous["byUser"] if r["userId"] == uid)["displayName"] is None
    assert next(c for c in anonymous["userAgents"] if c["userId"] == uid)["displayName"] is None

    identified = client.get("/api/admin/usage/overview?identify=true", headers=ADMIN).json()
    user_row = next(r for r in identified["byUser"] if r["userId"] == uid)
    agent_cell = next(c for c in identified["userAgents"] if c["userId"] == uid)
    # One directory read serves both user-keyed sections.
    assert user_row["displayName"] == "dave"
    assert user_row["email"] == "dave@example.com"
    assert agent_cell["displayName"] == "dave"
    assert agent_cell["email"] == "dave@example.com"


def test_overview_marks_truncation_like_the_endpoints_it_replaces(client):
    client.app.state.admin_usage._max_records = 2
    _seed(client, [_rec("alice"), _rec("bob"), _rec("carol")])
    body = client.get("/api/admin/usage/overview", headers=ADMIN).json()
    assert body["truncated"] is True
    assert body["scannedRecords"] == 2


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


# ---- web search health (diagnostics for the otherwise-invisible fail-soft path) ----


def test_web_search_health_reports_counts_and_recent_failures(client):
    # The recorder is always present (built at startup even when the feature is
    # off), so the endpoint always answers. Drive it directly to simulate calls.
    health = client.app.state.web_search_health
    health.record_success()
    health.record_failure("auth", "401 from api.microsoft.ai\nnot entitled")
    health.record_failure("auth", "401 again")
    health.record_failure("connection", "timed out")

    body = client.get("/api/admin/metrics/web-search", headers=ADMIN).json()
    assert body["successes"] == 1
    assert body["failures"] == 3
    assert body["totalCalls"] == 4
    # Counts are grouped by category in a stable display order (auth before connection).
    assert [c["category"] for c in body["byCategory"]] == ["auth", "connection"]
    assert {c["category"]: c["count"] for c in body["byCategory"]} == {"auth": 2, "connection": 1}
    # Recent ring buffer is newest-first, de-identified, and single-lined.
    assert body["recent"][0]["category"] == "connection"
    assert all("\n" not in (r["detail"] or "") for r in body["recent"])
    assert all("userId" not in r for r in body["recent"])


def test_web_search_health_posture_unconfigured_by_default(client):
    # Default test settings: feature off, no key, no Entra fallback.
    body = client.get("/api/admin/metrics/web-search", headers=ADMIN).json()
    assert body["enabled"] is False
    assert body["authMode"] == "unconfigured"


def test_web_search_health_authmode_api_key():
    # A configured API key -> authMode "api_key" (the key itself is never returned).
    c = _client(web_search_enabled=True, webiq_api_key="secret-key")
    try:
        body = c.get("/api/admin/metrics/web-search", headers=ADMIN).json()
        assert body["enabled"] is True
        assert body["authMode"] == "api_key"
        assert "secret-key" not in c.get(
            "/api/admin/metrics/web-search", headers=ADMIN
        ).text
    finally:
        c.__exit__(None, None, None)


def test_web_search_health_authmode_managed_identity():
    # No key but Entra fallback on -> managed identity (this bug's smoking gun when
    # paired with a run of auth failures).
    c = _client(webiq_use_entra=True)
    try:
        body = c.get("/api/admin/metrics/web-search", headers=ADMIN).json()
        assert body["authMode"] == "managed_identity"
    finally:
        c.__exit__(None, None, None)


# ---- official MCP reachability ----


class _StubOfficialMcp:
    """Minimal stand-in for OfficialMcpService (only what the endpoint reads)."""

    def __init__(self, servers):
        self._servers = servers
        self.refresh_calls = 0

    def refresh(self) -> None:
        self.refresh_calls += 1

    async def list_all(self):
        return self._servers


def _official_server(name: str, *, tools: list, last_error: str | None):
    return UserMcpServer(
        id=name,
        userId="__official__",
        name=name,
        displayName=f"Toolbox {name}",
        endpoint=f"https://apim.example.net/{name}/mcp",
        host="apim.example.net",
        discoveredTools=tools,
        lastError=last_error,
    )


def test_official_mcp_absent_service_reports_posture_not_500(client):
    # Feature off in default test settings -> app.state.official_mcp_service is None.
    # The endpoint must still answer, so "off" is distinguishable from "broken".
    body = client.get("/api/admin/metrics/official-mcp", headers=ADMIN).json()
    assert body["enabled"] is False
    assert body["gatewayConfigured"] is False
    assert body["servers"] == []


def test_official_mcp_reports_unreachable_upstream(client):
    # THE REGRESSION THIS ENDPOINT EXISTS FOR: APIM is healthy and `initialize`
    # succeeds, but the upstream toolbox does not exist so `tools/list` fails.
    # Discovery yields zero tools plus an error — that pair must be visible.
    client.app.state.official_mcp_service = _StubOfficialMcp(
        [_official_server("ai4ia-toolbox", tools=[], last_error="Toolbox not found")]
    )
    body = client.get("/api/admin/metrics/official-mcp", headers=ADMIN).json()
    assert len(body["servers"]) == 1
    server = body["servers"][0]
    assert server["name"] == "ai4ia-toolbox"
    assert server["toolCount"] == 0
    assert server["lastError"] == "Toolbox not found"


def test_official_mcp_refresh_is_opt_in(client):
    stub = _StubOfficialMcp([_official_server("ai4ia-toolbox", tools=[], last_error=None)])
    client.app.state.official_mcp_service = stub
    client.get("/api/admin/metrics/official-mcp", headers=ADMIN)
    assert stub.refresh_calls == 0  # cached read by default
    client.get("/api/admin/metrics/official-mcp?refresh=true", headers=ADMIN)
    assert stub.refresh_calls == 1  # explicit re-probe after a fix


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
