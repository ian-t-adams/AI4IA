"""Paired controls for claims, policy intersections and owner-bound resolution."""
from __future__ import annotations

import json
import time

import pytest

from ai4ia_api.auth.base import AuthenticatedUser
from ai4ia_api.auth.policy_claims import verified_policy_claims
from ai4ia_api.catalog import load_catalog
from ai4ia_api.entitlements.memory_store import InMemoryEntitlementStore
from ai4ia_api.entitlements.models import Entitlement
from ai4ia_api.entitlements.service import EntitlementService
from ai4ia_api.policy.models import PolicyError, PolicyRequest, parse_policy_config
from ai4ia_api.policy.service import PolicyService
from ai4ia_api.workflows.record_types import (
    AUTOMATION_OWNER_ID, AUTOMATION_OWNER_KIND, WORKFLOW_DEFINITION_KIND, is_definition,
)
from tests.conftest import make_settings
from tests.test_entitlement_service import CountingReader

TENANT = "tenant"
GROUP = "11111111-2222-3333-4444-555555555555"


def user(*, roles=(), groups=(), expiry=None, **claims):
    return AuthenticatedUser(
        internal_user_id="owner", subject="subject", issuer="issuer",
        tenant_id=TENANT, provider="entra",
        policy_claims=verified_policy_claims({
            "roles": list(roles), "groups": list(groups),
            "exp": expiry if expiry is not None else int(time.time()) + 3600, **claims,
        }),
    )


def service(config, **settings):
    store = InMemoryEntitlementStore()
    entitlements = EntitlementService(store, CountingReader(), Entitlement.unlimited())
    policy = PolicyService(
        make_settings(
            group_policy_enabled=True, group_policy_json=json.dumps(config),
            entra_tenant_id=TENANT, entra_allowed_tenants=TENANT, **settings,
        ),
        catalog=load_catalog(), entitlements=entitlements,
    )
    return policy, store


def domain(claim="roles", value="Researcher"):
    return {
        "default": {"allow": []},
        "mappings": [{"claim": claim, "value": value, "allow": ["read"]}],
    }


@pytest.mark.parametrize("roles,allowed", [(("Researcher",), True), (("researcher",), False), ((), False)])
async def test_role_values_are_exact_and_missing_never_grants(roles, allowed):
    policy, _ = service({"domains": {"documents": domain()}})
    result = await policy.authorize(await policy.resolve(user(roles=roles)), PolicyRequest("document.read"))
    assert result.allowed is allowed


@pytest.mark.parametrize("claims,allowed", [
    ({"groups": [GROUP]}, True), ({"groups": []}, False),
    ({"groups": None}, False), ({"groups": GROUP}, False),
    ({"groups": [GROUP, 7]}, False), ({"groups": [GROUP], "hasgroups": True}, False),
    ({"groups": [GROUP], "_claim_names": {"groups": "source"}}, False),
    ({"groups": [GROUP], "_claim_sources": {"source": {"endpoint": "https://forbidden/"}}}, False),
    ({"groups": [GROUP] * 201}, False),
])
async def test_group_claim_shape_and_overage_do_not_increase_authority(claims, allowed):
    policy, _ = service({"domains": {"documents": domain("groups", GROUP)}})
    actor = user()
    actor.policy_claims = verified_policy_claims({"exp": int(time.time()) + 3600, **claims})
    result = await policy.authorize(await policy.resolve(actor), PolicyRequest("document.read"))
    assert result.allowed is allowed


async def test_denies_and_intersecting_constraints_win_over_multiple_grants():
    policy, _ = service({"domains": {"documents": {
        "default": {"allow": ["read", "upload"]},
        "mappings": [
            {"claim": "roles", "value": "restricted", "restrict": ["read"]},
            {"claim": "groups", "value": GROUP, "deny": ["read"]},
        ],
    }}})
    request = PolicyRequest("document.read")
    assert (await policy.authorize(await policy.resolve(user(roles=["restricted"])), request)).allowed
    both = await policy.resolve(user(roles=["restricted"], groups=[GROUP]))
    assert not (await policy.authorize(both, request)).allowed
    assert not (await policy.authorize(both, PolicyRequest("document.upload"))).allowed


async def test_latest_individual_limit_is_a_ceiling_not_replaced_by_group_grant():
    policy, store = service({"spend": {
        "default": {"tokensPerDay": 100},
        "mappings": [{"claim": "roles", "value": "Researcher", "limits": {"tokensPerDay": 80}}],
    }})
    await store.put(Entitlement(id="owner", userId="owner", tokensPerDay=50))
    assert (await policy.resolve(user(roles=["Researcher"]))).limits.tokensPerDay == 50
    await store.put(Entitlement(id="owner", userId="owner", tokensPerDay=20))
    assert (await policy.resolve(user(roles=["Researcher"]))).limits.tokensPerDay == 20


async def test_store_outage_is_unavailable_and_disabled_is_not_overridden(monkeypatch):
    policy, store = service({})
    actor = await policy.resolve(user())
    request = PolicyRequest("tool.invoke", tool_name="calculator")
    assert (await policy.authorize(actor, request)).allowed

    async def unavailable(_owner):
        raise RuntimeError("private store detail")

    monkeypatch.setattr(store, "get_strict", unavailable)
    result = await policy.authorize(actor, request)
    assert result.outcome == "unavailable"
    assert result.reason == "policy_unavailable"


async def test_same_actor_expiry_and_owner_mismatch_are_not_transferable(monkeypatch):
    policy, _ = service({})
    expiry = int(time.time()) + 3600
    actor = await policy.resolve(user(expiry=expiry), expected_owner="owner")
    assert (await policy.authorize(actor, PolicyRequest("tool.invoke", tool_name="calculator"))).allowed
    monkeypatch.setattr("ai4ia_api.policy.service.time.time", lambda: expiry)
    result = await policy.authorize(actor, PolicyRequest("tool.invoke", tool_name="calculator"))
    assert result.reason == "reauthentication_required"
    with pytest.raises(PolicyError):
        await policy.resolve(user(), expected_owner="other")


async def test_unattended_has_no_claim_authority_but_operator_only_policy_works():
    policy, _ = service({"domains": {"documents": domain("groups", GROUP)}})
    result = await policy.authorize(await policy.resolve_unattended("owner"), PolicyRequest("document.read"))
    assert result.outcome == "unavailable"
    assert result.reason == "reauthentication_required"
    operator, _ = service({"domains": {"documents": {"default": {"allow": ["read"]}}}})
    assert (await operator.authorize(
        await operator.resolve_unattended("owner"), PolicyRequest("document.read"),
    )).allowed


async def test_admin_ceiling_and_review_role_do_not_create_global_admin():
    operation = "admin.usage.read"
    config = {"domains": {"admin": {
        "default": {"allow": []},
        "mappings": [{"claim": "roles", "value": "reader", "allow": [operation]}],
    }}}
    policy, _ = service(config)
    actor = await policy.resolve(user(roles=["reader"]))
    assert not (await policy.authorize(actor, PolicyRequest(operation))).allowed
    policy.settings.group_policy_json = json.dumps({**config, "adminCeiling": [operation]})
    assert (await policy.authorize(actor, PolicyRequest(operation))).allowed
    assert not (await policy.authorize(actor, PolicyRequest("admin.entitlements.write"))).allowed
    assert not (await policy.authorize(actor, PolicyRequest("publication.review"))).allowed


@pytest.mark.parametrize("raw", [
    '{"version":1,"version":1}', '{"domains":{"documents":{}}}',
    '{"adminCeiling":["admin.everything"]}', '{"spend":{"default":{"tokensPerDay":NaN}}}',
    '{"domains":{"documents":{"default":{"allow":["*"]}}}}',
    '{"domains":{"documents":{"default":{"allow":["read","read"]}}}}',
])
def test_bad_configuration_is_not_silently_accepted(raw):
    with pytest.raises(ValueError):
        parse_policy_config(raw)


def test_legacy_definition_control_record_filters_are_nonvacuous():
    draft = {"id": "legacy", "name": "legacy", "userId": "owner"}
    assert is_definition(draft, user_id="owner", kind=WORKFLOW_DEFINITION_KIND)
    assert is_definition(
        {**draft, "recordKind": WORKFLOW_DEFINITION_KIND},
        user_id="owner", kind=WORKFLOW_DEFINITION_KIND,
    )
    for value in (
        {**draft, "recordKind": AUTOMATION_OWNER_KIND},
        {**draft, "id": AUTOMATION_OWNER_ID, "name": AUTOMATION_OWNER_ID},
        {**draft, "userId": "other"}, {**draft, "recordKind": None},
    ):
        assert not is_definition(value, user_id="owner", kind=WORKFLOW_DEFINITION_KIND)
