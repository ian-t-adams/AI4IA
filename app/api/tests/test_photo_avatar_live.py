"""Server-only live avatar grants: ownership, readiness, availability and re-verification.

Each refusal is paired with the identical call succeeding once only the guarded
condition flips. The provider is a synthetic fake behind the governed route.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.catalog import load_catalog
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.library.blob_store import InMemoryBlobStore
from ai4ia_api.photo_avatars import live
from ai4ia_api.photo_avatars.availability import CapabilityProbe
from ai4ia_api.photo_avatars.catalog import load_photo_avatar_catalog
from ai4ia_api.photo_avatars.live import LIVE_AVATAR_ERROR_CODES, LiveAvatarError, LiveAvatarGrant
from ai4ia_api.photo_avatars.preview import PhotoAvatarArtifactStore
from ai4ia_api.photo_avatars.provider import PhotoAvatarGateway
from ai4ia_api.photo_avatars.service import REVERIFY_COOLDOWN, PhotoAvatarService
from ai4ia_api.photo_avatars.store import PhotoAvatarRecord, PhotoAvatarStore, PreviewMeta
from ai4ia_api.policy.context import bind_authenticated, clear_policy_context
from ai4ia_api.policy.service import PolicyService
from ai4ia_api.publishing.store import InMemoryRecordStore
from ai4ia_api.usage.memory_repo import InMemoryUsageRepository
from ai4ia_api.usage.pricing import load_pricing
from ai4ia_api.usage.service import UsageService
from tests.conftest import make_settings
from tests.test_entitlement_service import CountingReader
from tests.test_group_policy import GROUP, TENANT
from tests.test_group_policy import user as claims_user

CATALOG = load_photo_avatar_catalog()
T0 = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
PROVIDER_ID = "ai4ia-0123456789abcdef0123"
RECORD_ID = "0123456789abcdef0123456789abcdef"
OTHER_RECORD_ID = "fedcba9876543210fedcba9876543210"
NOT_FOUND = {"error": {"code": "NotFound", "message": "Synthetic avatar not found."}}


def person(owner: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        internal_user_id=owner, subject=owner, issuer="issuer", tenant_id="tenant", provider="dev",
    )


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class Provider:
    def __init__(self) -> None:
        self.features: object = ["CustomAvatar"]
        self.features_status = 200
        self.avatar_reply: httpx.Response = httpx.Response(200, json={"state": "Succeeded"})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = request.url.path.removeprefix("/ai4ia-photo-avatars-v1")
        if route == "/features":
            return httpx.Response(self.features_status, json=self.features)
        assert request.method == "GET" and route.startswith("/photoavatars/"), route
        return self.avatar_reply

    def reads(self, route: str) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith(route))


class Rig:
    def __init__(self, *, policy: PolicyService | None = None, **settings) -> None:
        values = dict(photo_avatars_enabled=True, model_gateway_url="https://proxy.test/openai")
        values.update(settings)
        self.settings = make_settings(**values)
        self.provider, self.clock, self.records = Provider(), Clock(), InMemoryRecordStore()
        gateway = PhotoAvatarGateway(self.settings, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(self.provider), follow_redirects=False,
        ))
        entitlements = EntitlementService(InMemoryEntitlementStore(), CountingReader(), Entitlement.unlimited())
        usage = UsageService(InMemoryUsageRepository(), load_pricing(), enabled=True)
        self.policy = policy or PolicyService(self.settings, catalog=load_catalog(), entitlements=entitlements)
        self.store = PhotoAvatarStore(self.records)
        self.service = PhotoAvatarService(
            settings=self.settings, catalog=CATALOG, store=self.store,
            artifacts=PhotoAvatarArtifactStore(InMemoryBlobStore()), gateway=gateway,
            capability=CapabilityProbe(gateway, CATALOG.requiredFeature),
            entitlements=entitlements, usage=usage, pricing=usage.pricing, clock=self.clock,
            policy=self.policy,
        )

    async def seed(self, owner: str = "alice", record_id: str = RECORD_ID, **fields) -> PhotoAvatarRecord:
        values = dict(
            id=record_id, userId=owner, providerAvatarId=PROVIDER_ID, homeRegion=CATALOG.homeRegion,
            displayName="Host", prompt="A friendly host.", attributes={"gender": None},
            attestationVersion="2026-09-25", attestedAt=T0, status="ready",
            preview=PreviewMeta(width=1024, height=1024, bytes=64, sha256Prefix="0" * 16),
            createdAt=T0, updatedAt=T0, readyAt=T0,
        )
        values.update(fields)
        record = PhotoAvatarRecord(**values)
        await self.store.reserve(record, max_avatars=50, max_per_day=50, now=T0)
        return record

    async def stored(self, owner: str = "alice", record_id: str = RECORD_ID) -> PhotoAvatarRecord:
        loaded = await self.store.get(owner, record_id)
        assert loaded is not None
        return loaded[0]


@pytest.fixture(autouse=True)
def _no_leaked_binding():
    clear_policy_context()
    yield
    clear_policy_context()


async def refusal(rig: Rig, user: AuthenticatedUser, record_id: str = RECORD_ID) -> LiveAvatarError:
    with pytest.raises(LiveAvatarError) as caught:
        await rig.service.resolve_live_avatar(user, record_id)
    assert caught.value.code in LIVE_AVATAR_ERROR_CODES
    return caught.value


async def test_the_owner_gets_an_immutable_grant_that_hides_the_provider_id_from_repr():
    rig = Rig()
    await rig.seed()
    grant = await rig.service.resolve_live_avatar(person("alice"), RECORD_ID)
    assert grant == LiveAvatarGrant(
        record_id=RECORD_ID, provider_avatar_id=PROVIDER_ID,
        base_model=CATALOG.baseModel, home_region=CATALOG.homeRegion,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.provider_avatar_id = "ai4ia-ffffffffffffffffffff"  # type: ignore[misc]
    assert PROVIDER_ID not in repr(grant) and PROVIDER_ID not in str(grant)


@pytest.mark.parametrize("who, record_id", [
    ("bob", RECORD_ID),  # another user's record
    ("alice", OTHER_RECORD_ID),  # unknown, well-formed
    ("alice", "not-a-record-id"),
    ("alice", RECORD_ID.upper()),
    ("alice", PROVIDER_ID),  # the provider id is never an accepted handle
    ("alice", ""),
])
async def test_foreign_unknown_and_malformed_ids_are_one_indistinguishable_not_found(who, record_id):
    rig = Rig()
    await rig.seed()
    await rig.seed(owner="bob", record_id="ab" * 16)
    error = await refusal(rig, person(who), record_id)
    assert (error.status, error.code, error.detail, error.reason) == (404, "not_found", "Not found.", None)
    # Control: the owner's own id resolves on the same rig.
    assert (await rig.service.resolve_live_avatar(person("alice"), RECORD_ID)).record_id == RECORD_ID


@pytest.mark.parametrize("fields", [
    {"status": "creating", "readyAt": None},
    {"status": "generating", "readyAt": None},
    {"status": "confirming", "readyAt": None},
    {"status": "failed", "failureCode": "provider_failed"},
    {"status": "deleting"},
    {"preview": None},  # ready without a stored preview
    {"providerAvatarId": "demo-ai-host"},  # not an AI4IA-issued provider id
])
async def test_only_a_ready_record_with_a_preview_and_issued_provider_id_is_granted(fields):
    rig = Rig()
    await rig.seed(**fields)
    error = await refusal(rig, person("alice"))
    assert (error.status, error.code) == (409, "avatar_not_ready")
    control = Rig()
    await control.seed()
    assert await control.service.resolve_live_avatar(person("alice"), RECORD_ID)


async def test_a_record_from_another_home_account_is_refused():
    rig = Rig()
    await rig.seed(homeRegion="swedencentral")
    assert (await refusal(rig, person("alice"))).code == "avatar_home_changed"
    await rig.store.remove("alice", RECORD_ID)
    await rig.seed()
    assert await rig.service.resolve_live_avatar(person("alice"), RECORD_ID)


@pytest.mark.parametrize("features, status, reason", [
    (["BuiltInVoicePreview"], 200, "capability_unavailable"),
    (["customavatar", "CustomAvatarPreview"], 200, "capability_unavailable"),
    ({"CustomAvatar": True}, 200, "capability_unknown"),
    (["CustomAvatar"], 503, "capability_unknown"),
])
async def test_the_capability_is_required_and_fails_closed(features, status, reason):
    rig = Rig()
    await rig.seed()
    rig.provider.features, rig.provider.features_status = features, status
    error = await refusal(rig, person("alice"))
    assert (error.status, error.code, error.reason) == (503, "photo_avatars_unavailable", reason)
    # Control: the exact feature admits the identical call once the cache is refreshed.
    rig.provider.features, rig.provider.features_status = ["CustomAvatar"], 200
    rig.service._capability.invalidate()
    assert await rig.service.resolve_live_avatar(person("alice"), RECORD_ID)


@pytest.mark.parametrize("enabled, residency, reason", [
    (False, "global", "disabled"),
    (True, "eu", "residency_unsupported"),
])
async def test_the_flag_and_residency_are_rechecked(enabled, residency, reason):
    rig = Rig(photo_avatars_enabled=enabled, data_residency=residency)
    await rig.seed()
    error = await refusal(rig, person("alice"))
    assert (error.code, error.reason) == ("photo_avatars_unavailable", reason)
    assert rig.provider.reads("/features") == 0  # the predicate stops before probing
    control = Rig(photo_avatars_enabled=True, data_residency="us")
    await control.seed()
    assert await control.service.resolve_live_avatar(person("alice"), RECORD_ID)


def pilot(allow: list[str]) -> dict:
    return {"version": 1, "domains": {"avatars": {
        "default": {"allow": []},
        "mappings": [{"claim": "groups", "value": GROUP, "allow": allow}],
    }}}


def group_policy(config: dict) -> PolicyService:
    settings = make_settings(
        group_policy_enabled=True, group_policy_json=json.dumps(config),
        entra_tenant_id=TENANT, entra_allowed_tenants=TENANT,
    )
    entitlements = EntitlementService(InMemoryEntitlementStore(), CountingReader(), Entitlement.unlimited())
    return PolicyService(settings, catalog=load_catalog(), entitlements=entitlements)


@pytest.mark.parametrize("allow, groups, granted", [
    (["use"], [GROUP], True),
    (["use"], [], False),  # not in the pilot group
    (["create"], [GROUP], False),  # creating is not using
    (["create", "use"], [GROUP], True),
])
async def test_policy_enforces_avatar_use_for_the_bound_caller(allow, groups, granted):
    policy = group_policy(pilot(allow))
    rig = Rig(policy=policy)
    member = claims_user(groups=groups)
    await rig.seed(owner=member.internal_user_id)
    bind_authenticated(policy, member)
    if granted:
        assert await rig.service.resolve_live_avatar(member, RECORD_ID)
    else:
        error = await refusal(rig, member)
        assert (error.status, error.code) == (403, "policy_denied")
        assert rig.provider.reads("/features") == 0  # policy is checked before the probe


async def test_enabled_policy_without_a_bound_caller_is_unavailable_never_allowed():
    policy = group_policy(pilot(["use"]))
    rig = Rig(policy=policy)
    member = claims_user(groups=[GROUP])
    await rig.seed(owner=member.internal_user_id)
    error = await refusal(rig, member)  # nothing bound for this request
    assert (error.code, error.reason) == ("photo_avatars_unavailable", "policy_unavailable")
    bind_authenticated(policy, member)
    assert await rig.service.resolve_live_avatar(member, RECORD_ID)


async def test_a_binding_for_a_different_caller_is_denied():
    rig = Rig()
    await rig.seed(owner="alice")
    bind_authenticated(rig.policy, person("mallory"))
    assert (await refusal(rig, person("alice"))).code == "policy_denied"
    bind_authenticated(rig.policy, person("alice"))
    assert await rig.service.resolve_live_avatar(person("alice"), RECORD_ID)


async def test_a_verification_failure_refuses_until_a_fresh_read_succeeds():
    rig = Rig()
    await rig.seed()
    assert await rig.service.mark_live_avatar_verification_failed(
        person("alice"), RECORD_ID, provider_code="avatar_verification_failed",
    )
    stored = await rig.stored()
    assert stored.liveVerificationFailedAt == T0 and stored.liveVerificationCode == "avatar_verification_failed"
    error = await refusal(rig, person("alice"))
    assert error.code == "avatar_needs_reverification"
    assert error.retry_after == int(REVERIFY_COOLDOWN.total_seconds())
    view = await rig.service.get(person("alice"), RECORD_ID)
    assert view.needsReverification is True and view.usable is False and view.status == "ready"
    assert rig.provider.reads(f"/photoavatars/{PROVIDER_ID}") == 0  # no re-check inside the cooldown
    # Unknown provider answer after the cooldown: still refused, and the cooldown restarts.
    rig.clock.advance(REVERIFY_COOLDOWN)
    rig.provider.avatar_reply = httpx.Response(503)
    error = await refusal(rig, person("alice"))
    assert error.code == "avatar_needs_reverification"
    # Exactly one re-check ran (a single status read with its configured transient retries).
    assert rig.provider.reads(f"/photoavatars/{PROVIDER_ID}") == rig.settings.outbound_retry_max_attempts
    assert (await rig.stored()).liveVerificationFailedAt == rig.clock.now
    # Control: a fresh Succeeded read after the next cooldown clears it and grants.
    rig.clock.advance(REVERIFY_COOLDOWN)
    rig.provider.avatar_reply = httpx.Response(200, json={"state": "Succeeded"})
    assert await rig.service.resolve_live_avatar(person("alice"), RECORD_ID)
    cleared = await rig.stored()
    assert cleared.liveVerificationFailedAt is None and cleared.liveVerificationCode is None
    assert (await rig.service.get(person("alice"), RECORD_ID)).needsReverification is False


@pytest.mark.parametrize("reply, failure", [
    (httpx.Response(200, json={"state": "Failed", "error": {"code": "VerificationFailed"}}), "provider_failed"),
    (httpx.Response(404, json=NOT_FOUND), "provider_missing"),
])
async def test_a_failed_or_missing_avatar_becomes_terminal_but_stays_visible_and_deletable(reply, failure):
    rig = Rig()
    await rig.seed()
    await rig.service.mark_live_avatar_verification_failed(person("alice"), RECORD_ID)
    rig.clock.advance(REVERIFY_COOLDOWN)
    rig.provider.avatar_reply = reply
    assert (await refusal(rig, person("alice"))).code == "avatar_not_ready"
    stored = await rig.stored()
    assert (stored.status, stored.failureCode) == ("failed", failure)
    listed = await rig.service.list(person("alice"))
    assert [item.id for item in listed.avatars] == [RECORD_ID]
    assert listed.avatars[0].needsReverification is False


async def test_marking_ignores_foreign_unknown_malformed_and_not_ready_records():
    rig = Rig()
    await rig.seed()
    await rig.seed(owner="alice", record_id=OTHER_RECORD_ID, status="generating", readyAt=None)
    for user, record_id in (
        (person("bob"), RECORD_ID), (person("alice"), "ab" * 16),
        (person("alice"), "bad id"), (person("alice"), OTHER_RECORD_ID),
    ):
        assert await rig.service.mark_live_avatar_verification_failed(user, record_id) is False
    assert (await rig.stored()).liveVerificationFailedAt is None
    assert (await rig.stored(record_id=OTHER_RECORD_ID)).liveVerificationFailedAt is None
    # Control: the owner's ready record is marked; an unbounded provider code is dropped.
    assert await rig.service.mark_live_avatar_verification_failed(
        person("alice"), RECORD_ID, provider_code="not a code; prompt text",
    )
    marked = await rig.stored()
    assert marked.liveVerificationFailedAt is not None and marked.liveVerificationCode is None


async def test_the_app_state_entry_points_report_a_disabled_feature():
    user = person("alice")
    for state in (SimpleNamespace(settings=make_settings()), SimpleNamespace()):
        with pytest.raises(LiveAvatarError) as caught:
            await live.resolve_live_avatar(state, user, RECORD_ID)
        assert (caught.value.code, caught.value.reason) == ("photo_avatars_unavailable", "disabled")
        assert await live.mark_live_avatar_verification_failed(state, user, RECORD_ID) is False
    rig = Rig()
    await rig.seed()
    state = SimpleNamespace(settings=rig.settings, photo_avatars=rig.service)
    assert (await live.resolve_live_avatar(state, user, RECORD_ID)).record_id == RECORD_ID
    assert await live.mark_live_avatar_verification_failed(state, user, RECORD_ID) is True
    # A service left behind after the flag turns off is still refused.
    off = SimpleNamespace(settings=make_settings(), photo_avatars=rig.service)
    with pytest.raises(LiveAvatarError):
        await live.resolve_live_avatar(off, user, RECORD_ID)


def test_error_codes_are_the_published_set():
    assert LIVE_AVATAR_ERROR_CODES == {
        "not_found", "avatar_not_ready", "avatar_needs_reverification", "avatar_home_changed",
        "photo_avatars_unavailable", "policy_denied",
    }


async def test_concurrent_resolves_share_one_capability_probe():
    rig = Rig()
    await rig.seed()
    grants = await asyncio.gather(
        *(rig.service.resolve_live_avatar(person("alice"), RECORD_ID) for _ in range(8)),
    )
    assert len(set(grants)) == 1
    assert rig.provider.reads("/features") == 1
