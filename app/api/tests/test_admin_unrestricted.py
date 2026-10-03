"""Unrestricted admins: their own usage skips per-user application caps.

The repository owner approved this scope on 2026-10-03. An admin's own usage is
exempt from per-user application caps. Security controls, provider and legal
obligations, global feature prerequisites, technical bounds, group-policy
restrictions and hard quota still apply.

Every exemption is paired on one fixture: the admin is exempt, an ordinary user
still meets the cap, spoofable dev auth never exempts a named admin, and an
``admin`` app-role principal is exempt exactly like a subject-allowlisted one.
Every identity, tenant and key here is synthetic.
"""
from __future__ import annotations

import json
import time
from functools import partial
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.auth.entra import EntraAuthProvider
from ai4ia_api.auth.identity import admin_is_unrestricted
from ai4ia_api.catalog import load_catalog
from ai4ia_api.config import Environment
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement, EntitlementLimits
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.gateway.client import ModelGatewayClient
from ai4ia_api.main import create_app
from ai4ia_api.photo_avatars import service as avatar_service_module
from ai4ia_api.photo_avatars.models import CreatePhotoAvatarRequest, PhotoAvatarError
from ai4ia_api.photo_avatars.store import MAX_LISTED
from ai4ia_api.policy.context import bind_authenticated, clear_policy_context
from ai4ia_api.policy.models import ADMIN_OPERATIONS
from ai4ia_api.policy.routes import ADMIN_ROUTE_OPERATIONS
from ai4ia_api.policy.service import PolicyService
from ai4ia_api.realtime_avatar import LiveAvatarSession, open_live_avatar
from ai4ia_api.routers.entitlements import EntitlementView
from ai4ia_api.routers.library import document_retention_cap
from tests.conftest import make_settings
from tests.test_auth_entra import _new_keypair
from tests.test_entitlement_service import CountingReader
from tests.test_photo_avatar_api import BODY, Harness, owner
from tests.test_photo_avatar_live import CATALOG as AVATAR_CATALOG
from tests.test_photo_avatar_live import RECORD_ID, Rig
from tests.test_realtime_api import _avatar_client, _avatar_connects, _seed_avatar

TENANT = "00000000-0000-4000-8000-0000000000e1"
AUDIENCE = "00000000-0000-4000-8000-0000000000e2"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
KID = "synthetic-admin-exemption-key"
SUBJECT_ADMIN = "00000000-0000-4000-8000-0000000000a1"
ROLE_ADMIN = "00000000-0000-4000-8000-0000000000a2"
ORDINARY = "00000000-0000-4000-8000-0000000000b1"
CANARY = "00000000-0000-4000-8000-0000000000c1"
# Production-shaped: group policy on, with only the monitor canary configured.
PRODUCTION_POLICY = {
    "version": 1,
    "canaryActor": {
        "tenantId": TENANT, "subject": CANARY,
        "restrictions": {"models": ["chat"], "spend": {"requestsPerMinute": 5}},
    },
}
CHAT_MODEL = next(
    item for item in load_catalog().models
    if item.conversational and item.api == "chat" and not item.reasoningEffortOptions
    and item.category in {"chat", "chat-fast"}
)


def principal(subject: str = "someone", *, roles: list[str] | None = None, email: str | None = None,
              provider: str = "entra") -> AuthenticatedUser:
    return AuthenticatedUser(
        internal_user_id=f"owner-{subject}", subject=subject, issuer=ISSUER, tenant_id=TENANT,
        provider=provider, email=email, claims={"roles": roles} if roles is not None else {},
    )


@pytest.fixture(autouse=True)
def _no_leaked_binding():
    clear_policy_context()
    yield
    clear_policy_context()


# --- the shared predicate ---------------------------------------------------


@pytest.mark.parametrize(("who", "admin"), [
    (principal(SUBJECT_ADMIN), True),
    (principal(roles=["admin"]), True),
    (principal(email="Ops@Example.test"), True),
    (principal(roles=["Admin"]), False),
    (principal(), False),
])
@pytest.mark.parametrize(("auth", "env", "trusted"), [
    ("entra", "prod", True), ("dev", "local", True), ("dev", "dev", False), ("dev", "prod", False),
])
def test_only_a_trustworthy_admin_identity_is_unrestricted(who, admin, auth, env, trusted):
    settings = make_settings(
        auth_provider=auth, env=env, admin_subjects=SUBJECT_ADMIN, admin_emails="ops@example.test",
    )
    assert settings.auth_provider_is_spoofable is (not trusted)
    assert admin_is_unrestricted(who, settings) is (admin and trusted)


# --- the real app on Entra auth ---------------------------------------------


class EntraApp:
    """The real app on Entra auth, optionally with the production-shaped policy."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, policy: bool) -> None:
        async def no_network(*_args, **_kwargs):
            pytest.fail("A synthetic control attempted a real HTTP transport.")

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
        self.private, jwks = _new_keypair(KID)
        self.app = create_app(make_settings(
            auth_provider="entra", entra_tenant_id=TENANT, entra_audience=AUDIENCE,
            applicationinsights_connection_string=None, admin_subjects=SUBJECT_ADMIN,
            group_policy_enabled=policy,
            group_policy_json=json.dumps(PRODUCTION_POLICY) if policy else None,
        ))
        self.client = TestClient(self.app)
        self.client.__enter__()
        provider = EntraAuthProvider(audience=AUDIENCE, allowed_tenants=[TENANT])
        provider._jwks_cache[TENANT] = (time.time() + 3600, jwks)
        self.app.state.auth_provider = provider
        self.calls: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ready"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
            })

        # The real client, so the shared dispatch seam (and its disabled check) runs.
        self.app.state.gateway = ModelGatewayClient(
            self.app.state.settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        )

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    def headers(self, subject: str, *roles: str) -> dict[str, str]:
        now = int(time.time())
        token = jwt.encode({
            "aud": AUDIENCE, "iss": ISSUER, "tid": TENANT, "oid": subject,
            "iat": now, "exp": now + 3600, "roles": list(roles),
        }, self.private, algorithm="RS256", headers={"kid": KID})
        return {"Authorization": "Bearer " + token}

    def owner_id(self, headers: dict[str, str]) -> str:
        return self.client.get("/api/entitlement", headers=headers).json()["userId"]

    def session(self, headers: dict[str, str]) -> str:
        created = self.client.post(
            "/api/sessions", json={"model": CHAT_MODEL.id, "libraryDocumentIds": []}, headers=headers,
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    def chat(self, headers: dict[str, str], session_id: str) -> httpx.Response:
        return self.client.post(
            "/api/chat", json={"sessionId": session_id, "content": "hello", "stream": False},
            headers=headers,
        )

    def usage_rows(self, owner_id: str) -> list:
        return list(self.app.state.usage._repo._by_user.get(owner_id, []))


# --- admin operations under the production-shaped policy --------------------

# Every route in ADMIN_ROUTE_OPERATIONS. The query flags add the two operations
# that only exist as options: directory reads and an MCP discovery refresh.
ADMIN_CALLS = (
    ("usage_summary", "GET", "/api/admin/usage/summary"),
    ("usage_by_model", "GET", "/api/admin/usage/by-model"),
    ("usage_by_day", "GET", "/api/admin/usage/by-day"),
    ("usage_agents", "GET", "/api/admin/usage/agents"),
    ("usage_user_agents", "GET", "/api/admin/usage/user-agents?identify=true"),
    ("usage_distributions", "GET", "/api/admin/usage/distributions"),
    ("usage_overview", "GET", "/api/admin/usage/overview?identify=true"),
    ("usage_by_user", "GET", "/api/admin/usage/by-user?identify=true"),
    ("metrics_resources", "GET", "/api/admin/metrics/resources"),
    ("metrics_operations", "GET", "/api/admin/metrics/operations"),
    ("metrics_security", "GET", "/api/admin/metrics/security"),
    ("metrics_web_search", "GET", "/api/admin/metrics/web-search"),
    ("metrics_official_mcp", "GET", "/api/admin/metrics/official-mcp?refresh=true"),
    ("list_overrides", "GET", "/api/admin/entitlements"),
    ("get_user_entitlement", "GET", "/api/admin/entitlements/{target}"),
    ("set_user_entitlement", "PUT", "/api/admin/entitlements/{target}"),
    ("clear_user_entitlement", "DELETE", "/api/admin/entitlements/{target}"),
)
OPTION_OPERATIONS = {"identify=true": "admin.directory.read", "refresh=true": "admin.mcp.refresh"}


def test_the_admin_calls_cover_every_admin_route_and_operation():
    assert {name for name, _, _ in ADMIN_CALLS} == set(ADMIN_ROUTE_OPERATIONS)
    covered: set[str] = set()
    for name, _, path in ADMIN_CALLS:
        covered.update(ADMIN_ROUTE_OPERATIONS[name])
        covered.update(operation for flag, operation in OPTION_OPERATIONS.items() if flag in path)
    assert covered == ADMIN_OPERATIONS


@pytest.mark.parametrize("who", ["subject-admin", "role-admin", "ordinary"])
def test_production_policy_grants_every_admin_operation_to_admins_only(monkeypatch, who):
    env = EntraApp(monkeypatch, policy=True)
    try:
        headers = {
            "subject-admin": env.headers(SUBJECT_ADMIN),
            "role-admin": env.headers(ROLE_ADMIN, "admin"),
            "ordinary": env.headers(ORDINARY),
        }[who]
        admin = who != "ordinary"
        target = env.owner_id(env.headers(ORDINARY))
        whoami = env.client.get("/api/admin/whoami", headers=headers).json()
        assert whoami["isAdmin"] is admin
        assert whoami["adminOperations"] == (sorted(ADMIN_OPERATIONS) if admin else [])
        for name, method, path in ADMIN_CALLS:
            response = env.client.request(
                method, path.format(target=target), headers=headers,
                **({"json": {"note": "synthetic"}} if method == "PUT" else {}),
            )
            expected = (204 if method == "DELETE" else 200) if admin else 403
            assert response.status_code == expected, (name, response.text)
    finally:
        env.close()


# --- entitlements -----------------------------------------------------------


@pytest.mark.parametrize("policy", [False, True], ids=["policy-off", "production-policy"])
@pytest.mark.parametrize(("override", "refused"), [
    ({"disabled": True}, 403), ({"requestsPerMinute": 0}, 429),
], ids=["disabled", "zero-rate"])
def test_admins_resolve_unlimited_whatever_is_stored_and_are_still_metered(
    monkeypatch, policy, override, refused,
):
    env = EntraApp(monkeypatch, policy=policy)
    try:
        manager = env.headers(SUBJECT_ADMIN)
        for headers, exempt in (
            (env.headers(SUBJECT_ADMIN), True),
            (env.headers(ROLE_ADMIN, "admin"), True),
            (env.headers(ORDINARY), False),
        ):
            owner_id = env.owner_id(headers)
            session_id = env.session(headers)
            put = env.client.put(f"/api/admin/entitlements/{owner_id}", json=override, headers=manager)
            assert put.status_code == 200, put.text
            # Management shows what is stored, including an admin's own override.
            stored = env.client.get(f"/api/admin/entitlements/{owner_id}", headers=manager).json()
            assert (stored["source"], stored["isUnlimited"]) == ("override", False)
            mine = env.client.get("/api/entitlement", headers=headers).json()
            before = len(env.calls)
            reply = env.chat(headers, session_id)
            if exempt:
                assert (mine["source"], mine["isUnlimited"], mine["disabled"]) == ("admin", True, False)
                assert reply.status_code == 200, reply.text
                assert len(env.calls) == before + 1
                # Accounting is never skipped for an admin.
                assert env.usage_rows(owner_id)
            else:
                assert mine["source"] == ("policy" if policy else "override")
                assert mine["isUnlimited"] is False
                assert reply.status_code == refused, reply.text
                assert len(env.calls) == before
    finally:
        env.close()


def _dev_client(env: str) -> TestClient:
    client = TestClient(create_app(make_settings(
        env=env, admin_subjects="alice",
        model_gateway_url="https://proxy.test/openai", model_gateway_auth_mode="api_key",
        model_gateway_api_key="proxy-secret", model_gateway_api_key_header="S7P-KEY",
        model_gateway_allowed_hosts="proxy.test",
    )))
    client.__enter__()
    client.app.state.gateway = ModelGatewayClient(
        client.app.state.settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ready"}}]},
        ))),
    )
    return client


@pytest.mark.parametrize(("env", "exempt"), [("local", True), ("dev", False)])
def test_spoofable_dev_auth_never_exempts_a_named_admin(env, exempt):
    client = _dev_client(env)
    try:
        alice = {"X-Dev-User": "alice"}
        assert client.app.state.settings.auth_provider_is_spoofable is (not exempt)
        owner_id = client.get("/api/entitlement", headers=alice).json()["userId"]
        created = client.post("/api/sessions", json={"model": CHAT_MODEL.id}, headers=alice)
        assert created.status_code == 201, created.text
        client.portal.call(partial(
            client.app.state.entitlements.set, owner_id, EntitlementLimits(disabled=True), updated_by=None,
        ))
        mine = client.get("/api/entitlement", headers=alice).json()
        assert (mine["source"], mine["disabled"]) == (("admin", False) if exempt else ("override", True))
        reply = client.post(
            "/api/chat", json={"sessionId": created.json()["id"], "content": "hello", "stream": False},
            headers=alice,
        )
        assert reply.status_code == (200 if exempt else 403), reply.text
    finally:
        client.__exit__(None, None, None)


def _service(**settings) -> tuple[EntitlementService, InMemoryEntitlementStore, PolicyService]:
    store = InMemoryEntitlementStore()
    entitlements = EntitlementService(store, CountingReader(), Entitlement.unlimited(), cache_ttl_seconds=0)
    policy = PolicyService(
        make_settings(admin_subjects=SUBJECT_ADMIN, **settings),
        catalog=load_catalog(), entitlements=entitlements,
    )
    return entitlements, store, policy


async def test_an_admin_binding_exempts_only_the_admins_own_soft_reads():
    entitlements, store, policy = _service()
    admin, other = principal(SUBJECT_ADMIN), principal(ORDINARY)
    for owner_id in (admin.internal_user_id, other.internal_user_id):
        await store.put(Entitlement(id=owner_id, userId=owner_id, disabled=True))
    bind_authenticated(policy, admin)
    assert (await entitlements.get_effective(admin.internal_user_id)).is_unrestricted_admin
    assert (await entitlements.check(admin.internal_user_id)).allowed
    # A binding for the admin says nothing about another owner.
    assert (await entitlements.get_effective(other.internal_user_id)).disabled
    assert not (await entitlements.check(other.internal_user_id)).allowed
    # Management and hard admission read the stored policy, even for the admin.
    assert (await entitlements.get_effective(admin.internal_user_id, exempt_admin=False)).disabled
    assert (await entitlements.get_for_admission(admin.internal_user_id)).disabled
    # Unattended work has no principal, so it is never exempt.
    clear_policy_context()
    assert (await entitlements.get_effective(admin.internal_user_id)).disabled


async def test_hard_admission_keeps_its_known_disabled_outage_fallback_for_an_admin(monkeypatch):
    entitlements, store, policy = _service()
    admin = principal(SUBJECT_ADMIN)
    await store.put(Entitlement(id=admin.internal_user_id, userId=admin.internal_user_id, disabled=True))
    bind_authenticated(policy, admin)
    assert (await entitlements.get_for_admission(admin.internal_user_id)).disabled

    async def outage(_owner):
        raise RuntimeError("synthetic store outage")

    monkeypatch.setattr(store, "get", outage)
    monkeypatch.setattr(store, "get_strict", outage)
    # The last-known disabled policy still answers, exactly as for anyone else.
    assert (await entitlements.get_for_admission(admin.internal_user_id)).disabled
    # Control: the admin's own soft read is exempt on the same fixture.
    assert (await entitlements.get_effective(admin.internal_user_id)).is_unrestricted_admin


async def test_group_spend_still_composes_onto_an_admins_unrestricted_base():
    entitlements, store, policy = _service(
        group_policy_enabled=True, group_policy_json=json.dumps({"spend": {"default": {"tokensPerDay": 100}}}),
        entra_tenant_id=TENANT, entra_allowed_tenants=TENANT,
    )
    admin = principal(SUBJECT_ADMIN)
    await store.put(Entitlement(id=admin.internal_user_id, userId=admin.internal_user_id, disabled=True))
    restricted = (await policy.resolve(admin)).limits
    # The stored override does not apply, but the group restriction still does.
    assert (restricted.disabled, restricted.tokensPerDay) == (False, 100)
    assert EntitlementView.of(admin.internal_user_id, restricted).source != "admin"
    # Control: with no group restriction the admin resolves unrestricted.
    policy.settings.group_policy_json = json.dumps({"version": 1})
    unrestricted = (await policy.resolve(admin)).limits
    assert unrestricted.is_unrestricted_admin and unrestricted.is_unlimited
    assert EntitlementView.of(admin.internal_user_id, unrestricted).source == "admin"


# --- photo avatars ----------------------------------------------------------


def test_admins_skip_both_avatar_caps_and_every_creation_is_still_counted():
    harness = Harness(photo_avatar_max_per_user=1, photo_avatar_max_creations_per_day=1, admin_subjects="alice")
    try:
        # Control on the same fixture: an ordinary user meets both caps.
        bob_first = harness.create(user="bob").json()
        refused = harness.create(user="bob")
        assert refused.status_code == 409 and refused.json()["code"] == "avatar_limit_reached"
        bob = harness.call("GET", "/config", user="bob").json()
        assert (bob["canCreate"], bob["limits"]["unlimited"], bob["limits"]["maxAvatars"]) == (False, False, 1)
        assert harness.call("DELETE", f"/{bob_first['id']}", user="bob").status_code == 204
        daily = harness.create(user="bob")
        assert daily.status_code == 429 and daily.json()["code"] == "daily_creation_limit"

        created = [harness.create(user="alice") for _ in range(3)]
        assert [response.status_code for response in created] == [202, 202, 202]
        assert harness.call("DELETE", f"/{created[0].json()['id']}", user="alice").status_code == 204
        assert harness.create(user="alice").status_code == 202
        alice = harness.call("GET", "/config", user="alice").json()
        assert alice["canCreate"] is True
        assert {key: alice["limits"][key] for key in (
            "unlimited", "maxAvatars", "avatarCount", "creationsInLastDay", "maxCreationsPerDay",
            "nextCreationAt",
        )} == {
            "unlimited": True, "maxAvatars": MAX_LISTED, "avatarCount": 3, "creationsInLastDay": 4,
            "maxCreationsPerDay": 1, "nextCreationAt": None,
        }
        # Accounting is unchanged: the ledger counts every creation, each metered once.
        ledger = harness.client.portal.call(harness.service._store.ledger, owner("alice"))
        assert len(ledger.active) == 3 and len(ledger.recent(harness.clock())) == 4
        assert sum(row.userId == owner("alice") for row in harness.usage_rows()) == 4
    finally:
        harness.close()


def test_the_gallery_listing_bound_still_applies_to_an_admin(monkeypatch):
    monkeypatch.setattr(avatar_service_module, "MAX_LISTED", 2)
    harness = Harness(photo_avatar_max_per_user=1, photo_avatar_max_creations_per_day=1, admin_subjects="alice")
    try:
        assert [harness.create(user="alice").status_code for _ in range(2)] == [202, 202]
        refused = harness.create(user="alice")
        assert refused.status_code == 409 and refused.json()["code"] == "avatar_limit_reached"
        assert "2 avatars" in refused.json()["detail"]
        config = harness.call("GET", "/config", user="alice").json()
        assert (config["canCreate"], config["limits"]["unlimited"], config["limits"]["maxAvatars"]) == (
            False, True, 2,
        )
    finally:
        harness.close()


def test_spoofable_dev_auth_keeps_both_avatar_caps_for_a_named_admin():
    harness = Harness(photo_avatar_max_per_user=1, photo_avatar_max_creations_per_day=5, admin_subjects="alice")
    try:
        settings = harness.app.state.settings
        settings.env = Environment.dev
        assert settings.auth_provider_is_spoofable
        assert harness.create(user="alice").status_code == 202
        refused = harness.create(user="alice")
        assert refused.status_code == 409 and refused.json()["code"] == "avatar_limit_reached"
        assert harness.call("GET", "/config", user="alice").json()["limits"]["unlimited"] is False
        # Control: the same admin on trustworthy auth.
        settings.env = Environment.local
        assert harness.create(user="alice").status_code == 202
        assert harness.call("GET", "/config", user="alice").json()["limits"]["unlimited"] is True
    finally:
        harness.close()


@pytest.mark.parametrize(("roles", "exempt"), [(["admin"], True), ([], False)])
def test_an_admin_app_role_skips_avatar_caps_like_an_allowlisted_subject(roles, exempt):
    harness = Harness(photo_avatar_max_per_user=1, photo_avatar_max_creations_per_day=1)
    try:
        who = principal(f"role-{len(roles)}", roles=roles)
        request = CreatePhotoAvatarRequest.model_validate(BODY)
        config = harness.client.portal.call(harness.service.config, who)
        assert config.limits is not None and config.limits.unlimited is exempt
        harness.client.portal.call(harness.service.create, who, request)
        if exempt:
            harness.client.portal.call(harness.service.create, who, request)
        else:
            with pytest.raises(PhotoAvatarError) as caught:
                harness.client.portal.call(harness.service.create, who, request)
            assert caught.value.code == "avatar_limit_reached"
    finally:
        harness.close()


# --- live avatar session length --------------------------------------------


@pytest.mark.parametrize(("realtime_cap", "admin_cap", "user_cap"), [
    (0.0, 0, 600), (90.0, 90, 90), (900.0, 900, 600),
])
def test_only_an_admin_skips_the_live_minute_cap_and_the_idle_limit_holds(realtime_cap, admin_cap, user_cap):
    c, rig = _avatar_client(realtime_max_session_seconds=realtime_cap)  # alice is this suite's admin
    try:
        for user, expected in (("alice", admin_cap), ("bob", user_cap)):
            _seed_avatar(c, rig, user=user)
            session = _avatar_connects(c, user=user)
            assert session["type"] == "ai4ia.avatar.session"
            assert session["max_session_seconds"] == expected
            assert (session["idle_timeout_seconds"], session["idle_warning_seconds"]) == (120, 30)
    finally:
        c.__exit__(None, None, None)


def test_spoofable_dev_auth_keeps_the_live_minute_cap_for_a_named_admin():
    c, rig = _avatar_client()
    try:
        _seed_avatar(c, rig)
        settings = c.app.state.settings
        settings.env = Environment.dev
        settings.realtime_allowed_origins = "http://localhost:3000"
        assert settings.auth_provider_is_spoofable
        assert _avatar_connects(c)["max_session_seconds"] == 600
        # Control: the same admin on trustworthy auth.
        settings.env = Environment.local
        assert _avatar_connects(c)["max_session_seconds"] == 0
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize(("roles", "expected"), [(["admin"], 0.0), ([], 600.0)])
async def test_an_admin_app_role_skips_the_live_minute_cap_like_an_allowlisted_subject(roles, expected):
    rig = Rig()
    who = principal(f"live-{len(roles)}", roles=roles)
    await rig.seed(who.internal_user_id)
    state = SimpleNamespace(
        photo_avatars=rig.service, settings=rig.settings,
        usage=SimpleNamespace(pricing=rig.service._pricing),
    )
    opened = await open_live_avatar(
        state, rig.settings, who, RECORD_ID, session_region=AVATAR_CATALOG.homeRegion,
    )
    assert isinstance(opened, LiveAvatarSession)
    assert opened.max_seconds == expected
    assert opened.idle_timeout_seconds == float(rig.settings.photo_avatar_live_idle_timeout_seconds)


# --- documents --------------------------------------------------------------


def _upload(client: TestClient, user: str, name: str) -> httpx.Response:
    return client.post(
        "/api/library/documents", headers={"X-Dev-User": user},
        files={"file": (name, f"{user} {name} content".encode(), "text/plain")},
    )


def test_admins_skip_the_document_retention_cap():
    client = TestClient(create_app(make_settings(
        document_understanding_enabled=True, document_max_per_user=1, admin_subjects="alice",
    )))
    client.__enter__()
    try:
        for user, exempt in (("alice", True), ("bob", False)):
            headers = {"X-Dev-User": user}
            assert _upload(client, user, "first.txt").status_code == 201
            second = _upload(client, user, "second.txt")
            assert second.status_code == (201 if exempt else 409), second.text
            assert client.get("/api/library/summary", headers=headers).json()["maxDocuments"] == (
                0 if exempt else 1
            )
            capabilities = client.get("/api/attachments/capabilities", headers=headers).json()
            assert capabilities["maxPerUserDocuments"] == (0 if exempt else 1)
        # Spoofable dev auth never exempts the same named admin.
        client.app.state.settings.env = Environment.dev
        assert _upload(client, "alice", "third.txt").status_code == 409
        assert client.get("/api/library/summary", headers={"X-Dev-User": "alice"}).json()["maxDocuments"] == 1
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(("roles", "cap"), [(["admin"], 0), ([], 1)])
def test_an_admin_app_role_skips_the_document_cap_like_an_allowlisted_subject(roles, cap):
    settings = make_settings(document_max_per_user=1)
    assert document_retention_cap(settings, principal("documents", roles=roles)) == cap
